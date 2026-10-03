"""Removal of brief operator-selection camera flashes.

The game changes its 3-D camera when an operator is selected.  A whole-frame
stabilizer also moves the HUD and cannot undo building/sprite parallax.  This
module removes bounded selection flashes and verifies a return to the original
normal view. Flow mode also accepts verified selection UI when the selected
view moves too far for reliable floor tracking. Long selections retain their
ten-times speed. All times use the source/container clock.
"""
from __future__ import annotations

from fractions import Fraction
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable

import cv2
import numpy as np

from .detect import Detector


class _FloorMotion:
    """Compare a small, mostly planar board region with one normal-view anchor."""

    def __init__(self, reference: np.ndarray):
        self.reference = reference
        self.height, self.width = reference.shape
        mask = np.zeros(reference.shape, np.uint8)
        # Ratio coordinates also work for wide phone recordings and rounded
        # corners. Exclude the left operator panel, top HUD and bottom cards.
        mask[round(.52 * self.height):round(.81 * self.height),
             round(.35 * self.width):round(.82 * self.width)] = 255
        self.points = cv2.goodFeaturesToTrack(reference, 300, .01, 4,
                                              mask=mask, blockSize=5)

    def estimate(self, current: np.ndarray) -> dict | None:
        if self.points is None or len(self.points) < 16:
            return None
        target, status, _ = cv2.calcOpticalFlowPyrLK(
            self.reference, current, self.points, None,
            winSize=(31, 31), maxLevel=4)
        if target is None or status is None:
            return None
        back, reverse_status, _ = cv2.calcOpticalFlowPyrLK(
            current, self.reference, target, None,
            winSize=(31, 31), maxLevel=4)
        if back is None or reverse_status is None:
            return None
        consistent = ((status[:, 0] > 0) & (reverse_status[:, 0] > 0)
                      & (np.linalg.norm(back - self.points, axis=2)[:, 0] < 1.0))
        if int(consistent.sum()) < 16:
            return None
        source = self.points[consistent].reshape(-1, 2)
        target = target[consistent].reshape(-1, 2)
        return self.measure(source, target)

    def measure(self, source: np.ndarray, target: np.ndarray) -> dict | None:
        """Validate a distributed planar fit; shared by direct and short-chain tracking."""
        matrix, inliers = cv2.findHomography(target, source, cv2.RANSAC, 2.0)
        if matrix is None or inliers is None or not np.isfinite(matrix).all():
            return None
        good = inliers[:, 0] > 0
        count = int(good.sum())
        ratio = count / len(source)
        source, target = source[good], target[good]
        if count < 16 or ratio < .65:
            return None
        extent = np.ptp(source, axis=0)
        if extent[0] < .16 * self.width or extent[1] < .09 * self.height:
            return None
        displacement = np.linalg.norm(target - source, axis=1) / self.height
        projected = cv2.perspectiveTransform(target.reshape(-1, 1, 2), matrix).reshape(-1, 2)
        error = np.linalg.norm(projected - source, axis=1)
        if not np.isfinite(error).all() or float(np.median(error)) > 1.0:
            return None
        # Reject a collapsed or wildly distorted fit from a different scene.
        roi_corners = np.float32([[[.35 * self.width, .52 * self.height],
                                  [.82 * self.width, .52 * self.height],
                                  [.82 * self.width, .81 * self.height],
                                  [.35 * self.width, .81 * self.height]]])
        warped = cv2.perspectiveTransform(roi_corners, matrix)[0]
        area = abs(float(cv2.contourArea(warped)))
        original_area = .47 * .29 * self.width * self.height
        if not np.isfinite(warped).all() or not .4 <= area / original_area <= 2.5:
            return None
        return {"median": float(np.median(displacement)),
                "p90": float(np.percentile(displacement, 90)),
                "inliers": count, "ratio": float(ratio),
                "residual_px": float(np.median(error))}


def _sequential_opening(reference: np.ndarray,
                        frames: list[tuple[float, np.ndarray]]) -> list[tuple[float, dict | None]]:
    """Confirm large moves through consecutive source frames, without bridging a failure.

    This is only a jump detector. The return is always independently matched
    against the original reference, so tracking drift cannot declare recovery.
    """
    tracker = _FloorMotion(reference)
    if tracker.points is None:
        return []
    original = tracker.points.copy()
    points = tracker.points.copy()
    previous = reference
    results = []
    last_time = None
    for timestamp, current in frames:
        if last_time is not None and timestamp - last_time > .085:
            break
        target, status, _ = cv2.calcOpticalFlowPyrLK(
            previous, current, points, None, winSize=(31, 31), maxLevel=4)
        if target is None or status is None:
            break
        back, reverse_status, _ = cv2.calcOpticalFlowPyrLK(
            current, previous, target, None, winSize=(31, 31), maxLevel=4)
        if back is None or reverse_status is None:
            break
        keep = ((status[:, 0] > 0) & (reverse_status[:, 0] > 0)
                & (np.linalg.norm(back - points, axis=2)[:, 0] < 1.0))
        if int(keep.sum()) < 16:
            break
        old = points[keep].reshape(-1, 2)
        new = target[keep].reshape(-1, 2)
        origins = original[keep].reshape(-1, 2)
        matrix, inliers = cv2.findHomography(new, old, cv2.RANSAC, 2.0)
        if matrix is None or inliers is None:
            break
        good = inliers[:, 0] > 0
        if int(good.sum()) < 16 or float(good.mean()) < .65:
            break
        measurement = tracker.measure(origins[good], new[good])
        if measurement is None:
            break
        results.append((timestamp, measurement))
        points = new[good].reshape(-1, 1, 2)
        original = origins[good].reshape(-1, 1, 2)
        previous, last_time = current, timestamp
    return results


def _decode_window(source: Path, ffmpeg: str, start: float, end: float,
                   width: int, height: int) -> list[tuple[float, np.ndarray]]:
    """Accurate source seeking and original PTS, including VFR input.

    OpenCV millisecond seeking derives positions from nominal frame rate on
    some variable-rate MP4 files. FFmpeg decodes the original timestamps before
    processing. At most a roughly two-second grayscale window lives in memory.
    """
    if end <= start:
        return []
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-nostdin",
               "-ss", f"{start:.9f}", "-t", f"{end - start:.9f}", "-i", str(source),
               "-map", "0:v:0", "-an", "-vf",
               f"trim=end={end - start:.9f},scale={width}:{height},format=gray,showinfo",
               "-vsync", "0", "-threads", "2", "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-3000:])
    size = width * height
    if len(result.stdout) % size:
        raise RuntimeError("Incomplete grayscale frame in camera-analysis window.")
    frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, height, width)
    # showinfo runs after the source-clock seek and before any output frame
    # rate conversion. A timestamp such as 0.0325667 means the first source
    # frame is start+0.0325667, not start. That matters at a short camera cut.
    timestamps = [float(match.group(1)) for match in re.finditer(
        rb"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([-+\d.eE]+)", result.stderr)]
    if len(timestamps) != len(frames):
        raise RuntimeError("Source frame timestamps and decoded camera frames disagree.")
    return [(start + timestamp, frame) for timestamp, frame in zip(timestamps, frames)]


def _merge_cuts(cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(cuts):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _retained_duration(segments: list[dict], start: float, end: float) -> float:
    return sum(max(0.0, min(float(s["end"]), end) - max(float(s["start"]), start))
               / float(s.get("speed", 1)) for s in segments)


def _subtract_cuts(segments: list[dict], cuts: list[tuple[float, float]]) -> list[dict]:
    """Preserve original timing/speeds/metadata; flag the new camera seams."""
    cuts = _merge_cuts(cuts)
    output: list[dict] = []
    for original in segments:
        pieces = [(float(original["start"]), float(original["end"]))]
        for cut_start, cut_end in cuts:
            remaining = []
            for start, end in pieces:
                if cut_end <= start or cut_start >= end:
                    remaining.append((start, end))
                else:
                    if start < cut_start:
                        remaining.append((start, min(end, cut_start)))
                    if cut_end < end:
                        remaining.append((max(start, cut_end), end))
            pieces = remaining
            if not pieces:
                break
        for start, end in pieces:
            if end - start > 1e-9:
                copy = dict(original)
                copy.update(start=start, end=end)
                # A preexisting seam marker belongs only to the original head.
                if start > float(original["start"]) + 1e-9:
                    copy.pop("camera_gap_before", None)
                output.append(copy)
    for start, end in cuts:
        if _retained_duration(segments, start, end) <= 1e-9:
            continue
        following = next((s for s in output if s["start"] >= end - 1e-9), None)
        if following is not None:
            following["camera_gap_before"] = True
    return output


def _candidates(runs: list[dict], segments: list[dict], flash_seconds: float) -> list[dict]:
    """Combine one interaction across tiny classifier fragments, including pause-only."""
    result = []
    index = 0
    while index < len(runs):
        run = runs[index]
        if run.get("state") not in {"pause", "bullet"}:
            index += 1
            continue
        group = [run]
        index += 1
        while index < len(runs):
            following = runs[index]
            if abs(float(following['start']) - float(group[-1]['end'])) >= .025:
                break
            if following.get('state') in {'pause', 'bullet'}:
                group.append(following)
                index += 1
            elif (float(following['end']) - float(following['start']) <= .05
                  and index + 1 < len(runs)
                  and runs[index + 1].get('state') in {'pause', 'bullet'}
                  and abs(float(runs[index + 1]['start']) - float(following['end'])) < .025):
                group.append(following)
                index += 1
            else:
                break
        bullet = [r for r in group if r.get("state") == "bullet"]
        length = sum(float(r["end"]) - float(r["start"]) for r in bullet)
        start, end = float(group[0]["start"]), float(group[-1]["end"])
        retained = _retained_duration(segments, start, end)
        if retained > flash_seconds + 1e-9:
            continue
        result.append({"start": start, "end": end,
                       "bullet_source_seconds": length,
                       "interaction_output_seconds": retained,
                       "contains_pause": any(r.get("state") == "pause" for r in group),
                       "bullet_heads": [{"start": float(r["start"]), "end": float(r["end"])}
                                        for r in bullet]})
    return result


def _stable(measurement: dict | None) -> bool:
    return measurement is not None and measurement["median"] < .003 and measurement["p90"] < .005


def _settle_return(frames: list[tuple[float, np.ndarray]],
                   measured: list[tuple[float, dict | None]], index: int,
                   detector: Detector | None, segments: list[dict],
                   boundaries: list[tuple[float, str]], fps: float,
                   settle_frames: int) -> tuple[int, dict]:
    """Skip at most two real source frames, bounded by *output* frame time.

    The first stable floor pair confirms the normal view, but small building
    parallax and CFR nearest-frame selection can still show its first pose.
    A verified next pose gets a short guard; a second is useful only when the
    first pair still moves. Never manufacture PTS or cross a long VFR hold,
    pause gap, new selection, unverified HUD, or unmatched floor.
    """
    original = measured[index][0]
    guard = {"requested_output_frames": settle_frames,
             "original_return_time": original, "cut_end": original,
             "source_frames_skipped": 0, "source_seconds_removed": 0.0,
             "output_seconds_removed": 0.0, "output_frames_removed": 0.0,
             "reason": "disabled", "samples": []}
    if settle_frames == 0:
        return index, guard
    normal_segment = next((s for s in segments
                           if float(s["start"]) <= original < float(s["end"])
                           and float(s.get("speed", 1)) != 10), None)
    if normal_segment is None:
        guard["reason"] = "normal_view_not_retained"
        return index, guard
    boundary, boundary_reason = min(
        [*boundaries, (float(normal_segment["end"]), "normal_segment_boundary")],
        key=lambda item: item[0])
    endpoint = index
    budget = settle_frames / fps
    for step in range(1, settle_frames + 1):
        following = index + step
        if following >= len(measured):
            guard["reason"] = "no_following_source_frame"
            break
        timestamp, measurement = measured[following]
        if timestamp >= boundary - 1e-9:
            guard["reason"] = boundary_reason
            break
        output_seconds = _retained_duration(segments, original, timestamp)
        # A one-source-frame VFR hold can span many CFR output frames. The
        # budget counts the entire removed interval after existing speedups.
        if timestamp <= measured[endpoint][0] or output_seconds > budget + 1e-9:
            guard["reason"] = "output_frame_budget_reached"
            break
        if not _stable(measurement):
            guard["reason"] = "unreliable_floor_after_return"
            break
        ui = detector.classify_image(frames[following][1]) if detector is not None else {}
        sample = {"timestamp": timestamp, "ui_state": ui.get("state", "unknown"),
                  "floor_median": measurement["median"], "floor_p90": measurement["p90"],
                  "output_frames_removed": output_seconds * fps}
        guard["samples"].append(sample)
        if ui.get("state") not in {"one", "two"}:
            guard["reason"] = "unverified_normal_ui_after_return"
            break
        endpoint = following
        guard.update(cut_end=timestamp, source_frames_skipped=step,
                     source_seconds_removed=timestamp - original,
                     output_seconds_removed=output_seconds,
                     output_frames_removed=output_seconds * fps,
                     reason="one_stable_source_frame" if step == 1
                            else "residual_motion_two_source_frames")
        if step == 1 and settle_frames == 2:
            relative = _FloorMotion(frames[index][1]).estimate(frames[following][1])
            if relative is None:
                guard["reason"] = "one_frame_relative_motion_unavailable"
                break
            guard["residual_floor_motion"] = relative
            # About 0.2/0.4 pixels on an 852-high source. Tracking is against
            # consecutive poses here; return safety still uses the original
            # pre-selection anchor to avoid cumulative tracking drift.
            if relative["median"] <= .00025 and relative["p90"] <= .0005:
                guard["reason"] = "one_frame_sufficient"
                break
    return endpoint, guard


def _verify_selected_ui(detector: Detector, source: Path, ffmpeg: str,
                        heads: list[dict], width: int, height: int) -> tuple[bool, list[dict], float]:
    """Inspect sparse entry frames, including bullet time after a long pause.

    Only at most 120 ms around each real bullet entry is decoded. A pause can
    last minutes without increasing memory or forcing a full interaction scan.
    The same CN UI detector/configuration used for analysis verifies both the
    operator panel and live pause-bars; an absent speed label alone is unsafe.
    """
    samples = []
    decode_seconds = 0.0
    for head in heads:
        started = time.perf_counter()
        frames = _decode_window(source, ffmpeg, head["start"],
                                min(head["end"], head["start"] + .12), width, height)
        decode_seconds += time.perf_counter() - started
        # Sampling preserves the first, middle and final available source PTS.
        # Tiny one-frame classifier fragments are still explicitly verified.
        chosen = sorted(set(np.linspace(0, len(frames) - 1,
                                       min(5, len(frames)), dtype=int))) if frames else []
        for index in chosen:
            timestamp, frame = frames[index]
            ui = detector.classify_image(frame)
            samples.append({"timestamp": timestamp, "state": ui["state"],
                            "panel": ui.get("panel", -1.0), "bars": ui.get("bars", -1.0)})
            if ui["state"] == "bullet":
                return True, samples, decode_seconds
    return False, samples, decode_seconds


def optimize_camera(input_path: str | os.PathLike[str], report: dict, *,
                    ffmpeg: str = "ffmpeg", flash_ms: float = 1000,
                    recovery_ms: float = 1000, analysis_width: int = 640,
                    policy: str = "flow", settle_frames: int = 2,
                    output_fps: object | None = None,
                    progress: Callable | None = None) -> tuple[list[dict], dict]:
    """Remove bounded selection flashes; leave ``report`` untouched.

    Flow mode prioritizes continuity: verified live operator-selection UI and
    two normal-UI frames matched to the original floor can replace a large-jump
    measurement. Strict mode retains the earlier measured-jump requirement.
    ``flash_ms`` counts retained output time after speed changes; recovery uses
    source time. ``settle_frames`` adds a verified return guard with at most
    0/1/2 output frames of time. Its actual source-frame count can be smaller
    on VFR input. Unsupported scenes retain their original frames.
    """
    if policy not in {"flow", "strict"}:
        raise ValueError("policy must be flow or strict.")
    if not math.isfinite(float(flash_ms)) or not 0 <= float(flash_ms) <= 5000:
        raise ValueError("flash_ms must be between 0 and 5000.")
    if not math.isfinite(float(recovery_ms)) or not 0 <= float(recovery_ms) <= 2000:
        raise ValueError("recovery_ms must be between 0 and 2000.")
    if not 320 <= int(analysis_width) <= 1280:
        raise ValueError("analysis_width must be between 320 and 1280.")
    if isinstance(settle_frames, bool) or not isinstance(settle_frames, int) or settle_frames not in {0, 1, 2}:
        raise ValueError("settle_frames must be 0, 1 or 2.")
    try:
        rate = Fraction(str(output_fps if output_fps is not None else (report.get("nominal_fps") or "60"))).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        raise ValueError("Invalid output frame rate for the return guard.") from None
    if not 1 <= float(rate) <= 240:
        raise ValueError("Output frame rate must be between 1 and 240.")
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    started = time.perf_counter()
    original = [dict(s) for s in report.get("segments", [])]
    runs = report.get("runs", [])
    width = int(analysis_width)
    height = max(2, round(int(report["height"]) * width / int(report["width"])))
    detector = (Detector(int(report["width"]), int(report["height"]), report.get("config"))
                if policy == "flow" else None)
    # Strict mode keeps its existing measured-jump/return policy. Only this
    # optional guard additionally verifies the HUD before moving its endpoint.
    guard_detector = (detector or Detector(int(report["width"]), int(report["height"]), report.get("config"))) if settle_frames else None
    candidates = _candidates(runs, original, float(flash_ms) / 1000)
    events: list[dict] = []
    cuts: list[tuple[float, float]] = []
    original_cuts: list[tuple[float, float]] = []
    decode_seconds = tracking_seconds = 0.0
    long_bullets = [r for r in runs if r.get("state") == "bullet"
                    and (float(r["end"]) - float(r["start"])) / 10 > float(flash_ms) / 1000]
    for index, candidate in enumerate(candidates):
        event = dict(candidate)
        event.update(status="kept", reason="no_reliable_floor_matches")
        start, closing = candidate["start"], candidate["end"]
        # A preceding retained normal frame is required. Selection after a
        # long gap cannot use the dark pause/selection view as its anchor.
        # UI text can lag the first camera movement by >100ms. Flow mode
        # includes a wider normal prefix, still bounded and independent of the
        # possibly long PAUSE interval inside an interaction.
        anchor_start = max(0.0, start - (.32 if policy == "flow" else .18))
        preceding = next((s for s in reversed(original)
                          if float(s["start"]) <= anchor_start < float(s["end"])
                          and float(s.get("speed", 1)) != 10), None)
        separate_anchor = None
        if preceding is None:
            preceding = next((s for s in reversed(original)
                              if float(s['end']) <= start + 1e-9
                              and float(s.get('speed', 1)) != 10
                              and float(s['end']) - float(s['start']) >= .025
                              and start - float(s['end']) <= 3.0), None)
            if preceding is not None:
                # A VFR recording can hold its final normal frame for >80ms.
                # Seeking into that hold and decoding only 80ms yields zero
                # images. Include the preceding normal interval's last 320ms
                # and select an actual decoded frame instead of guessing PTS.
                separate_anchor = max(float(preceding['start']), float(preceding['end']) - .32)
        if preceding is None:
            event["reason"] = "no_normal_view_before_selection"
            events.append(event)
            if progress:
                progress(index + 1, len(candidates), "Checked camera flash")
            continue
        # Never trim into an independent long bullet-time selection.
        recovery_end = min(float(report["duration"]), closing + float(recovery_ms) / 1000 + 1 / 60)
        for long_run in long_bullets:
            if closing < float(long_run["start"]) < recovery_end:
                recovery_end = float(long_run["start"])
        try:
            st = time.perf_counter()
            opening_end = min(closing, start + .20)
            opening = _decode_window(source, ffmpeg, anchor_start, opening_end, width, height)
            recovery = _decode_window(source, ffmpeg, closing, recovery_end, width, height)
            anchor = (_decode_window(source, ffmpeg, separate_anchor,
                                    float(preceding['end']), width, height)
                      if separate_anchor is not None else opening)
            decode_seconds += time.perf_counter() - st
            if not opening or not anchor or len(recovery) < 2:
                event["reason"] = "insufficient_frames"
                events.append(event)
                continue
            tracker = _FloorMotion(anchor[0][1])
            if detector is not None:
                anchor_ui = detector.classify_image(anchor[0][1])
                event["anchor_ui_state"] = anchor_ui["state"]
                if anchor_ui["state"] not in {"one", "two"}:
                    event["reason"] = "no_verified_normal_ui_before_selection"
                    events.append(event)
                    continue
            st = time.perf_counter()
            measured_opening = [(timestamp, tracker.estimate(frame)) for timestamp, frame in opening]
            measured_recovery = [(timestamp, tracker.estimate(frame)) for timestamp, frame in recovery]
            tracking_seconds += time.perf_counter() - st
            strong = [(t, m) for t, m in measured_opening + measured_recovery
                      if m is not None and m["median"] > .01]
            confirmation = 'direct'
            if not strong and separate_anchor is None:
                sequential = _sequential_opening(anchor[0][1], opening)
                confirmed = [(t1, m1) for (t1, m1), (t2, m2) in zip(sequential, sequential[1:])
                             if m1['median'] > .01 and m2['median'] > .01 and t2 - t1 <= .085]
                if confirmed:
                    strong = confirmed
                    confirmation = 'sequential_opening'
                    # Use only the chain's reliable prefix to locate entry.
                    measured_opening = sequential
            selected_ui = False
            if not strong and policy == "flow" and candidate["bullet_heads"]:
                selected_ui, evidence, extra_decode = _verify_selected_ui(
                    detector, source, ffmpeg, candidate["bullet_heads"], width, height)
                decode_seconds += extra_decode
                event["selection_ui_verified"] = selected_ui
                event["selection_ui_samples"] = evidence
                if selected_ui:
                    confirmation = "verified_selection_ui"
            if not strong and not selected_ui:
                event["reason"] = "no_confirmed_camera_jump"
                events.append(event)
                continue
            first_jump = strong[0][0] if strong else start
            stable_before = [(t, m) for t, m in measured_opening
                             if t < first_jump and _stable(m)]
            if not stable_before:
                event["reason"] = "no_stable_opening"
                events.append(event)
                continue
            # Preserve earlier normal footage while allowing a one-frame
            # guard for nearest-frame rounding in the final CFR export. The
            # UI classification can lag camera motion. Flow permits at most
            # 200ms lookback; strict retains the previous 100ms boundary.
            cut_start = max(start - (.20 if policy == "flow" else .10), anchor_start,
                            min(start, stable_before[-1][0] - 1 / 60))
            cut_end = None
            return_measurement = None
            return_index = None
            for recovery_index, ((t1, m1), (t2, m2)) in enumerate(zip(measured_recovery, measured_recovery[1:])):
                if not (_stable(m1) and _stable(m2)):
                    continue
                # A matched floor can still belong to a selected view while
                # UI fades. Every flow cut independently requires normal
                # speed UI on two recovered frames before it is allowed.
                if policy == "flow":
                    frame1 = next(frame for timestamp, frame in recovery if timestamp == t1)
                    frame2 = next(frame for timestamp, frame in recovery if timestamp == t2)
                    ui1, ui2 = detector.classify_image(frame1), detector.classify_image(frame2)
                    if ui1["state"] not in {"one", "two"} or ui2["state"] not in {"one", "two"}:
                        continue
                    event["return_ui_states"] = [ui1["state"], ui2["state"]]
                if t1 <= closing + float(recovery_ms) / 1000 + 1e-9:
                    cut_end = t1
                    return_measurement = m1
                    return_index = recovery_index
                    break
            if cut_end is None:
                event["reason"] = "camera_did_not_return_within_limit"
                events.append(event)
                continue
            if cut_end <= cut_start or _retained_duration(original, cut_start, cut_end) <= 1e-9:
                event["reason"] = "no_new_frames_to_remove"
                events.append(event)
                continue
            original_cut_end = cut_end
            boundaries = [(float(report["duration"]), "source_end"),
                          (closing + float(recovery_ms) / 1000, "recovery_window_end")]
            boundaries.extend((float(run["start"]), "next_interaction_boundary")
                              for run in runs if run.get("state") in {"pause", "bullet"}
                              and float(run["start"]) >= closing - 1e-9)
            st = time.perf_counter()
            try:
                guarded_index, guard = _settle_return(
                    recovery, measured_recovery, return_index, guard_detector,
                    original, boundaries, float(rate), settle_frames)
            except (RuntimeError, cv2.error, ValueError) as error:
                # A failed optional check must not discard an independently
                # confirmed camera cut. Keep precisely the old endpoint.
                guarded_index = return_index
                guard = {"requested_output_frames": settle_frames,
                         "original_return_time": original_cut_end,
                         "cut_end": original_cut_end, "source_frames_skipped": 0,
                         "source_seconds_removed": 0.0, "output_seconds_removed": 0.0,
                         "output_frames_removed": 0.0, "reason": "guard_analysis_failed",
                         "detail": str(error)[-300:], "samples": []}
            tracking_seconds += time.perf_counter() - st
            cut_end, return_measurement = measured_recovery[guarded_index]
            event["settle_guard"] = guard
            peak = max((m for _, m in strong), key=lambda m: m["median"]) if strong else None
            event.update(status="removed", reason=("short_selection_ui_return_confirmed" if selected_ui
                                                   else "short_selection_camera_return_confirmed"),
                         cut_start=cut_start, cut_end=cut_end,
                         output_seconds_removed=_retained_duration(original, cut_start, cut_end),
                         jump_confirmation=confirmation, anchor_time=anchor[0][0],
                         new_source_seconds_removed=sum(max(0.0, min(float(s["end"]), cut_end)
                             - max(float(s["start"]), cut_start)) for s in original),
                         opening_lookback_seconds=start - cut_start,
                         recovery_source_seconds=cut_end - closing,
                         opening_floor_shift=stable_before[-1][1]["median"],
                         return_floor_shift=return_measurement["median"],
                         return_floor_p90=return_measurement["p90"],
                         peak_floor_shift=peak["median"] if peak else None,
                         floor_inliers=peak["inliers"] if peak else return_measurement["inliers"],
                         floor_inlier_ratio=peak["ratio"] if peak else return_measurement["ratio"],
                         floor_residual_px=peak["residual_px"] if peak else return_measurement["residual_px"])
            cuts.append((cut_start, cut_end))
            original_cuts.append((cut_start, original_cut_end))
        except (RuntimeError, cv2.error, ValueError) as error:
            # Camera cleanup is optional; a decoding or tracking failure must
            # not prevent the original pause/speed edit from being exported.
            event["reason"] = "camera_analysis_failed"
            event["detail"] = str(error)[-500:]
        finally:
            if progress:
                progress(index + 1, len(candidates), "Checked camera flash")
        events.append(event)
    merged = _merge_cuts(cuts)
    output = _subtract_cuts(original, merged)
    if not output:
        output, merged = original, []
    before = sum((s["end"] - s["start"]) / float(s.get("speed", 1)) for s in original)
    after = sum((s["end"] - s["start"]) / float(s.get("speed", 1)) for s in output)
    baseline_removed = sum(_retained_duration(original, start, end)
                           for start, end in _merge_cuts(original_cuts))
    return output, {"mode": "short_selection_cleanup", "policy": policy, "flash_ms": float(flash_ms),
                    "recovery_ms": float(recovery_ms), "analysis_width": width,
                    "settle_frames": settle_frames, "settle_output_fps": str(rate),
                    "settle_output_seconds_removed": max(0.0, before - after - baseline_removed),
                    "settle_source_frames_skipped": sum(e.get("settle_guard", {}).get("source_frames_skipped", 0) for e in events),
                    "candidate_count": len(candidates), "removed_events": sum(e["status"] == "removed" for e in events),
                    "cuts": [{"start": s, "end": e} for s, e in merged],
                    "output_seconds_removed": before - after,
                    "analysis_seconds": time.perf_counter() - started,
                    "decode_seconds": decode_seconds, "tracking_seconds": tracking_seconds,
                    "events": events}

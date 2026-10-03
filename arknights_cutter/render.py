"""Bounded-memory FFmpeg export for an already analysed cut timeline.

Only this module knows about encoding.  Segment times always refer to the
original recording.  Audio is encoded only once, after sample-accurate assembly,
so hundreds of cuts do not accumulate AAC encoder padding.
"""

from __future__ import annotations

import array
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Mapping


class RenderError(RuntimeError):
    """FFmpeg could not create or validate the edited recording."""


def _number(value: float) -> str:
    return f"{float(value):.9f}"


def _run(command: list[str], log: Path) -> None:
    # Keep diagnostics on disk; FFmpeg's stderr must not fill a pipe or RAM.
    with log.open("wb") as error:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=error,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        with log.open("rb") as error:
            error.seek(max(0, log.stat().st_size - 24000))
            detail = error.read().decode("utf-8", errors="replace")
        raise RenderError(f"FFmpeg exited with {result.returncode}:\n{detail}")


def _probe(path: Path, ffprobe: str) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RenderError(result.stderr.decode("utf-8", errors="replace"))
    return json.loads(result.stdout)


def _resolve_tool(tool: str | os.PathLike[str]) -> str:
    value = os.fspath(tool)
    resolved = shutil.which(value)
    if resolved:
        return resolved
    if Path(value).is_file():
        return str(Path(value).resolve())
    raise RenderError(f"Cannot find {value}. Install FFmpeg or provide its executable path.")


def _fps_fraction(value: object) -> Fraction:
    try:
        rate = Fraction(str(value)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Invalid output frame rate: {value!r}") from None
    if not 1 <= float(rate) <= 240:
        raise ValueError("Output frame rate must be between 1 and 240.")
    return rate


def normalize_segments(segments: Iterable[Mapping], duration: float) -> list[dict]:
    """Validate source times and combine adjacent segments with equal speed."""
    output: list[dict] = []
    for supplied in segments:
        start, end = float(supplied["start"]), float(supplied["end"])
        speed = float(supplied.get("speed", 1))
        if not all(math.isfinite(x) for x in (start, end, speed)):
            raise ValueError("Segment timestamps and speed must be finite.")
        if speed not in (1.0, 2.0, 10.0):
            raise ValueError("Segment speed must be 1, 2, or 10.")
        if start < 0 or end <= start or end > duration + 0.15:
            raise ValueError(f"Invalid source segment {start:.6f}–{end:.6f} for {duration:.6f}s video.")
        end = min(end, duration)
        if end <= start:
            continue
        if output and start < output[-1]["end"] - 1e-6:
            raise ValueError("Segments must be ordered and must not overlap.")
        if output and abs(start - output[-1]["end"]) < 1e-6 and speed == output[-1]["speed"]:
            output[-1]["end"] = end
        else:
            entry={"start":start,"end":end,"speed":speed}
            if supplied.get('camera_gap_before'):
                entry['camera_gap_before']=True
            output.append(entry)
    if not output:
        raise ValueError("No frames remain after pause removal.")
    return output


def make_render_plan(segments: Iterable[Mapping], fps: object,
                     sample_rate: int = 48000) -> list[dict]:
    """Allocate from cumulative time: rounding error cannot grow at each cut.

    Video determines the output clock. PCM samples are rounded from cumulative
    video frame counts, keeping audio within one sample of that clock.
    """
    rate = _fps_fraction(fps)
    total_time = Fraction(0)
    frames_before = samples_before = 0
    plan: list[dict] = []
    for segment in segments:
        entry = dict(segment)
        duration = (Fraction(str(entry["end"])) - Fraction(str(entry["start"]))) / Fraction(str(entry["speed"]))
        total_time += duration
        frames_after = round(total_time * rate)
        samples_after = round(Fraction(frames_after, 1) / rate * sample_rate)
        count = frames_after - frames_before
        if count:
            entry.update(frames=count, samples=samples_after - samples_before,
                         output_start=float(Fraction(frames_before, 1) / rate),
                         output_end=float(Fraction(frames_after, 1) / rate))
            plan.append(entry)
        frames_before, samples_before = frames_after, samples_after
    if not plan:
        raise ValueError("The retained timeline is shorter than one output frame.")
    return plan


def _audio_piece(source: Path, destination: Path, ffmpeg: str, log: Path,
                 start: float, end: float, speed: float, samples: int,
                 sample_rate: int, channels: int, fade_in: int, fade_out: int) -> None:
    filters = [f"aresample={sample_rate}:first_pts=0", "asetpts=PTS-STARTPTS"]
    if speed != 1:
        # atempo above 2 skips input samples. Chain factors <= 2, including
        # 2 * 2 * 2 * 1.25 for the game's 0.2x operator-selection bullet time.
        # Trailing silence gives each WSOLA stage enough context to flush the
        # last real samples before the final exact-duration trim.
        filters.append("apad=pad_dur=0.25")
        remaining = speed
        while remaining > 2:
            filters.append("atempo=2")
            remaining /= 2
        filters.append(f"atempo={_number(remaining)}")
    # atempo may finish a few samples early; padding and trimming restore the
    # exact output clock without moving any later cut.
    filters += [f"apad=whole_len={samples}", f"atrim=end_sample={samples}", "asetpts=PTS-STARTPTS"]
    if fade_in:
        filters.append(f"afade=t=in:ss=0:ns={fade_in}:curve=qsin")
    if fade_out:
        filters.append(f"afade=t=out:ss={max(0, samples - fade_out)}:ns={fade_out}:curve=qsin")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-ss", _number(start), "-t", _number(end - start), "-i", str(source),
               "-map", "0:a:0", "-vn", "-af", ",".join(filters),
               "-ar", str(sample_rate), "-ac", str(channels), "-f", "f32le", str(destination)]
    _run(command, log)
    expected_bytes = samples * channels * 4
    # FFmpeg can output no audio when a very short interval lands beyond the
    # audio stream. Missing trailing sound is represented by silence.
    actual_bytes = destination.stat().st_size
    with destination.open("r+b") as pcm:
        if actual_bytes < expected_bytes:
            pcm.seek(0, 2)
            left = expected_bytes - actual_bytes
            zeros = bytes(262144)
            while left:
                count = min(left, len(zeros))
                pcm.write(zeros[:count])
                left -= count
        elif actual_bytes > expected_bytes:
            pcm.truncate(expected_bytes)


def _mix_tail(head_path: Path, tail_path: Path, channels: int) -> None:
    """Blend at most tail_ms of PCM; never insert time or overlap video."""
    byte_count = min(head_path.stat().st_size, tail_path.stat().st_size)
    byte_count -= byte_count % (channels * 4)
    if not byte_count:
        return
    with head_path.open("r+b") as head, tail_path.open("rb") as tail:
        main = array.array("f")
        carry = array.array("f")
        main.frombytes(head.read(byte_count))
        carry.frombytes(tail.read(byte_count))
        if sys.byteorder != "little":
            main.byteswap()
            carry.byteswap()
        frames = len(main) // channels
        for frame in range(frames):
            # Keep incoming sound, add a fading 60% tail. The tanh-free clamp
            # avoids overflow while changing only occasional overloaded samples.
            weight = 0.6 * math.cos(math.pi * 0.5 * frame / max(1, frames - 1))
            for channel in range(channels):
                index = frame * channels + channel
                main[index] = max(-1.0, min(1.0, main[index] + carry[index] * weight))
        if sys.byteorder != "little":
            main.byteswap()
        head.seek(0)
        head.write(main.tobytes())


def export_video(input_path: str | os.PathLike[str], output_path: str | os.PathLike[str],
                 segments: Iterable[Mapping], *, ffmpeg: str | os.PathLike[str] = "ffmpeg",
                 ffprobe: str | os.PathLike[str] | None = None, audio_mode: str = "smooth",
                 tail_ms: float = 120, fade_ms: float = 8, crf: int = 18,
                 preset: str = "fast", fps: object | None = None,
                 progress: Callable[[int, int, str], None] | None = None,
                 work_dir: str | os.PathLike[str] | None = None,
                 overwrite: bool = False, threads: int | None = None) -> dict:
    """Export source-time segments to H.264 MP4 and optional AAC audio.

    Speeds 1, 2, and 10 are accepted; 10 restores a 0.2x bullet-time selection
    to the target 2x game pace. Video/audio share the same output clock.

    ``cut``: exact cut audio and pitch-preserving speedup.
    ``smooth``: same timeline plus short fades at edit boundaries (default).
    ``tail``: smooth plus opt-in 120ms sound from the beginning of removed gaps,
              mixed over the next segment; this can also carry UI sounds/music.
    ``mute``: no audio stream. A source without audio works in every mode.

    No algorithm can recover a sound that exists only in deleted pause time.
    Tail mode preserves just a short onset and cannot separate effects/music.
    Segment times are relative to the container start, matching FFmpeg input
    seeking even when the video track starts later than the audio track.
    Output is CFR; source VFR timestamps are honoured before the speed change.
    Resolution and framing are retained, padding an odd dimension by one pixel.
    ``progress`` receives (completed_segments, total_segments, message).
    """
    source, target = Path(input_path).resolve(), Path(output_path).resolve()
    threads = min(8, os.cpu_count() or 1) if threads is None else int(threads)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == target:
        raise ValueError("Input and output paths must differ.")
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    if audio_mode not in {"cut", "smooth", "tail", "mute"}:
        raise ValueError("audio_mode must be cut, smooth, tail, or mute.")
    if not 0 <= float(fade_ms) <= 100 or not 0 <= float(tail_ms) <= 1000:
        raise ValueError("fade_ms must be 0–100 and tail_ms must be 0–1000.")
    if not 0 <= int(crf) <= 51 or int(threads) < 1:
        raise ValueError("crf must be 0–51 and threads must be positive.")
    allowed_presets = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
    if preset not in allowed_presets:
        raise ValueError(f"Unknown x264 preset: {preset}")
    ffmpeg_exe = _resolve_tool(ffmpeg)
    if ffprobe is None:
        sibling = Path(ffmpeg_exe).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        ffprobe = str(sibling) if sibling.is_file() else "ffprobe"
    ffprobe_exe = _resolve_tool(ffprobe)
    info = _probe(source, ffprobe_exe)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if video is None:
        raise ValueError("Input contains no video stream.")
    container_start = float(info["format"].get("start_time") or video.get("start_time") or 0)
    video_start = float(video.get("start_time") or container_start)
    # A late-starting video stream's duration excludes the preceding audio.
    # Detector and FFmpeg seek times use the container clock, so its last frame
    # is at (video start - container start + video duration).
    if video.get("duration") not in (None, "N/A"):
        duration = max(0.0, video_start - container_start) + float(video["duration"])
    else:
        duration = float(info["format"]["duration"])
    rate = _fps_fraction(fps if fps is not None else (video.get("r_frame_rate") or "60"))
    validated = normalize_segments(segments, duration)
    plan = make_render_plan(validated, rate)
    sample_rate = 48000
    channels = min(2, max(1, int(audio.get("channels", 2)))) if audio else 0
    has_audio = audio is not None and audio_mode != "mute"
    target.parent.mkdir(parents=True, exist_ok=True)
    base_dir = Path(work_dir).resolve() if work_dir is not None else target.parent
    base_dir.mkdir(parents=True, exist_ok=True)
    expected_duration = plan[-1]["output_end"]
    source_expected_duration = sum((s["end"] - s["start"]) / s["speed"] for s in validated)
    total = len(plan)
    frame_rate_text = f"{rate.numerator}/{rate.denominator}"
    fade_samples = round(float(fade_ms) / 1000 * sample_rate)
    tails_mixed = 0
    camera_tails_mixed = 0
    with tempfile.TemporaryDirectory(prefix="ark_render_", dir=base_dir) as scratch:
        temporary = Path(scratch)
        log = temporary / "ffmpeg.log"
        assembled_audio = temporary / "audio.f32"
        audio_out = assembled_audio.open("wb") if has_audio else None
        parts: list[str] = []
        try:
            for index, segment in enumerate(plan):
                part = temporary / f"part_{index:06d}.mp4"
                source_length = segment["end"] - segment["start"]
                # trim is inside the filter, since output-side -t would apply
                # after setpts and could truncate a speedup at the wrong clock.
                filters = [f"trim=duration={_number(source_length)}", "setpts=PTS-STARTPTS"]
                if segment["speed"] != 1:
                    filters.append(f"setpts=PTS/{_number(segment['speed'])}")
                # Pad before fps: an accelerated interval shorter than half an
                # output frame can otherwise produce zero frames, even when
                # cumulative rounding allocated this segment one frame.
                filters += [f"tpad=stop_mode=clone:stop_duration={_number(2 / float(rate))}",
                            f"fps={frame_rate_text}:start_time=0",
                            f"trim=end_frame={segment['frames']}", "setpts=PTS-STARTPTS",
                            "pad=ceil(iw/2)*2:ceil(ih/2)*2", "format=yuv420p"]
                _run([ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-ss", _number(segment["start"]), "-t", _number(source_length), "-i", str(source),
                      "-map", "0:v:0", "-an", "-vf", ",".join(filters),
                      "-frames:v", str(segment["frames"]), "-c:v", "libx264", "-crf", str(crf),
                      "-preset", preset, "-threads", str(threads), "-r", frame_rate_text,
                      "-video_track_timescale", str(rate.numerator), str(part)], log)
                parts.append(part.name)
                if has_audio:
                    piece = temporary / "piece.f32"
                    boundary_fade = min(fade_samples, segment["samples"] // 2) if audio_mode in {"smooth", "tail"} else 0
                    _audio_piece(source, piece, ffmpeg_exe, log, segment["start"], segment["end"],
                                 segment["speed"], segment["samples"], sample_rate, channels,
                                 boundary_fade if index else 0,
                                 boundary_fade if index < total - 1 else 0)
                    if audio_mode == "tail" and index and tail_ms:
                        previous = plan[index - 1]
                        gap = segment["start"] - previous["end"]
                        if gap > 1 / sample_rate:
                            camera_gap=bool(segment.get('camera_gap_before'))
                            tail_speed=segment['speed'] if camera_gap else previous['speed']
                            tail_duration = min(float(tail_ms) / 1000, gap / tail_speed,
                                                segment["samples"] / sample_rate)
                            tail_samples = round(tail_duration * sample_rate)
                            tail = temporary / "tail.f32"
                            if tail_samples:
                                # A camera seam carries sound just before the
                                # stable view resumes (e.g. skill activation),
                                # rather than the earlier selection-button click.
                                tail_start=(segment['start']-tail_duration*tail_speed) if camera_gap else previous['end']
                                _audio_piece(source, tail, ffmpeg_exe, log, tail_start,
                                             tail_start + tail_duration * tail_speed,
                                             tail_speed, tail_samples, sample_rate, channels,
                                             min(tail_samples // 2, fade_samples),
                                             min(tail_samples // 2, fade_samples))
                                _mix_tail(piece, tail, channels)
                                tails_mixed += 1
                                camera_tails_mixed += int(camera_gap)
                    with piece.open("rb") as pcm:
                        shutil.copyfileobj(pcm, audio_out, length=262144)
                if progress:
                    progress(index + 1, total, f"Rendered segment {index + 1}/{total}")
        finally:
            if audio_out is not None:
                audio_out.close()
        concat_file = temporary / "concat.txt"
        # MP4's container duration is rounded to milliseconds. Using it at
        # every join can eventually add whole frames. Override each duration
        # with its planned frame count; audio uses the same cumulative clock.
        concat_file.write_text(
            "ffconcat version 1.0\n" + "".join(
                f"file '{name}'\nduration {_number(segment['frames'] / float(rate))}\n"
                for name, segment in zip(parts, plan)), encoding="utf-8")
        rendered = temporary / "rendered.mp4"
        command = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "concat", "-safe", "1", "-i", str(concat_file)]
        if has_audio:
            command += ["-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels), "-i", str(assembled_audio),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-map", "0:v:0", "-an", "-c:v", "copy"]
        command += ["-t", _number(expected_duration), "-movflags", "+faststart", str(rendered)]
        _run(command, log)
        exported = _probe(rendered, ffprobe_exe)
        actual_video = next(s for s in exported["streams"] if s["codec_type"] == "video")
        actual_duration = float(actual_video.get("duration") or exported["format"]["duration"])
        tolerance = 0.001
        if abs(actual_duration - expected_duration) > tolerance:
            raise RenderError(f"Output duration mismatch: expected {expected_duration:.6f}s, got {actual_duration:.6f}s.")
        exported_audio = next((s for s in exported["streams"] if s["codec_type"] == "audio"), None)
        if has_audio and exported_audio is None:
            raise RenderError("The input had audio, but the output does not.")
        if exported_audio is not None:
            audio_duration = float(exported_audio.get("duration") or exported["format"]["duration"])
            if abs(audio_duration - expected_duration) > 0.05:
                raise RenderError(f"Audio duration mismatch: expected {expected_duration:.6f}s, got {audio_duration:.6f}s.")
        # The scratch directory may be on another drive; copy to a temporary
        # sibling first so the final replacement is atomic on the target drive.
        with tempfile.NamedTemporaryFile(prefix=".ark_export_", suffix=".mp4", dir=target.parent, delete=False) as stage:
            staged = Path(stage.name)
        try:
            shutil.copyfile(rendered, staged)
            if target.exists() and not overwrite:
                raise FileExistsError(target)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    return {"output_path": str(target), "expected_duration": expected_duration,
            "source_timeline_duration": source_expected_duration,
            "actual_duration": actual_duration, "fps": frame_rate_text,
            "segments_rendered": total, "has_audio": has_audio,
            "audio_mode": audio_mode if has_audio else "mute", "tails_mixed": tails_mixed,
            "camera_tails_mixed":camera_tails_mixed,
            "audio_channels": channels if has_audio else 0,
            "sample_rate": sample_rate if has_audio else None,"encoder_threads":threads}

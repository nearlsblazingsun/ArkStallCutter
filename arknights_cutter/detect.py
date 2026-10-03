"""Height-scaled UI templates; one FFmpeg decode, exact presentation timestamps."""
from __future__ import annotations

import csv
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

DEFAULTS = {
    "analysis_height": 360,
    "pause_roi": [0.25, 0.32, 0.50, 0.26],
    "speed_roi": [0.68, 0.0, 0.32, 0.18],
    "pause_threshold": 0.60,
    "speed_threshold": 0.80,
    "speed_margin": 0.055,
    "scale_min": 0.70,
    "scale_max": 1.30,
    "scale_step": 0.05,
    "panel_roi": [0.0, 0.32, 0.33, 0.15],
    "panel_threshold": 0.58,
    "bullet_ui_threshold": 0.70,
    "timestamp_mode": "auto",
}


def probe(path, ffprobe="ffprobe"):
    p = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, check=True)
    meta = json.loads(p.stdout)
    v = next((s for s in meta["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise ValueError("输入没有视频轨道")
    return meta, v


def frame_times(path, ffprobe="ffprobe", packet_mode=False):
    if packet_mode:
        p=subprocess.run([str(ffprobe),"-v","error","-select_streams","v:0","-show_packets",
                          "-show_entries","packet=pts_time,duration_time","-of","json",str(path)],
                         capture_output=True,check=True)
        packets=json.loads(p.stdout)["packets"]
        if packets and all("pts_time" in x for x in packets):
            packets.sort(key=lambda x:float(x["pts_time"]))
            pts=np.array([float(x["pts_time"]) for x in packets],np.float64)
            if np.all(np.diff(pts)>0):
                return pts,float(packets[-1].get("duration_time",0)),"packet PTS"
    p = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_frames",
                        "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(path)],
                       capture_output=True, check=True)
    frames = json.loads(p.stdout)["frames"]
    if not frames or any("best_effort_timestamp_time" not in f for f in frames):
        raise ValueError("视频缺少可用的逐帧时间戳")
    pts = np.array([float(f["best_effort_timestamp_time"]) for f in frames], np.float64)
    if np.any(np.diff(pts) <= 0):
        raise ValueError("视频时间戳不递增；请先用 FFmpeg 修复输入")
    last_duration = float(frames[-1].get("pkt_duration_time", 0))
    return pts, last_duration,"frame PTS"


def _feature(gray):
    gray = gray.astype(np.float32)
    # Remove low-frequency terrain without binarizing translucent text.
    return gray - cv2.GaussianBlur(gray, (0, 0), 1.4)


def _pause_feature(gray):
    # GIF dithering / compression must not dominate broad PAUSE strokes.
    gray=cv2.GaussianBlur(gray.astype(np.float32),(0,0),0.7)
    return gray-cv2.GaussianBlur(gray,(0,0),3.0)


def _rect(frac, w, h):
    if len(frac) != 4 or not all(math.isfinite(float(x)) for x in frac):
        raise ValueError("ROI 必须是四个有限小数 [x,y,width,height]")
    x, y, rw, rh = frac
    if x < 0 or y < 0 or rw <= 0 or rh <= 0 or x+rw > 1.00001 or y+rh > 1.00001:
        raise ValueError("ROI 必须位于画面内，使用 0~1 的相对坐标")
    return int(x*w), int(y*h), max(1, int(rw*w)), max(1, int(rh*h))


class Detector:
    def __init__(self, width, height, config=None, templates=None):
        self.config = {**DEFAULTS, **(config or {})}
        c = self.config
        self.height = int(c["analysis_height"])
        if not 180 <= self.height <= 1080:
            raise ValueError("analysis_height 必须在 180~1080 之间")
        for key in ("pause_threshold", "speed_threshold", "speed_margin", "panel_threshold", "bullet_ui_threshold"):
            if not 0 <= float(c[key]) <= 1:
                raise ValueError(f"{key} 必须在 0~1 之间")
        if not 0.3 <= c["scale_min"] <= c["scale_max"] <= 3 or c["scale_step"] <= 0:
            raise ValueError("模板缩放范围无效")
        self.width = round(width*self.height/height)
        self.pause_rect = _rect(c["pause_roi"], self.width, self.height)
        self.speed_rect = _rect(c["speed_roi"], self.width, self.height)
        self.panel_rect = _rect(c["panel_roi"], self.width, self.height)
        self.tile_width = max(self.pause_rect[2], self.speed_rect[2],self.panel_rect[2])
        self.tile_height = self.pause_rect[3] + self.speed_rect[3]+self.panel_rect[3]
        root = Path(templates or Path(__file__).parent/"templates")
        self.bank = {k: [] for k in ("pause", "one", "two", "panel", "arrows", "bars")}
        specs = [("pause", "pause.png", 852), ("pause", "pause_standard.png", 2157),
                 ("one", "one.png", 852), ("two", "two.png", 852),
                 ("panel", "panel.png",852),("panel","panel_range.png",852),
                 ("arrows","arrows.png",852),("bars","bars.png",852)]
        scales = np.arange(c["scale_min"], c["scale_max"]+c["scale_step"]/2, c["scale_step"])
        for kind, name, reference_height in specs:
            data = cv2.imdecode(np.fromfile(root/name, np.uint8), cv2.IMREAD_GRAYSCALE)
            if data is None:
                raise ValueError(f"无法读取模板 {root/name}")
            seen = set()
            for scale in scales:
                size = tuple(max(3, round(x*self.height/reference_height*scale)) for x in data.shape[::-1])
                if size in seen:
                    continue
                seen.add(size)
                resized = cv2.resize(data, size, interpolation=cv2.INTER_AREA)
                feature_fn=_pause_feature if kind=="pause" else _feature
                feature=feature_fn(resized)
                self.bank[kind].append({"feature": feature,"std":float(np.std(feature)),"scale":float(scale),"name":name})
        self.pause_lock = None
        self.speed_lock = None
        self.panel_lock = None
        self.arrows_lock = None
        self.bars_lock = None
        self.calls = 0

    def filter_graph(self):
        px,py,pw,ph = self.pause_rect
        sx,sy,sw,sh = self.speed_rect
        lx,ly,lw,lh = self.panel_rect
        return (f"[0:v:0]scale={self.width}:{self.height}:flags=bilinear,format=gray,split=3[p][s][l];"
                f"[p]crop={pw}:{ph}:{px}:{py},pad={self.tile_width}:{ph}:0:0[pc];"
                f"[s]crop={sw}:{sh}:{sx}:{sy},pad={self.tile_width}:{sh}:0:0[sc];"
                f"[l]crop={lw}:{lh}:{lx}:{ly},pad={self.tile_width}:{lh}:0:0[lc];[pc][sc][lc]vstack=inputs=3[out]")

    @staticmethod
    def _match(feature, templates, lock=None):
        best = (-1.0, None)
        if float(np.std(feature)) < 0.05:
            return best
        for i, entry in enumerate(templates):
            if lock is not None and i != lock["index"]:
                continue
            tpl = entry["feature"]
            th, tw = tpl.shape
            if entry.get('std',1.0) < 0.5:
                continue
            if lock is not None:
                x, y = lock["xy"]
                x0, y0 = max(0, x-3), max(0, y-3)
                x1, y1 = min(feature.shape[1], x+tw+3), min(feature.shape[0], y+th+3)
                area = feature[y0:y1, x0:x1]
            else:
                area, x0, y0 = feature, 0, 0
            if area.shape[0] < th or area.shape[1] < tw:
                continue
            corr = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, xy = cv2.minMaxLoc(corr)
            patch=area[xy[1]:xy[1]+th,xy[0]:xy[0]+tw]
            if float(np.std(patch)) < 0.5:
                # Near-zero local variance can produce a spurious perfect NCC.
                # Build a variance mask only on this rare numerical edge case.
                sums,squares=cv2.integral2(area,sdepth=cv2.CV_64F,sqdepth=cv2.CV_64F)
                total=sums[th:,tw:]-sums[:-th,tw:]-sums[th:,:-tw]+sums[:-th,:-tw]
                total2=squares[th:,tw:]-squares[:-th,tw:]-squares[th:,:-tw]+squares[:-th,:-tw]
                variance=total2/(th*tw)-(total/(th*tw))**2
                corr[variance < 0.25]=-1.0
                _,score,_,xy=cv2.minMaxLoc(corr)
            if score > best[0]:
                best = (score, {"index": i, "xy": (xy[0]+x0, xy[1]+y0), "scale": entry["scale"]})
        return best

    def classify_tile(self, tile, timestamp=0.0):
        ph = self.pause_rect[3]
        sh = self.speed_rect[3]
        p = _pause_feature(tile[:ph, :self.pause_rect[2]])
        s = _feature(tile[ph:ph+sh, :self.speed_rect[2]])
        pc, pl = self._match(p, self.bank["pause"], self.pause_lock)
        if self.pause_lock is None and pc >= self.config["pause_threshold"]:
            self.pause_lock = pl
        # Locked placement is fast. Periodic broad search reacquires moved UI.
        if self.pause_lock and pc < self.config["pause_threshold"] and self.calls % 90 == 0:
            newpc, newpl = self._match(p, self.bank["pause"])
            if newpc > pc:
                pc, pl = newpc, newpl
                if pc >= self.config["pause_threshold"]:
                    self.pause_lock = pl
        one, ol = self._match(s, self.bank["one"], self.speed_lock)
        two, tl = self._match(s, self.bank["two"], self.speed_lock)
        if self.speed_lock is None and max(one, two) >= self.config["speed_threshold"]:
            self.speed_lock = ol if one > two else tl
        if self.speed_lock and max(one, two) < self.config["speed_threshold"] and self.calls % 30 == 0:
            one, ol = self._match(s, self.bank["one"])
            two, tl = self._match(s, self.bank["two"])
            if max(one, two) >= self.config["speed_threshold"]:
                self.speed_lock = ol if one > two else tl
        self.calls += 1
        if pc >= self.config["pause_threshold"]:
            state = "pause"
        elif one >= self.config["speed_threshold"] and one-two >= self.config["speed_margin"]:
            state = "one"
        elif two >= self.config["speed_threshold"] and two-one >= self.config["speed_margin"]:
            state = "two"
        else:
            state = "unknown"
        panel,arrows,bars=-1.0,-1.0,-1.0
        if state == "unknown":
            bars,lb=self._match(s,self.bank["bars"],self.bars_lock)
            if bars<self.config["bullet_ui_threshold"] and self.bars_lock and self.calls%30==0:
                bars,lb=self._match(s,self.bank["bars"])
            # Most non-battle frames have no live pause-bars; skip panel searches.
            if bars>=self.config["bullet_ui_threshold"]:
                self.bars_lock=lb
                left=_feature(tile[ph+sh:,:self.panel_rect[2]])
                panel,lp=self._match(left,self.bank["panel"],self.panel_lock)
                if panel < self.config["panel_threshold"] and self.panel_lock:
                    panel,lp=self._match(left,self.bank["panel"])
                if panel>=self.config["panel_threshold"]:
                    state="bullet"
                    self.panel_lock=lp
                    arrows,la=self._match(s,self.bank["arrows"],self.arrows_lock)
                    if arrows>=self.config["bullet_ui_threshold"]:
                        self.arrows_lock=la
        return {"state": state, "pause": round(pc, 5), "one": round(one, 5), "two": round(two, 5),
                "panel":round(panel,5),"arrows":round(arrows,5),"bars":round(bars,5)}

    def classify_image(self, image):
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        image = cv2.resize(image, (self.width,self.height), interpolation=cv2.INTER_LINEAR)
        pieces=[]
        for rect in (self.pause_rect, self.speed_rect,self.panel_rect):
            x,y,w,h=rect
            piece=np.zeros((h,self.tile_width),np.uint8)
            piece[:,:w]=image[y:y+h,x:x+w]
            pieces.append(piece)
        return self.classify_tile(np.vstack(pieces))


def runs_from_frames(times, states, duration):
    if not states:
        raise ValueError("输入没有可解码帧")
    runs=[]
    start=0
    for i in range(1,len(states)+1):
        if i == len(states) or states[i] != states[start]:
            end=float(times[i]) if i<len(states) else duration
            runs.append({"start":float(times[start]),"end":float(end),"state":states[start]})
            start=i
    # Actual editing action merges two/unknown transitions: both keep original speed.
    segments=[]
    for r in runs:
        if r["state"] == "pause":
            continue
        speed={"one":2.0,"bullet":10.0}.get(r["state"],1.0)
        if segments and abs(segments[-1]["end"]-r["start"])<1e-7 and segments[-1]["speed"]==speed:
            segments[-1]["end"]=r["end"]
        else:
            segments.append({"start":r["start"],"end":r["end"],"speed":speed})
    return runs,segments


def analyze(path, *, ffmpeg="ffmpeg", ffprobe="ffprobe", config=None, frames_csv=None, progress=None):
    cv2.setNumThreads(1)
    started=time.perf_counter()
    meta,v=probe(path,ffprobe)
    probe_seconds=time.perf_counter()-started
    cfg={**DEFAULTS,**(config or {})}
    if cfg["timestamp_mode"] not in ("auto","frames"):
        raise ValueError("timestamp_mode 必须为 auto 或 frames")
    # H.264/HEVC in common recording containers have one access unit per packet.
    # Packet PTS avoids a second full video decode. Count validation remains mandatory.
    formats=meta.get("format",{}).get("format_name","").split(',')
    fast_pts=cfg["timestamp_mode"]=="auto" and v.get("codec_name") in ("h264","hevc") and any(f in formats for f in ('mov','mp4','matroska','webm'))
    times,last_duration,pts_mode=frame_times(path,ffprobe,fast_pts)
    timestamps_seconds=time.perf_counter()-started-probe_seconds
    origin=float(meta.get("format",{}).get("start_time",times[0]))
    times=times-origin
    video_start=float(v.get("start_time",origin))-origin
    duration=max(video_start+float(v.get("duration",0)),float(times[-1])+last_duration)
    if duration <= times[-1]:
        duration=float(times[-1])+float(np.median(np.diff(times)))
    input_width,input_height=v["width"],v["height"]
    rotation=float(v.get('tags',{}).get('rotate',0))
    for side in v.get('side_data_list',[]):
        if 'rotation' in side:
            rotation=float(side['rotation'])
    if round(rotation)%180==90:
        input_width,input_height=input_height,input_width
    det=Detector(input_width,input_height,config)
    states=[]
    csv_file=None
    if frames_csv:
        Path(frames_csv).parent.mkdir(parents=True,exist_ok=True)
        csv_file=open(frames_csv,"w",newline="",encoding="utf-8-sig")
        writer=csv.writer(csv_file)
        writer.writerow(["frame","timestamp","state","pause_score","one_score","two_score","panel_score","arrows_score","bars_score"])
    with tempfile.TemporaryFile() as err:
        command=[str(ffmpeg),"-hide_banner","-loglevel","error","-i",str(path),
                 "-filter_complex",det.filter_graph(),"-map","[out]","-an","-vsync","0",
                 "-pix_fmt","gray","-f","rawvideo","pipe:1"]
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=err,bufsize=det.tile_width*det.tile_height*3)
        count=0
        size=det.tile_width*det.tile_height
        try:
            while True:
                buffer=process.stdout.read(size)
                if not buffer:
                    break
                if len(buffer)!=size or count >=len(times):
                    raise RuntimeError("解码帧与时间戳数量不一致")
                tile=np.frombuffer(buffer,np.uint8).reshape(det.tile_height,det.tile_width)
                result=det.classify_tile(tile,float(times[count]))
                states.append(result["state"])
                if csv_file:
                    writer.writerow([count,f"{times[count]:.9f}",result["state"],result["pause"],result["one"],result["two"],result["panel"],result["arrows"],result["bars"]])
                count+=1
                if progress and count%240==0:
                    progress(count,len(times),f"识别 {times[count-1]:.1f}/{duration:.1f} 秒")
            code=process.wait()
            err.seek(0)
            if code:
                raise RuntimeError(err.read().decode("utf-8",errors="replace"))
            if count !=len(times):
                raise RuntimeError(f"解码帧 {count} 与时间戳 {len(times)} 不一致；停止导出以防错切")
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait()
            if csv_file:
                csv_file.close()
    runs,segments=runs_from_frames(times,states,duration)
    lengths={k:sum(r["end"]-r["start"] for r in runs if r["state"]==k) for k in ("pause","one","two","bullet","unknown")}
    return {
        "schema":1,"input":str(Path(path).resolve()),"width":input_width,"height":input_height,
        "duration":duration,"input_start_pts":origin,"frame_count":count,"timestamp_mode":pts_mode,
        "nominal_fps":v.get("r_frame_rate"),"avg_fps":v.get("avg_frame_rate"),
        "config":det.config,"pause_anchor":det.pause_lock,"speed_anchor":det.speed_lock,
        "state_seconds":lengths,"output_duration":sum((s["end"]-s["start"])/s["speed"] for s in segments),
        "analysis_seconds":time.perf_counter()-started,"runs":runs,"segments":segments,
        "probe_seconds":probe_seconds,"timestamps_seconds":timestamps_seconds,
        "notes":["PAUSE 优先；1X×2，子弹时间×10，未知状态保留原速。", "识别基于可见 UI；严重遮挡、改版 UI 或极低画质需要校准。"],
    }

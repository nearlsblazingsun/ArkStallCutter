"""GUI preferences, safe CLI jobs and background events (standard library only).

The GUI owns presentation. This module never calls Tk and never starts a shell.
The existing CLI remains responsible for detection, editing and encoding.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading


class SettingsError(ValueError):
    """A configuration value cannot be used by the existing CLI."""


@dataclass(frozen=True)
class Settings:
    audio: str = "tail"
    tail_ms: float = 120
    fade_ms: float = 8
    fps: str = "source"
    crf: int = 18
    preset: str = "fast"
    threads: str | int = "auto"
    camera: str = "auto"
    camera_flash_ms: float = 1000
    camera_recovery_ms: float = 1000
    camera_settle_frames: int = 2
    reuse_analysis: bool = True
    analyze_only: bool = False
    ffmpeg: str = "auto"
    ffprobe: str = "auto"
    work_dir: str = ""
    detection_config: str = ""

    def validate(self) -> Settings:
        """Return normalized settings; reject values outside supported ranges."""
        result = asdict(self)
        choices = {
            "audio": ("cut", "smooth", "tail", "mute"),
            "fps": ("source", "30", "60"),
            "preset": ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
            "camera": ("auto", "strict", "off"),
        }
        labels = {"audio": "音频模式", "fps": "输出帧率", "preset": "编码速度", "camera": "镜头处理"}
        for key, allowed in choices.items():
            value = str(result[key]).strip()
            if value not in allowed:
                raise SettingsError(f"{labels[key]}无效：{value}")
            result[key] = value
        ranges = {
            "tail_ms": (0, 1000, "尾音长度"),
            "fade_ms": (0, 100, "切点淡化"),
            "camera_flash_ms": (0, 5000, "短操作时长上限"),
            "camera_recovery_ms": (0, 2000, "镜头恢复搜索范围"),
        }
        for key, (minimum, maximum, label) in ranges.items():
            try:
                if isinstance(result[key], bool):
                    raise ValueError
                value = float(result[key])
            except (TypeError, ValueError):
                raise SettingsError(f"{label}需要填写数字。") from None
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise SettingsError(f"{label}必须在 {minimum}～{maximum} 毫秒之间。")
            result[key] = value
        result["crf"] = _integer(result["crf"], 0, 51, "画质 CRF")
        result["camera_settle_frames"] = _integer(result["camera_settle_frames"], 0, 2, "恢复后额外省略帧数")
        if str(result["threads"]).strip() == "auto":
            result["threads"] = "auto"
        else:
            result["threads"] = _integer(result["threads"], 1, 64, "编码线程数")
        for key in ("reuse_analysis", "analyze_only"):
            if not isinstance(result[key], bool):
                raise SettingsError(f"{key} 必须是布尔值。")
        for key in ("ffmpeg", "ffprobe", "work_dir", "detection_config"):
            if not isinstance(result[key], str):
                raise SettingsError(f"{key} 必须是文本路径。")
            result[key] = result[key].strip()
        return Settings(**result)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        if not isinstance(data, dict):
            raise SettingsError("设置文件需要包含 JSON 对象。")
        # Ignore future fields and input/output history from unrelated tools.
        names = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names}).validate()


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        numeric = float(value)
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError
        number = int(numeric)
    except (ValueError, TypeError, OverflowError):
        raise SettingsError(f"{label}需要填写整数。") from None
    if not minimum <= number <= maximum:
        raise SettingsError(f"{label}必须在 {minimum}～{maximum} 之间。")
    return number


# Each preset changes encoding choices; the user's audio/camera choices survive.
PRESETS = {
    "balanced": {"preset": "fast", "crf": 18, "fps": "source", "threads": "auto"},
    "fast": {"preset": "veryfast", "crf": 20, "fps": "30", "threads": "auto"},
    "quality": {"preset": "fast", "crf": 16, "fps": "source", "threads": "auto"},
}


def preset_settings(name: str, base: Settings | None = None) -> Settings:
    if name not in PRESETS:
        raise SettingsError(f"未知预设：{name}")
    return replace(base or Settings(), **PRESETS[name]).validate()


class SettingsStore:
    """Save only configuration, without keeping video paths or job history."""

    def __init__(self, path: str | os.PathLike | None = None):
        if path is None:
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) if local else Path.home() / ".local" / "share"
            path = base / "ArknightsCutter" / "gui-settings.json"
        self.path = Path(path)

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return Settings.from_dict(data.get("settings", data) if isinstance(data, dict) else data)
        except (OSError, json.JSONDecodeError, SettingsError) as exc:
            raise SettingsError(f"无法读取已保存的设置：{exc}") from exc

    def save(self, settings: Settings) -> None:
        settings = settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "settings": asdict(settings)}, ensure_ascii=False, indent=2)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".gui-settings-", suffix=".json", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


def _same_path(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve())):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def resolve_tool(value: str, name: str, package_dir: Path) -> str:
    """Find a bundled bin executable first, then PATH; explicit paths win."""
    value = value.strip()
    if not value or value == "auto":
        for candidate in (package_dir / "bin" / f"{name}.exe", package_dir / "bin" / name):
            if candidate.is_file():
                return str(candidate.resolve())
        found = shutil.which(name)
    else:
        candidate = Path(value).expanduser()
        found = str(candidate.resolve()) if candidate.is_file() else shutil.which(value)
    if not found:
        raise SettingsError(f"找不到 {name}，请在工具路径中选择它，或将 FFmpeg 放入程序的 bin 文件夹。")
    return str(Path(found).resolve())


@dataclass(frozen=True)
class JobSpec:
    argv: tuple[str, ...]
    cwd: Path
    input_path: Path
    output_path: Path
    report_path: Path
    analyze_only: bool


def build_job(input_path: str | os.PathLike, output_path: str | os.PathLike,
              settings: Settings, package_dir: str | os.PathLike,
              python_executable: str | os.PathLike | None = None,
              allow_overwrite: bool = False) -> JobSpec:
    settings = settings.validate()
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    package = Path(package_dir).resolve()
    if not source.is_file():
        raise SettingsError(f"输入视频不存在：{source}")
    if output.suffix.lower() != ".mp4":
        raise SettingsError("输出文件需要使用 .mp4 扩展名。")
    report = output.with_suffix(".report.json")
    if _same_path(source, output) or _same_path(source, report):
        raise SettingsError("输出视频和报告均不能覆盖输入文件。")
    if output.exists() and not output.is_file():
        raise SettingsError("输出路径指向文件夹，请选择一个 MP4 文件名。")
    if report.exists() and not report.is_file():
        raise SettingsError("报告路径指向文件夹，请修改输出文件名。")
    if not allow_overwrite and ((not settings.analyze_only and output.exists()) or report.exists()):
        raise FileExistsError("输出视频或剪辑报告已存在，需要确认覆盖。")
    if not (package / "arkcut.py").is_file():
        raise SettingsError(f"找不到剪辑脚本：{package / 'arkcut.py'}")
    python = Path(python_executable or sys.executable).resolve()
    if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").is_file():
        # The GUI may use pythonw. Its worker needs a dependable stdout pipe.
        python = python.with_name("python.exe")
    if not python.is_file():
        raise SettingsError("找不到 Python 运行程序。")
    ffmpeg = resolve_tool(settings.ffmpeg, "ffmpeg", package)
    ffprobe = resolve_tool(settings.ffprobe, "ffprobe", package)
    if settings.detection_config:
        config = Path(settings.detection_config).expanduser().resolve()
        if not config.is_file():
            raise SettingsError(f"检测配置文件不存在：{config}")
        if _same_path(config, output) or _same_path(config, report):
            raise SettingsError("检测配置文件不能与输出视频或报告使用同一路径。")
        try:
            config_data = json.loads(config.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"检测配置不是有效 JSON：{exc}") from exc
        if not isinstance(config_data, dict):
            raise SettingsError("检测配置需要是 JSON 对象。")
    else:
        config = None
    if settings.work_dir:
        work = Path(settings.work_dir).expanduser().resolve()
        if work.exists() and not work.is_dir():
            raise SettingsError("临时工作目录指向文件，请选择文件夹。")
    else:
        work = None
    argv = [str(python), "-u", str(package / "arkcut.py"), str(source), "--output", str(output),
            "--report", str(report), "--audio", settings.audio, "--tail-ms", str(settings.tail_ms),
            "--fade-ms", str(settings.fade_ms), "--crf", str(settings.crf), "--preset", settings.preset,
            "--camera", settings.camera, "--camera-flash-ms", str(settings.camera_flash_ms),
            "--camera-recovery-ms", str(settings.camera_recovery_ms),
            "--camera-settle-frames", str(settings.camera_settle_frames),
            "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]
    if settings.fps != "source":
        argv += ["--fps", settings.fps]
    if settings.threads != "auto":
        argv += ["--threads", str(settings.threads)]
    if settings.reuse_analysis:
        argv += ["--reuse-analysis"]
    if settings.analyze_only:
        argv += ["--analyze-only"]
    if allow_overwrite:
        argv += ["--overwrite"]
    if config:
        argv += ["--config", str(config)]
    if work:
        argv += ["--work-dir", str(work)]
    return JobSpec(tuple(argv), package, source, output, report, settings.analyze_only)


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    label: str
    percent: float | None = None


_PERCENT = re.compile(r"^\[\s*(\d+(?:\.\d+)?)%\]\s*(.*)")


def parse_progress(line: str) -> ProgressUpdate | None:
    line = line.strip()
    match = _PERCENT.match(line)
    percent = min(100.0, max(0.0, float(match.group(1)))) if match else None
    message = match.group(2) if match else line
    if "Rendered segment" in message or message.startswith("正在编码"):
        return ProgressUpdate("render", "编码视频与处理音效", percent)
    if "Checked camera flash" in message or message.startswith("正在检查短暂"):
        return ProgressUpdate("camera", "优化选中镜头与切点", percent)
    if message.startswith("正在读取逐帧") or message.startswith("识别 "):
        return ProgressUpdate("detect", "识别暂停与游戏速度", percent)
    if message.startswith("复用已完成") or message.startswith("使用指定"):
        return ProgressUpdate("detect", "已复用识别结果", 100.0)
    if message.startswith("剪辑区间 ") or message.startswith("暂停删除 "):
        return ProgressUpdate("plan", "剪辑方案已生成", None)
    if message.startswith("完成："):
        return ProgressUpdate("complete", "处理完成", 100.0)
    return None


@dataclass(frozen=True)
class JobEvent:
    kind: str
    message: str = ""
    progress: ProgressUpdate | None = None
    returncode: int | None = None
    cancelled: bool = False
    success: bool = False
    error: str = ""
    output_path: Path | None = None
    report_path: Path | None = None


def _signature(path: Path):
    if not path.is_file():
        return None
    stat = path.stat()
    # Reports are small. Video freshness uses its replacement time and size.
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.suffix == ".json" else None
    return stat.st_mtime_ns, stat.st_size, digest


def _validate_result(spec: JobSpec, old_report, old_output) -> None:
    if not spec.report_path.is_file() or spec.report_path.stat().st_size == 0:
        raise RuntimeError("程序退出了，但未生成剪辑报告。")
    if _signature(spec.report_path) == old_report:
        raise RuntimeError("程序退出了，但剪辑报告没有更新。")
    try:
        report = json.loads(spec.report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"生成的剪辑报告无法读取：{exc}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("segments"), list):
        raise RuntimeError("生成的报告缺少剪辑区间。")
    fingerprint = report.get("input_fingerprint")
    if not isinstance(fingerprint, dict) or not fingerprint.get("path"):
        raise RuntimeError("生成的报告没有输入文件记录。")
    if not _same_path(Path(fingerprint["path"]), spec.input_path):
        raise RuntimeError("生成的报告对应另一个输入文件。")
    stat = spec.input_path.stat()
    if fingerprint.get("bytes") != stat.st_size or fingerprint.get("mtime_ns") != stat.st_mtime_ns:
        raise RuntimeError("处理期间输入文件发生变化，请重新运行。")
    if spec.analyze_only:
        return
    if not spec.output_path.is_file() or spec.output_path.stat().st_size == 0:
        raise RuntimeError("程序退出了，但未生成 MP4 成片。")
    if _signature(spec.output_path) == old_output:
        raise RuntimeError("程序退出了，但 MP4 成片没有更新。")
    rendered = report.get("render")
    if not isinstance(rendered, dict) or not rendered.get("output_path"):
        raise RuntimeError("报告中没有成功导出的成片记录。")
    if not _same_path(Path(rendered["output_path"]), spec.output_path):
        raise RuntimeError("报告中的成片路径与当前任务不符。")


class _ProcessTree:
    """Terminate only this job's descendants; never match executable names."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.job = None
        self.kernel = None
        if os.name == "nt":
            self._attach_windows_job()

    def _attach_windows_job(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcesses", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("Io", IoCounters),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                and kernel.AssignProcessToJobObject(job, int(self.process._handle))):
            self.job, self.kernel = job, kernel
        else:
            kernel.CloseHandle(job)

    def terminate(self):
        if os.name == "nt":
            if self.job and self.kernel.TerminateJobObject(self.job, 130):
                return
            if self.process.poll() is None:
                # PID targets one still-running launcher and its descendants.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                               timeout=15, check=False)
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None


class JobRunner:
    """Run one CLI job in the background. Tk polls ``events`` using ``after``.

    ``cancel`` is nonblocking and kills this process tree. ``finished`` is sent
    only after the subprocess has ended and output/report validation has run.
    """

    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.events: queue.Queue[JobEvent] = queue.Queue(maxsize=1000)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._process = None
        self._tree = None
        self._stop_thread = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("同一任务只能启动一次。")
            self._thread = threading.Thread(target=self._run, name="arkcut-job", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._stop_thread is None:
                self._stop_thread = threading.Thread(target=self._stop, name="arkcut-cancel", daemon=True)
                self._stop_thread.start()

    def _stop(self):
        with self._lock:
            tree = self._tree
        if tree:
            try:
                tree.terminate()
            except (OSError, subprocess.TimeoutExpired):
                if self._process and self._process.poll() is None:
                    self._process.kill()

    def _emit(self, event: JobEvent):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def _run(self):
        returncode = None
        error = ""
        success = False
        try:
            old_report, old_output = _signature(self.spec.report_path), _signature(self.spec.output_path)
            if self._cancel.is_set():
                return
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(list(self.spec.argv), cwd=self.spec.cwd, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                       errors="replace", bufsize=1, shell=False, **options)
            tree = _ProcessTree(process)
            with self._lock:
                self._process, self._tree = process, tree
            if self._cancel.is_set():
                tree.terminate()
            for line in process.stdout:
                message = line.rstrip("\r\n")
                self._emit(JobEvent("log", message=message))
                progress = parse_progress(message)
                if progress:
                    self._emit(JobEvent("progress", message=message, progress=progress))
            returncode = process.wait()
            if not self._cancel.is_set():
                if returncode:
                    error = f"剪辑程序退出，错误码 {returncode}。请查看日志中的原因。"
                else:
                    _validate_result(self.spec, old_report, old_output)
                    success = True
        except Exception as exc:
            error = str(exc)
            if self._process and self._process.poll() is None:
                if self._tree:
                    self._tree.terminate()
                else:
                    self._process.kill()
                returncode = self._process.wait()
        finally:
            with self._lock:
                if self._tree:
                    self._tree.close()
                    self._tree = None
            if self._process and self._process.stdout:
                self._process.stdout.close()
            cancelled = self._cancel.is_set()
            self._emit(JobEvent("finished", returncode=returncode, cancelled=cancelled,
                                success=success and not cancelled, error="" if cancelled else error,
                                output_path=self.spec.output_path if success and not cancelled and not self.spec.analyze_only else None,
                                report_path=self.spec.report_path if success and not cancelled else None))

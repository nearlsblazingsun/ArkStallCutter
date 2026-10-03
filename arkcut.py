#!/usr/bin/env python3
"""PAUSE removal, 1X ×2, live operator selection ×10; see README.md."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time


def fingerprint(path, config):
    stat=path.stat()
    code=hashlib.sha256()
    package=Path(__file__).parent/'arknights_cutter'
    for p in sorted([package/'detect.py',*(package/'templates').glob('*.png')]):
        code.update(p.read_bytes())
    return {"path":str(path.resolve()),"bytes":stat.st_size,"mtime_ns":stat.st_mtime_ns,
            "config":config,"detector_sha256":code.hexdigest()}


def main():
    parser=argparse.ArgumentParser(description="明日方舟自动剪辑：删除 PAUSE，1X×2，子弹时间×10。")
    parser.add_argument('input',nargs='?',help='输入视频；省略时打开文件选择窗口')
    parser.add_argument('-o','--output',help='输出 MP4；默认输入目录下的 .edited.mp4')
    parser.add_argument('--analyze-only',action='store_true',help='只生成剪辑报告，不编码')
    parser.add_argument('--reuse-analysis',action='store_true',help='输入/配置/模板相同则复用已有报告')
    parser.add_argument('--plan',help='使用已审核/手动修改的报告中 segments，跳过识别')
    parser.add_argument('--report',help='报告路径；默认输出名.report.json')
    parser.add_argument('--frames-csv',help='可选逐帧时间戳与置信度 CSV 路径')
    parser.add_argument('--config',help='自定义检测参数 JSON，参见 config.example.json')
    parser.add_argument('--audio',choices=['cut','smooth','tail','mute'],default='tail',help='音频模式，默认 tail 尝试保留暂停开始后的短尾音')
    parser.add_argument('--tail-ms',type=float,default=120,help='tail 模式保留尾音长度，毫秒')
    parser.add_argument('--fade-ms',type=float,default=8,help='切点淡化长度，毫秒')
    parser.add_argument('--fps',help='输出恒定帧率，默认源视频标称帧率；可设 30/60')
    parser.add_argument('--crf',type=int,default=18,help='画质；数值越小越清晰，默认18')
    parser.add_argument('--preset',choices=['ultrafast','superfast','veryfast','faster','fast','medium','slow'],default='fast')
    parser.add_argument('--threads',type=int,help='编码线程数，默认自动使用至多8线程')
    parser.add_argument('--camera',choices=['auto','strict','off'],default='auto',help='auto 优先连贯、清理确认选中且恢复的短操作；strict 还要求捕获镜头移动；off 保留选中镜头')
    parser.add_argument('--camera-flash-ms',type=float,default=1000,help='候选操作加速后的保留时长上限，默认1000毫秒，最大5000；可能省略少量战斗画面')
    parser.add_argument('--camera-recovery-ms',type=float,default=1000,help='最多检查多少源视频毫秒的镜头恢复，默认1000，最大2000；按实际稳定点结束')
    parser.add_argument('--camera-settle-frames',type=int,choices=[0,1,2],default=2,help='恢复后额外清理的输出帧数上限；默认2，自适应省略1～2帧，0关闭')
    parser.add_argument('--ffmpeg',default='ffmpeg',help='FFmpeg 可执行文件路径')
    parser.add_argument('--ffprobe',default='ffprobe',help='FFprobe 可执行文件路径')
    parser.add_argument('--work-dir',help='中间文件所在目录，需有足够剩余空间')
    parser.add_argument('--overwrite',action='store_true',help='允许覆盖已有输出')
    args=parser.parse_args()
    if args.input is None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root=tk.Tk();root.withdraw()
            args.input=filedialog.askopenfilename(title='选择明日方舟录屏',filetypes=[('视频','*.mp4 *.mkv *.mov *.webm *.gif'),('所有文件','*.*')])
            root.destroy()
        except Exception:
            parser.error('请指定输入视频路径')
        if not args.input:
            return 0
    source=Path(args.input).resolve()
    if not source.is_file():
        parser.error(f'输入不存在：{source}')
    output=Path(args.output).resolve() if args.output else source.with_name(source.stem+'.edited.mp4')
    if source==output:
        parser.error('输出不能覆盖输入视频')
    report=Path(args.report).resolve() if args.report else output.with_suffix('.report.json')
    if report==source or report==output:
        parser.error('报告路径不能与输入或输出相同')
    if args.frames_csv and Path(args.frames_csv).resolve() in [source,output,report]:
        parser.error('CSV 路径不能与输入、输出或报告相同')
    if output.exists() and not args.overwrite and not args.analyze_only:
        parser.error(f'输出已存在：{output}。需要覆盖请添加 --overwrite')
    for name in ['ffmpeg','ffprobe']:
        value=getattr(args,name)
        found=shutil.which(value) or (str(Path(value).resolve()) if Path(value).is_file() else None)
        if not found:
            parser.error(f'找不到 {name}，请将 FFmpeg 放入 PATH，或使用 --{name} 指定路径')
        setattr(args,name,found)
    try:
        from arknights_cutter.detect import analyze
        from arknights_cutter.render import export_video
    except ModuleNotFoundError as exc:
        parser.error(f'缺少依赖 {exc.name}；请运行 python -m pip install -r requirements.txt，或使用 启动剪辑.cmd')
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    sig=fingerprint(source,config)
    last_progress=0
    def progress(done,total,message):
        nonlocal last_progress
        now=time.monotonic()
        if now-last_progress>2 or done==total:
            print(f'[{done/total:6.1%}] {message}',flush=True)
            last_progress=now
    started=time.perf_counter()
    data=None
    if args.plan:
        data=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'))
        expected=data.get('input_fingerprint')
        if expected and any(expected.get(k)!=sig[k] for k in ['path','bytes','mtime_ns']):
            parser.error('剪辑报告对应的输入视频已变化，不能安全复用')
        if not isinstance(data.get('segments'),list):
            parser.error('报告缺少 segments 剪辑区间')
        print('使用指定的剪辑区间。',flush=True)
    elif args.reuse_analysis and report.is_file():
        cached=json.loads(report.read_text(encoding='utf-8-sig'))
        if cached.get('input_fingerprint')==sig:
            data=cached
            print('复用已完成的识别结果。',flush=True)
    if data is None:
        print('正在读取逐帧时间戳并识别 UI……',flush=True)
        data=analyze(source,ffmpeg=args.ffmpeg,ffprobe=args.ffprobe,config=config,frames_csv=args.frames_csv,progress=progress)
    # Keep source UI analysis reusable when only camera/export settings change.
    base_segments=data['segments'] if args.plan else data.get('raw_segments',data['segments'])
    data['raw_segments']=[dict(s) for s in base_segments]
    for stale in ['render','validation','camera_preview_validation','final_camera_validation','camera_cut_validation','total_processing_seconds','render_seconds','analysis_and_render_seconds','camera_and_render_seconds']:
        data.pop(stale,None)
    if args.camera!='off':
        from arknights_cutter.camera import optimize_camera
        print('正在检查短暂选中镜头及其恢复过程……',flush=True)
        raw={**data,'segments':data['raw_segments']}
        data['segments'],data['camera']=optimize_camera(source,raw,ffmpeg=args.ffmpeg,
            flash_ms=args.camera_flash_ms,recovery_ms=args.camera_recovery_ms,
            settle_frames=args.camera_settle_frames,output_fps=args.fps,
            policy='flow' if args.camera=='auto' else 'strict',progress=progress)
        print(f"已清理 {data['camera']['removed_events']} 处镜头闪跳，缩短 {data['camera']['output_seconds_removed']:.3f}s。",flush=True)
    else:
        data['segments']=[dict(s) for s in data['raw_segments']]
        data['camera']={'mode':'off'}
    data['output_duration']=sum((s['end']-s['start'])/s.get('speed',1) for s in data['segments'])
    data['input_fingerprint']=sig
    report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    if 'state_seconds' in data:
        s=data['state_seconds']
        print(f"暂停删除 {s.get('pause',0):.2f}s；1X 加速 {s.get('one',0):.2f}s；子弹时间加速 {s.get('bullet',0):.2f}s。",flush=True)
    print(f"剪辑区间 {len(data['segments'])} 段；预计输出 {data.get('output_duration',0):.2f}s；报告 {report}",flush=True)
    if not args.analyze_only:
        print('正在编码视频和处理音效……',flush=True)
        rendered=export_video(source,output,data['segments'],ffmpeg=args.ffmpeg,ffprobe=args.ffprobe,
                              audio_mode=args.audio,tail_ms=args.tail_ms,fade_ms=args.fade_ms,crf=args.crf,
                              preset=args.preset,fps=args.fps,progress=progress,work_dir=args.work_dir,overwrite=args.overwrite,threads=args.threads)
        data['render']=rendered
        data['total_processing_seconds']=time.perf_counter()-started
        report.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'完成：{output}',flush=True)
    print(f'本次耗时 {time.perf_counter()-started:.2f}s',flush=True)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n已停止。原始视频未修改。',file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f'处理失败：{exc}',file=sys.stderr)
        raise SystemExit(1)

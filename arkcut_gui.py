#!/usr/bin/env python3
"""中文桌面界面；后台调用已经验证的 arkcut.py。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from arknights_cutter import __version__
from arknights_cutter.gui_support import (
    Settings, SettingsStore, JobRunner, build_job, preset_settings,
)

PACKAGE_DIR = Path(__file__).resolve().parent
BG = '#f3f5fa'
NAV = '#172239'
INK = '#202e45'
MUTED = '#62718a'
ACCENT = '#2465db'
CAMERA = {'连贯优先（推荐）': 'auto', '保守清理': 'strict', '保留全部操作': 'off'}
SETTLE = {'自动（最多 2 帧）': 2, '最多 1 帧': 1, '关闭': 0}
AUDIO = {'保留尾音（推荐）': 'tail', '平滑切点': 'smooth', '直接剪切': 'cut', '静音': 'mute'}
FPS = {'跟随原视频': 'source', '60 帧 / 秒': '60', '30 帧 / 秒': '30'}
SPEED = {'最快': 'ultrafast', '很快': 'superfast', '快速': 'veryfast',
         '较快': 'faster', '均衡': 'fast', '较慢': 'medium', '慢速': 'slow'}
QUALITY = {'均衡（推荐）': 'balanced', '更快导出': 'fast', '更清晰': 'quality'}


def display_value(mapping, value):
    return next((label for label, item in mapping.items() if str(item) == str(value)), '')


def hidden_process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


class ScrollPage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style='Page.TFrame')
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style='Page.TFrame')
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scroll.pack(side='right', fill='y')
        self.body.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.window, width=e.width))

    def wheel(self, delta):
        self.canvas.yview_scroll(-int(delta / 120) or (-1 if delta > 0 else 1), 'units')


class CutterApp:
    def __init__(self, root: tk.Tk, *, settings_path=None, initial_input=None):
        self.root = root
        self.store = SettingsStore(settings_path)
        self.runner = None
        self.spec = None
        self.last_result = None
        self.started = None
        self.closing = False
        self.busy = False
        self.edit_widgets = []
        self.metadata_queue = queue.Queue()
        self.probe_serial = 0
        self.applying = False
        self.last_auto_output = ''
        self._configure_window()
        self._styles()
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.meta_var = tk.StringVar(value='选择录屏后自动填写输出位置；支持不同分辨率和异形屏。')
        self.preset_var = tk.StringVar(value='均衡（推荐）')
        self.camera_var = tk.StringVar()
        self.settle_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.fps_var = tk.StringVar()
        self.speed_var = tk.StringVar()
        self.thread_var = tk.StringVar()
        self.status_var = tk.StringVar(value='准备就绪')
        self.detail_var = tk.StringVar(value='选择视频，调整设置，然后开始剪辑。')
        self.elapsed_var = tk.StringVar(value='')
        self.camera_hint = tk.StringVar()
        self.settings_vars = {}
        defaults = Settings()
        for field in ('tail_ms', 'fade_ms', 'crf', 'camera_flash_ms', 'camera_recovery_ms',
                      'ffmpeg', 'ffprobe', 'work_dir', 'detection_config'):
            self.settings_vars[field] = tk.StringVar(value=str(getattr(defaults, field) or ''))
        for field in ('reuse_analysis', 'analyze_only'):
            self.settings_vars[field] = tk.BooleanVar(value=getattr(defaults, field))
        self._layout()
        try:
            self._apply_settings(self.store.load())
        except (ValueError, OSError, TypeError) as exc:
            self._apply_settings(defaults)
            self._log(f'上次设置无法读取，已恢复默认：{exc}')
        self.preset_var.set('均衡（推荐）' if not self.store.path.exists() else '自定义 / 上次设置')
        self.input_var.trace_add('write', self._input_changed)
        for var in (self.camera_var, self.audio_var):
            var.trace_add('write', lambda *_: self._conditions())
        for var in (self.fps_var, self.speed_var, self.thread_var, self.settings_vars['crf']):
            var.trace_add('write', self._encoding_changed)
        self.settings_vars['analyze_only'].trace_add('write', self._action_label)
        self._action_label()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind_all('<MouseWheel>', self._wheel, add='+')
        self.root.after(80, self._poll)
        if initial_input:
            self.input_var.set(str(Path(initial_input).expanduser().resolve()))

    def _configure_window(self):
        self.root.title('明日方舟自动剪辑')
        width = min(1140, max(900, self.root.winfo_screenwidth() - 100))
        height = min(940, max(640, self.root.winfo_screenheight() - 90))
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(900, 640)
        self.root.configure(bg=BG)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        family = 'Microsoft YaHei UI' if os.name == 'nt' else 'DejaVu Sans'
        style.configure('.', font=(family, 10), foreground=INK)
        style.configure('Page.TFrame', background=BG)
        style.configure('Card.TFrame', background='white')
        style.configure('Card.TLabel', background='white', foreground=INK)
        style.configure('Hint.TLabel', background='white', foreground=MUTED, font=(family, 9))
        style.configure('Page.TLabel', background=BG, foreground=MUTED)
        style.configure('Title.TLabel', background=BG, foreground=INK, font=(family, 21, 'bold'))
        style.configure('Section.TLabel', background='white', font=(family, 12, 'bold'))
        style.configure('TButton', padding=(12, 7), background='#eaf0fb', borderwidth=0)
        style.map('TButton', background=[('active', '#dbe6fa'), ('disabled', '#edf0f5')],
                  foreground=[('disabled', '#93a0b4')])
        style.configure('Primary.TButton', background=ACCENT, foreground='white', padding=(20, 10), font=(family, 11, 'bold'))
        style.map('Primary.TButton', background=[('active', '#174fb4'), ('disabled', '#a7bbdb')],
                  foreground=[('disabled', 'white')])
        style.configure('TEntry', padding=6, fieldbackground='#fbfcff', bordercolor='#d9e0ee')
        style.configure('TCombobox', padding=5, fieldbackground='#fbfcff', bordercolor='#d9e0ee')
        style.map('TCombobox', fieldbackground=[('readonly', '#fbfcff'), ('disabled', '#edf0f5')])
        style.configure('TSpinbox', padding=5, fieldbackground='#fbfcff', bordercolor='#d9e0ee')
        style.configure('Card.TCheckbutton', background='white', padding=(0, 3))
        style.configure('Task.Horizontal.TProgressbar', background=ACCENT, troughcolor='#e6ecf6', borderwidth=0)

    def _editable(self, widget):
        self.edit_widgets.append(widget)
        return widget

    def _layout(self):
        nav = tk.Frame(self.root, bg=NAV, width=205)
        nav.pack(side='left', fill='y')
        nav.pack_propagate(False)
        brand = tk.Canvas(nav, width=56, height=56, bg=NAV, highlightthickness=0)
        brand.create_rectangle(4, 4, 52, 52, fill=ACCENT, outline='')
        brand.create_text(28, 28, text='AK', font=('Segoe UI', 19, 'bold'), fill='white')
        brand.pack(anchor='w', padx=24, pady=(28, 14))
        tk.Label(nav, text='明日方舟\n自动剪辑', bg=NAV, fg='white', font=('Microsoft YaHei UI', 18, 'bold'),
                 justify='left').pack(anchor='w', padx=24)
        tk.Label(nav, text='让实战回放更连贯', bg=NAV, fg='#acbbd5', font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=24, pady=(10, 32))
        self.nav_buttons = {}
        for key, label in [('main', '剪辑设置'), ('advanced', '高级设置'), ('log', '处理记录')]:
            button = tk.Button(nav, text=label, bg=NAV, fg='#cbd6e8', relief='flat', bd=0,
                               anchor='w', padx=24, pady=13, activebackground='#263953', activeforeground='white',
                               font=('Microsoft YaHei UI', 11), cursor='hand2', command=lambda k=key: self.show_page(k))
            button.pack(fill='x', padx=10, pady=3)
            self.nav_buttons[key] = button
        tk.Label(nav, text=f'简中服 UI · v{__version__}\n自动适配画面尺寸', bg=NAV, fg='#91a3c0',
                 justify='left', font=('Microsoft YaHei UI', 9)).pack(side='bottom', anchor='w', padx=24, pady=24)
        shell = ttk.Frame(self.root, style='Page.TFrame')
        shell.pack(side='left', fill='both', expand=True, padx=25, pady=(22, 18))
        header = ttk.Frame(shell, style='Page.TFrame')
        header.pack(fill='x', pady=(0, 15))
        ttk.Label(header, text='把录屏剪成流畅实战', style='Title.TLabel').pack(anchor='w')
        ttk.Label(header, text='删除暂停  /  1X 加速 2 倍  /  子弹时间加速 10 倍', style='Page.TLabel').pack(anchor='w', pady=(6, 0))
        self.page_host = ttk.Frame(shell, style='Page.TFrame')
        self.page_host.pack(fill='both', expand=True)
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)
        self.pages = {name: ScrollPage(self.page_host) for name in ['main', 'advanced', 'log']}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky='nsew')
        self._main_page(self.pages['main'].body)
        self._advanced_page(self.pages['advanced'].body)
        self._log_page(self.pages['log'].body)
        self._task_bar(shell)
        self.show_page('main')

    def _card(self, parent, title):
        card = ttk.Frame(parent, style='Card.TFrame', padding=16)
        ttk.Label(card, text=title, style='Section.TLabel').pack(anchor='w', pady=(0, 13))
        return card

    def _field(self, parent, label, variable, *, values=None, spin=None, hint=None):
        box = ttk.Frame(parent, style='Card.TFrame')
        box.pack(fill='x', pady=(0, 12))
        ttk.Label(box, text=label, style='Card.TLabel').pack(anchor='w', pady=(0, 5))
        if values:
            widget = ttk.Combobox(box, textvariable=variable, values=list(values), state='readonly')
        elif spin:
            widget = ttk.Spinbox(box, textvariable=variable, from_=spin[0], to=spin[1], increment=spin[2])
        else:
            widget = ttk.Entry(box, textvariable=variable)
        widget.pack(fill='x')
        self._editable(widget)
        if hint:
            hint_label = ttk.Label(box, text=hint, style='Hint.TLabel', wraplength=320)
            hint_label.pack(anchor='w', fill='x', pady=(5, 0))
            hint_label.bind('<Configure>', lambda e, w=hint_label: w.configure(wraplength=max(120, e.width - 2)))
        return widget

    def _file_row(self, card, title, variable, command, button_label='选择…'):
        row = ttk.Frame(card, style='Card.TFrame')
        row.pack(fill='x', pady=(0, 9))
        ttk.Label(row, text=title, style='Card.TLabel', width=9).pack(side='left')
        self._editable(ttk.Entry(row, textvariable=variable)).pack(side='left', fill='x', expand=True, padx=(0, 8))
        self._editable(ttk.Button(row, text=button_label, command=command)).pack(side='right')

    def _main_page(self, parent):
        file_card = self._card(parent, '视频文件')
        file_card.pack(fill='x', pady=(0, 14))
        self._file_row(file_card, '输入视频', self.input_var, self.choose_input)
        self._file_row(file_card, '输出成片', self.output_var, self.choose_output, '另存为…')
        ttk.Label(file_card, textvariable=self.meta_var, style='Hint.TLabel', wraplength=740).pack(anchor='w', pady=(3, 0))
        row = ttk.Frame(parent, style='Page.TFrame')
        row.pack(fill='x')
        row.columnconfigure(0, weight=1, uniform='cards')
        row.columnconfigure(1, weight=1, uniform='cards')
        visual = self._card(row, '画面与导出速度')
        visual.grid(row=0, column=0, sticky='nsew', padx=(0, 7))
        preset = self._field(visual, '快速选择', self.preset_var, values=[*QUALITY, '自定义 / 上次设置'])
        preset.bind('<<ComboboxSelected>>', lambda e: self.use_preset())
        self._field(visual, '输出帧率', self.fps_var, values=FPS)
        self._field(visual, '画质数值', self.settings_vars['crf'], spin=(0, 51, 1), hint='默认 18；数值越小越清晰，文件也更大。')
        self._field(visual, '编码速度', self.speed_var, values=SPEED, hint='速度越慢，同等画质下通常更省空间。')
        self._editable(ttk.Checkbutton(visual, text='复用已有识别结果，加快重复处理', variable=self.settings_vars['reuse_analysis'],
                                      style='Card.TCheckbutton')).pack(anchor='w', pady=(1, 0))
        flow = self._card(row, '连贯性与音效')
        flow.grid(row=0, column=1, sticky='nsew', padx=(7, 0))
        self._field(flow, '镜头清理方式', self.camera_var, values=CAMERA)
        camera_hint = ttk.Label(flow, textvariable=self.camera_hint, style='Hint.TLabel', wraplength=320)
        camera_hint.pack(anchor='w', fill='x', pady=(0, 10))
        camera_hint.bind('<Configure>', lambda e: camera_hint.configure(wraplength=max(120, e.width - 2)))
        self.flash_widget = self._field(flow, '短操作清理上限（毫秒）', self.settings_vars['camera_flash_ms'], spin=(0, 5000, 100),
                                      hint='按加速后的时长；原片 1 秒子弹时间只计 100 毫秒。')
        self._field(flow, '声音处理', self.audio_var, values=AUDIO)
        audio_row = ttk.Frame(flow, style='Card.TFrame')
        audio_row.pack(fill='x')
        audio_row.columnconfigure(0, weight=1)
        audio_row.columnconfigure(1, weight=1)
        for col, (label, key, limit) in enumerate([('尾音（毫秒）', 'tail_ms', 1000), ('切点淡化（毫秒）', 'fade_ms', 100)]):
            box = ttk.Frame(audio_row, style='Card.TFrame')
            box.grid(row=0, column=col, sticky='ew', padx=(0, 8) if col == 0 else (0, 0))
            widget = self._field(box, label, self.settings_vars[key], spin=(0, limit, 1))
            setattr(self, key + '_widget', widget)

    def _advanced_page(self, parent):
        camera = self._card(parent, '镜头恢复与性能')
        camera.pack(fill='x', pady=(0, 14))
        self.recovery_widget = self._field(camera, '镜头恢复检查窗口（源视频毫秒）', self.settings_vars['camera_recovery_ms'], spin=(0, 2000, 100),
                                          hint='默认 1000；只删到实际恢复点。窗口越大，检查耗时可能越长。')
        self.settle_widget = self._field(camera, '恢复后多去掉几帧', self.settle_var, values=SETTLE,
                                        hint='确认恢复后再根据余晃省略最多 1～2 帧，减少回正抖动；按输出帧率计算。')
        self._field(camera, '编码线程', self.thread_var, values=['自动（最多 8）', '1', '2', '4', '8', '12', '16', '32', '64'],
                    hint='自动设置适合大多数设备。电脑需要同时做其它工作时可以减少线程。')
        self._file_row(camera, '临时目录', self.settings_vars['work_dir'], self.choose_work)
        ttk.Label(camera, text='留空使用输出文件所在目录；选择空闲空间充足的磁盘。', style='Hint.TLabel').pack(anchor='w')
        self._editable(ttk.Checkbutton(camera, text='只生成剪辑报告，暂不导出视频',
                                      variable=self.settings_vars['analyze_only'], style='Card.TCheckbutton')).pack(anchor='w', pady=(13, 0))
        detection = self._card(parent, '特殊录屏与工具位置')
        detection.pack(fill='x', pady=(0, 14))
        self._file_row(detection, '检测配置', self.settings_vars['detection_config'], self.choose_config)
        ttk.Label(detection, text='通常留空即可。特殊黑边、裁剪或 UI 位置需要调整时，可选择自定义配置文件。',
                  style='Hint.TLabel', wraplength=720).pack(anchor='w', pady=(0, 16))
        self._file_row(detection, 'FFmpeg', self.settings_vars['ffmpeg'], lambda: self.choose_tool('ffmpeg'))
        self._file_row(detection, 'FFprobe', self.settings_vars['ffprobe'], lambda: self.choose_tool('ffprobe'))
        ttk.Label(detection, text='留空自动查找脚本目录的 bin 文件夹和系统 PATH。', style='Hint.TLabel').pack(anchor='w')
        help_card = self._card(parent, '使用提示')
        help_card.pack(fill='x')
        ttk.Label(help_card, text='连贯优先会省略短暂的选中面板、镜头移动和少量战斗起手画面。\n'
                  '保留全部操作仍会删除暂停，并将运行中的子弹时间加速 10 倍。\n'
                  '已混合的音轨无法独立恢复单条音效；尾音不增加视频时长。',
                  style='Hint.TLabel', wraplength=740, justify='left').pack(anchor='w')
        self._editable(ttk.Button(help_card, text='打开完整使用说明', command=lambda: self.open_path(PACKAGE_DIR / 'README.md'))).pack(anchor='w', pady=(12, 0))

    def _log_page(self, parent):
        card = self._card(parent, '处理记录')
        card.pack(fill='both', expand=True)
        ttk.Label(card, text='记录本次启动期间的处理信息，便于检查问题。最多保留最近 500 行。',
                  style='Hint.TLabel').pack(anchor='w', pady=(0, 12))
        self.log_text = tk.Text(card, height=23, wrap='word', bg='#101b2d', fg='#d3def1',
                                relief='flat', padx=14, pady=12, font=('Consolas', 10), state='disabled')
        self.log_text.pack(fill='both', expand=True)
        actions = ttk.Frame(card, style='Card.TFrame')
        actions.pack(fill='x', pady=(12, 0))
        ttk.Button(actions, text='复制记录', command=self.copy_log).pack(side='left')
        ttk.Button(actions, text='清空记录', command=self.clear_log).pack(side='left', padx=8)

    def _task_bar(self, parent):
        card = ttk.Frame(parent, style='Card.TFrame', padding=(16, 13))
        card.pack(fill='x', pady=(16, 0))
        line = ttk.Frame(card, style='Card.TFrame')
        line.pack(fill='x')
        ttk.Label(line, textvariable=self.status_var, style='Section.TLabel').pack(side='left')
        ttk.Label(line, textvariable=self.elapsed_var, style='Hint.TLabel').pack(side='right')
        self.progress_bar = ttk.Progressbar(card, mode='determinate', maximum=100, style='Task.Horizontal.TProgressbar')
        self.progress_bar.pack(fill='x', pady=(9, 6))
        ttk.Label(card, textvariable=self.detail_var, style='Hint.TLabel', wraplength=750).pack(anchor='w')
        controls = ttk.Frame(card, style='Card.TFrame')
        controls.pack(fill='x', pady=(12, 0))
        self.start_button = ttk.Button(controls, text='开始剪辑', style='Primary.TButton', command=self.start_job)
        self.start_button.pack(side='right')
        self.stop_button = ttk.Button(controls, text='停止', command=self.cancel_job, state='disabled')
        self.stop_button.pack(side='right', padx=(0, 8))
        self._editable(ttk.Button(controls, text='保存设置', command=self.save_settings)).pack(side='left')
        self._editable(ttk.Button(controls, text='恢复默认', command=self.reset_settings)).pack(side='left', padx=7)
        results = self.results_frame = ttk.Frame(card, style='Card.TFrame')
        self.result_buttons = {}
        for key, label in [('video', '打开成片'), ('folder', '打开输出目录'), ('report', '查看剪辑报告')]:
            button = ttk.Button(results, text=label, state='disabled', command=lambda k=key: self.open_result(k))
            button.pack(side='left', padx=(0, 8))
            self.result_buttons[key] = button

    def show_page(self, name):
        self.current_page = name
        self.pages[name].tkraise()
        for key, button in self.nav_buttons.items():
            button.configure(bg='#293c58' if key == name else NAV, fg='white' if key == name else '#cbd6e8')

    def _wheel(self, event):
        # Only scroll this app's body, never spinboxes or open dropdowns.
        if isinstance(event.widget, (ttk.Spinbox, ttk.Combobox)):
            return
        try:
            self.pages[self.current_page].wheel(event.delta)
        except tk.TclError:
            pass

    def _apply_settings(self, settings):
        self.applying = True
        for key, var in self.settings_vars.items():
            value = getattr(settings, key)
            if key in {'ffmpeg', 'ffprobe'} and value == 'auto':
                value = ''
            var.set(bool(value) if isinstance(var, tk.BooleanVar) else ('' if value is None else value))
        self.camera_var.set(display_value(CAMERA, settings.camera))
        self.settle_var.set(display_value(SETTLE, settings.camera_settle_frames))
        self.audio_var.set(display_value(AUDIO, settings.audio))
        self.fps_var.set(display_value(FPS, settings.fps))
        self.speed_var.set(display_value(SPEED, settings.preset))
        self.thread_var.set('自动（最多 8）' if str(settings.threads) == 'auto' else str(settings.threads))
        self.applying = False
        self._conditions()

    def current_settings(self):
        values = {key: var.get() for key, var in self.settings_vars.items()}
        for key in ('ffmpeg', 'ffprobe'):
            if not values[key].strip():
                values[key] = 'auto'
        values.update(camera=CAMERA.get(self.camera_var.get(), ''), audio=AUDIO.get(self.audio_var.get(), ''),
                      camera_settle_frames=SETTLE.get(self.settle_var.get(), ''),
                      fps=FPS.get(self.fps_var.get(), ''), preset=SPEED.get(self.speed_var.get(), ''),
                      threads='auto' if self.thread_var.get() == '自动（最多 8）' else self.thread_var.get())
        return Settings(**values)

    def use_preset(self):
        key = QUALITY.get(self.preset_var.get())
        if key:
            try:
                settings = preset_settings(key, self.current_settings())
                self._apply_settings(settings)
                self.detail_var.set('已应用导出预设；镜头、声音与高级设置保留当前选择。')
            except ValueError as exc:
                messagebox.showerror('设置需要调整', str(exc), parent=self.root)

    def _encoding_changed(self, *_):
        if not self.applying:
            self.preset_var.set('自定义 / 上次设置')

    def _action_label(self, *_):
        report_only = self.settings_vars['analyze_only'].get()
        self.start_button.configure(text='生成剪辑报告' if report_only else '开始剪辑')
        if not self.busy:
            self.detail_var.set('当前只生成报告；可在高级设置切换为导出视频。' if report_only else '选择视频，调整设置，然后开始剪辑。')

    def _conditions(self):
        camera = CAMERA.get(self.camera_var.get())
        audio = AUDIO.get(self.audio_var.get())
        self.camera_hint.set({'auto': '省略短选中镜头和恢复过渡，以观看连贯性为先。',
                              'strict': '只清理确认发生镜头移动的短操作，内容保留更多。',
                              'off': '保留运行中的选中画面，子弹时间仍加速 10 倍。'}.get(camera, ''))
        for widget, enabled in [(self.flash_widget, camera != 'off'), (self.recovery_widget, camera != 'off'),
                                (self.settle_widget, camera != 'off'),
                                (self.tail_ms_widget, audio == 'tail'), (self.fade_ms_widget, audio in {'tail', 'smooth'})]:
            state = 'readonly' if isinstance(widget, ttk.Combobox) else 'normal'
            widget.configure(state=state if enabled and not self.busy else 'disabled')

    def choose_input(self):
        path = filedialog.askopenfilename(parent=self.root, title='选择明日方舟录屏',
                                         filetypes=[('视频文件', '*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.gif'), ('所有文件', '*.*')])
        if path:
            self.input_var.set(path)

    def choose_output(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='保存剪辑后的成片', defaultextension='.mp4',
                                           initialfile=Path(self.output_var.get()).name or '剪辑成片.mp4',
                                           filetypes=[('MP4 视频', '*.mp4')])
        if path:
            self.output_var.set(path)

    def choose_work(self):
        path = filedialog.askdirectory(parent=self.root, title='选择临时文件目录')
        if path:
            self.settings_vars['work_dir'].set(path)

    def choose_config(self):
        path = filedialog.askopenfilename(parent=self.root, title='选择检测配置', filetypes=[('JSON 配置', '*.json')])
        if path:
            self.settings_vars['detection_config'].set(path)

    def choose_tool(self, name):
        path = filedialog.askopenfilename(parent=self.root, title=f'选择 {name}', filetypes=[('程序', '*.exe'), ('所有文件', '*.*')])
        if path:
            self.settings_vars[name].set(path)

    def _input_changed(self, *_):
        self.probe_serial += 1
        serial = self.probe_serial
        value = self.input_var.get().strip()
        if not value:
            return
        source = Path(value).expanduser()
        if not source.name:
            self.meta_var.set('请选择具体的视频文件。')
            return
        if not self.output_var.get() or self.output_var.get() == self.last_auto_output:
            self.last_auto_output = str(source.with_name(source.stem + '.edited.mp4'))
            self.output_var.set(self.last_auto_output)
        self.meta_var.set('正在读取视频信息…')
        tool = self.settings_vars['ffprobe'].get().strip()
        threading.Thread(target=self._probe_input, args=(value, serial, tool), daemon=True).start()

    def _probe_input(self, value, serial, requested):
        try:
            source = Path(value).expanduser()
            if not source.is_file():
                self.metadata_queue.put((serial, '请检查输入视频的位置。'))
                return
            executable = requested or (str(PACKAGE_DIR / 'bin/ffprobe.exe') if (PACKAGE_DIR / 'bin/ffprobe.exe').is_file() else shutil.which('ffprobe'))
            if not executable:
                self.metadata_queue.put((serial, f'{source.name}  ·  可在高级设置指定工具位置。'))
                return
            process = subprocess.run([executable, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(source)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, **hidden_process_options())
            data = json.loads(process.stdout)
            stream = next(s for s in data['streams'] if s.get('codec_type') == 'video')
            duration = float(data.get('format', {}).get('duration', stream.get('duration', 0)))
            has_audio = any(s.get('codec_type') == 'audio' for s in data['streams'])
            self.metadata_queue.put((serial, f"{source.name}  ·  {stream['width']} × {stream['height']}  ·  {int(duration // 60)} 分 {duration % 60:.1f} 秒  ·  {'含音轨' if has_audio else '无音轨'}"))
        except Exception:
            self.metadata_queue.put((serial, '视频信息暂时无法读取；开始前会检查文件与工具是否可用。'))

    def save_settings(self):
        try:
            self.store.save(self.current_settings())
            self.detail_var.set('设置已保存，下次打开时自动恢复。')
        except (ValueError, OSError, TypeError) as exc:
            messagebox.showerror('设置未保存', str(exc), parent=self.root)

    def reset_settings(self):
        self._apply_settings(Settings())
        self.preset_var.set('均衡（推荐）')
        self.detail_var.set('已恢复默认参数；开始剪辑或保存设置后会记住这些选择。')

    def start_job(self):
        if self.busy:
            return
        try:
            settings = self.current_settings().validate()
            if not self.input_var.get().strip() or not self.output_var.get().strip():
                raise ValueError('请选择输入视频与输出成片的位置。')
            output = Path(self.output_var.get().strip()).expanduser()
            report = output.with_suffix('.report.json')
            existing = [p for p in [output, report] if p.exists() and (p != output or not settings.analyze_only)]
            allow = False
            if existing:
                text = '\n'.join(p.name for p in existing)
                if not messagebox.askyesno('确认更新已有结果', f'目标位置已存在：\n{text}\n\n本次处理会更新这些结果，是否继续？', parent=self.root):
                    return
                allow = True
            self.spec = build_job(self.input_var.get().strip(), str(output), settings, PACKAGE_DIR,
                                  python_executable=sys.executable, allow_overwrite=allow)
            self.store.save(settings)
            self.runner = JobRunner(self.spec)
            self.last_result = None
            self.started = time.monotonic()
            self.progress_bar.stop()
            self.progress_bar.configure(mode='indeterminate', value=0)
            self.progress_bar.start(15)
            self.status_var.set('准备处理')
            self.detail_var.set('正在启动；进度百分比表示当前阶段。')
            self._set_busy(True)
            self._log(f'开始：{self.spec.input_path}\n输出：{self.spec.output_path}')
            self.runner.start()
        except (ValueError, OSError, TypeError) as exc:
            self._set_busy(False)
            messagebox.showerror('无法开始剪辑', str(exc), parent=self.root)
            self._log(str(exc))

    def _set_busy(self, value):
        self.busy = value
        self.start_button.configure(state='disabled' if value else 'normal')
        self.stop_button.configure(state='normal' if value else 'disabled')
        for widget in self.edit_widgets:
            widget.configure(state='disabled' if value else ('readonly' if isinstance(widget, ttk.Combobox) else 'normal'))
        for button in self.result_buttons.values():
            button.configure(state='disabled')
        self.results_frame.pack_forget()
        self._conditions()

    def cancel_job(self):
        if self.runner and self.busy:
            self.stop_button.configure(state='disabled')
            self.status_var.set('正在停止')
            self.detail_var.set('正在结束本次处理及其视频子进程，请稍候。')
            self.runner.cancel()

    def _poll(self):
        try:
            while True:
                serial, value = self.metadata_queue.get_nowait()
                if serial == self.probe_serial:
                    self.meta_var.set(value)
        except queue.Empty:
            pass
        if self.runner:
            try:
                for _ in range(150):
                    event = self.runner.events.get_nowait()
                    if event.kind == 'log':
                        self._log(event.message)
                    elif event.kind == 'progress' and self.stop_button.cget('state') != 'disabled':
                        update = event.progress
                        self.status_var.set(update.label)
                        if update.percent is None:
                            self.progress_bar.configure(mode='indeterminate')
                            self.progress_bar.start(15)
                        else:
                            self.progress_bar.stop()
                            self.progress_bar.configure(mode='determinate', value=update.percent)
                        self.detail_var.set('当前阶段的进度；各阶段所需时间不同。')
                    elif event.kind == 'finished':
                        self._finished(event)
                        break
            except queue.Empty:
                pass
        if self.busy and self.started:
            seconds = int(time.monotonic() - self.started)
            self.elapsed_var.set(f'已用时 {seconds // 60:02d}:{seconds % 60:02d}')
        try:
            self.root.after(80, self._poll)
        except tk.TclError:
            pass

    def _finished(self, event):
        self.progress_bar.stop()
        self.progress_bar.configure(mode='determinate', value=100 if event.success else 0)
        self._set_busy(False)
        self.last_result = event if event.success else None
        if event.cancelled:
            self.status_var.set('已停止')
            self.detail_var.set('本次处理已结束，可以修改设置后重新开始。')
        elif event.success:
            self.status_var.set('报告已生成' if self.spec.analyze_only else '剪辑完成')
            self.detail_var.set(str(event.report_path if self.spec.analyze_only else event.output_path))
            self.result_buttons['folder'].configure(state='normal')
            self.result_buttons['report'].configure(state='normal')
            self.results_frame.pack(fill='x', pady=(10, 0))
            if not self.spec.analyze_only:
                self.result_buttons['video'].configure(state='normal')
        else:
            self.status_var.set('处理未完成')
            self.detail_var.set(event.error or '打开处理记录，查看具体原因。')
            self._log(event.error or '处理失败。')
            self.show_page('log')
            if not self.closing:
                messagebox.showerror('处理未完成', event.error or '请查看处理记录。', parent=self.root)
        if self.closing:
            self.root.destroy()

    def _log(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', str(message).rstrip() + '\n')
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > 500:
            self.log_text.delete('1.0', f'{lines - 500}.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get('1.0', 'end-1c'))
        self.detail_var.set('处理记录已复制。')

    def clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def open_path(self, path):
        try:
            path = Path(path)
            if not path.exists():
                raise FileNotFoundError(f'文件或目录不存在：{path}')
            if os.name == 'nt':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except OSError as exc:
            messagebox.showerror('无法打开', str(exc), parent=self.root)

    def open_result(self, key):
        if self.last_result:
            path = {'video': self.last_result.output_path, 'report': self.last_result.report_path,
                    'folder': Path(self.last_result.report_path).parent}[key]
            self.open_path(path)

    def close(self):
        if self.busy:
            if messagebox.askyesno('停止并关闭', '视频仍在处理中。停止本次处理并关闭窗口？', parent=self.root):
                self.closing = True
                self.cancel_job()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = CutterApp(root, initial_input=sys.argv[1] if len(sys.argv) > 1 else None)
    def callback_error(exc_type, exc_value, tb):
        app._log(''.join(traceback.format_exception(exc_type, exc_value, tb)))
        messagebox.showerror('界面操作未完成', str(exc_value), parent=root)
    root.report_callback_exception = callback_error
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        try:
            folder = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ArknightsCutter'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'gui-error.log').write_text(traceback.format_exc(), encoding='utf-8')
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('界面无法启动', f'{exc}\n\n详细信息：{folder / "gui-error.log"}\n请从“启动界面.cmd”打开。', parent=root)
            root.destroy()
        except Exception:
            pass
        raise SystemExit(1)

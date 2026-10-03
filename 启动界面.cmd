@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
if errorlevel 1 goto folderfailed
if exist "%~dp0bin\ffmpeg.exe" set "PATH=%~dp0bin;%PATH%"
if not exist "%~dp0arkcut_gui.py" goto missinggui

echo 正在检查图形界面运行环境……
if exist "%~dp0.venv\Scripts\python.exe" goto checkenv

where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10); tkinter.Tcl()" >nul 2>nul
if errorlevel 1 goto usepython
echo 首次启动：正在建立独立的 Python 环境……
py -3 -m venv "%~dp0.venv"
if errorlevel 1 goto envfailed
goto checkenv

:usepython
where python >nul 2>nul
if errorlevel 1 goto nopython
python -c "import sys, tkinter; assert sys.version_info >= (3, 10); tkinter.Tcl()" >nul 2>nul
if errorlevel 1 goto nopython
echo 首次启动：正在建立独立的 Python 环境……
python -m venv "%~dp0.venv"
if errorlevel 1 goto envfailed

:checkenv
if not exist "%~dp0.venv\Scripts\python.exe" goto envfailed
"%~dp0.venv\Scripts\python.exe" -c "import sys, tkinter; assert sys.version_info >= (3, 10); tkinter.Tcl()" >nul 2>nul
if errorlevel 1 goto badenv
if not exist "%~dp0.venv\Scripts\pythonw.exe" goto badenv
"%~dp0.venv\Scripts\python.exe" -c "import cv2, numpy" >nul 2>nul
if not errorlevel 1 goto run

echo 首次使用需要联网安装视频识别依赖。这可能需要几分钟。
echo 正在安装依赖，进度与错误会显示在下方……
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto depsfailed
echo 正在验证识别依赖……
"%~dp0.venv\Scripts\python.exe" -c "import cv2, numpy" >nul 2>nul
if errorlevel 1 goto depsfailed

:run
"%~dp0.venv\Scripts\python.exe" -c "import arkcut_gui"
if errorlevel 1 goto guifailed
echo 正在打开剪辑界面……
if "%~1"=="" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0arkcut_gui.py"
) else (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0arkcut_gui.py" "%~1"
)
if errorlevel 1 goto launchfailed
popd
exit /b 0

:nopython
echo 没有找到支持图形界面的 Python 3.10 或更高版本。
echo 请从 https://www.python.org/downloads/ 安装 Python，启用 Add Python to PATH。
echo 安装时请保留 Tcl/Tk and IDLE 组件，然后重新启动本文件。
goto failed

:envfailed
echo 无法建立独立环境，请查看上方错误，并确认解压目录可以写入。
goto failed

:badenv
echo 已有的 .venv 环境缺少可用的 Python 3.10、Tcl/Tk 或 pythonw.exe。
echo 请确认 Python 安装完整；可将 .venv 文件夹改名后重新启动，以建立新环境。
goto failed

:depsfailed
echo 视频识别依赖安装或验证失败，请查看上方错误。
echo 请检查网络连接后重试；已安装成功的依赖会继续使用。
goto failed

:missinggui
echo 找不到 arkcut_gui.py，请重新完整解压脚本包。
goto failed

:launchfailed
echo 图形界面未能启动。请查看上方错误。
echo 界面日志位于："%LOCALAPPDATA%\ArknightsCutter\gui-error.log"
goto failed

:guifailed
echo 界面模块无法载入，请查看上方错误，并重新完整解压脚本包。
goto failed

:folderfailed
echo 无法进入脚本所在目录，请将完整脚本包解压到可访问的本地文件夹。
echo 按任意键关闭……
pause >nul
exit /b 1

:failed
echo.
echo 未能打开界面。使用说明见 README.md，按任意键关闭……
popd
pause >nul
exit /b 1

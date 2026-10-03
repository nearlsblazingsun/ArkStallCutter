@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
if exist "%~dp0bin\ffmpeg.exe" set "PATH=%~dp0bin;%PATH%"
if exist ".venv\Scripts\python.exe" goto checkdeps
echo 正在建立独立的 Python 环境……
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 -m venv .venv
goto checkenv
:usepython
where python >nul 2>nul
if errorlevel 1 goto nopython
python -m venv .venv
:checkenv
if not exist ".venv\Scripts\python.exe" goto failed
:checkdeps
".venv\Scripts\python.exe" -c "import cv2, numpy" >nul 2>nul
if not errorlevel 1 goto run
echo 首次使用需要联网安装图像识别依赖……
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
:run
".venv\Scripts\python.exe" -u arkcut.py %*
if errorlevel 1 goto failed
popd
echo.
echo 处理完成。
pause
exit /b 0
:nopython
echo 请先安装 Python 3.10 或更高版本，并启用 Add Python to PATH。
:failed
echo.
echo 未完成处理，请查看上面的错误。使用说明见 README.md。
popd
pause
exit /b 1

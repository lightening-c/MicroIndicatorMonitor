@echo off
cd /d "%~dp0"

rem 优先使用 MICRO_PYTHON 指定的解释器，其次本机 conda 环境，最后 PATH 中的 python
set "PY=%MICRO_PYTHON%"
if not defined PY if exist "%USERPROFILE%\.conda\envs\micro-tracker\python.exe" set "PY=%USERPROFILE%\.conda\envs\micro-tracker\python.exe"
if not defined PY set "PY=python"

echo ============================================
echo   微观指标跟踪 - 启动服务器
echo   http://localhost:4323
echo   关闭此窗口即停止服务器
echo ============================================
echo.
"%PY%" "%~dp0server.py"
pause

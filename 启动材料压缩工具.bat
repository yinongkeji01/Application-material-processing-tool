@echo off
setlocal
chcp 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0材料压缩工具\启动工具.ps1" %*
if errorlevel 1 (
  echo.
  echo 启动失败，请保留上方错误信息。
  pause
  exit /b 1
)
exit /b 0

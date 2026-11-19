@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "state\daemon.pid" (
    set /p DPID=<"state\daemon.pid"
    echo Đang tắt Team Discord Daemon (PID %DPID%)...
    taskkill /PID %DPID% /F >nul 2>&1
    del /f /q "state\daemon.pid" >nul 2>&1
    echo Đã tắt thành công!
) else (
    echo Không tìm thấy file state\daemon.pid (có thể daemon đang tắt).
)
pause

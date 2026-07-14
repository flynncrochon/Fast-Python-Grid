@echo off
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
call "%ROOT%build-windows.bat" || exit /b 1
call "%ROOT%build-linux.bat"   || exit /b 1

echo [build-all] win_amd64 + linux_x86_64 wheels -^> %ROOT%dist
exit /b 0

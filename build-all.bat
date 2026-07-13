@echo off
REM Build BOTH platform wheels into dist\: the Windows win_amd64 wheel and the Linux
REM linux_x86_64 wheel. Each carries only its own platform's binaries. The Linux step
REM needs WSL with Ubuntu-22.04 (see build-linux.bat for one-time setup).
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%dist" rmdir /s /q "%ROOT%dist"
call "%ROOT%build-windows.bat" || exit /b 1
call "%ROOT%build-linux.bat"   || exit /b 1

echo [build-all] win_amd64 + linux_x86_64 wheels -^> %ROOT%dist
exit /b 0

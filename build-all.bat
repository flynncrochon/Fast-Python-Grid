@echo off
REM Build everything in one shot: Windows DLLs, Linux .so (via WSL), then the
REM wheel + sdist. Requires WSL with Ubuntu-22.04 for the Linux step (see
REM build-linux.bat for one-time setup).
REM
REM Note: build-local.bat recompiles the Windows DLLs via CMake anyway, so the
REM build-windows.bat step here is only for parity/native iteration. Drop it if you
REM just want the wheel -- build-linux.bat + build-local.bat is the minimum.
setlocal
set "ROOT=%~dp0"

call "%ROOT%build-windows.bat" || exit /b 1
call "%ROOT%build-linux.bat"   || exit /b 1
call "%ROOT%build-local.bat"   || exit /b 1

echo [build-all] done -^> %ROOT%dist
exit /b 0

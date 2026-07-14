@echo off
setlocal
set "DISTRO=Ubuntu-22.04"
set "ROOT=%~dp0"

pushd "%ROOT%"
wsl -d %DISTRO% -u root -- bash build.sh
set "RC=%ERRORLEVEL%"
popd

if not "%RC%"=="0" (
  echo [build-linux] FAILED ^(is '%DISTRO%' installed?^)
  pause
  exit /b 1
)

echo [build-linux] linux_x86_64 wheel -^> %ROOT%dist
exit /b 0

@echo off
REM Build the Linux shared objects (gridcore.so, glsurface.so) from Windows by
REM running build.sh inside WSL. They land in fastpygrid\core\ so a following
REM build-local.bat sweeps them into the single py3-none-any wheel (dll + so together).
REM
REM One-time setup if the distro is missing:
REM   wsl --install -d Ubuntu-22.04
REM   wsl -d Ubuntu-22.04 -u root -- bash -c "apt-get update && apt-get install -y build-essential libgl1-mesa-dev libx11-dev libfreetype-dev"
setlocal
set "DISTRO=Ubuntu-22.04"
set "ROOT=%~dp0"

pushd "%ROOT%"
REM wsl launched from the repo dir starts in its /mnt path; build.sh cd's to its own dir.
wsl -d %DISTRO% -u root -- bash build.sh
set "RC=%ERRORLEVEL%"
popd

if not "%RC%"=="0" (
  echo [build-linux] FAILED ^(is '%DISTRO%' installed? see one-time setup at top of this script^)
  pause
  exit /b 1
)

echo [build-linux] gridcore.so + glsurface.so -^> %ROOT%fastpygrid\core
exit /b 0

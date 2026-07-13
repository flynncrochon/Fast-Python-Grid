@echo off
REM Build the Linux wheel (linux_x86_64) from Windows by running build.sh inside WSL.
REM build.sh compiles gridcore.so + glsurface.so into fastpygrid\core\ then packages
REM the wheel into dist\. The wheel carries Linux binaries only (no Windows .dll).
REM
REM One-time setup if the distro is missing:
REM   wsl --install -d Ubuntu-22.04
REM   wsl -d Ubuntu-22.04 -u root -- bash -c "apt-get update && apt-get install -y build-essential libgl1-mesa-dev libx11-dev libfreetype-dev python3-pip"
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

echo [build-linux] linux_x86_64 wheel -^> %ROOT%dist
exit /b 0

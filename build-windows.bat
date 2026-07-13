@echo off
REM Build the Windows wheel (win_amd64) into dist\. Two steps:
REM   1. Compile gridcore.dll + glsurface.dll straight into fastpygrid\core\ with MSVC,
REM      so in-place bench/fuzz scripts that import the source tree stay fresh.
REM   2. Package the win_amd64 wheel via `python -m build` (scikit-build-core rebuilds
REM      the DLLs through CMakeLists.txt and platform-tags the wheel win_amd64).
REM The wheel carries Windows binaries only (no Linux .so). See build-linux.bat for the
REM Linux wheel, build-all.bat to make both.
setlocal
set "ROOT=%~dp0"
set "OUT=%ROOT%fastpygrid\core"
set "OBJ=%ROOT%build\win-native"

REM --- step 1: native compile into fastpygrid\core\ (in-place dev binaries) ---
REM Locate the MSVC dev environment via vswhere and load it.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" ( echo [build-windows] vswhere not found - is Visual Studio installed? & pause & exit /b 1 )
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -property installationPath`) do set "VSPATH=%%i"
if not defined VSPATH ( echo [build-windows] no Visual Studio install found & pause & exit /b 1 )
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul || ( echo [build-windows] vcvars64 failed & pause & exit /b 1 )

if not exist "%OBJ%" mkdir "%OBJ%"
set "FLAGS=/nologo /LD /O2 /std:c++17 /EHsc"

REM /link /IMPLIB keeps the .lib/.exp import stubs in OBJ, not beside the .dll.
echo [build-windows] gridcore.dll
cl %FLAGS% /Fo"%OBJ%\\" /Fe"%OUT%\gridcore.dll" "%ROOT%fastpygrid\csrc\gridcore.cpp" /link /IMPLIB:"%OBJ%\gridcore.lib" || ( echo [build-windows] gridcore FAILED & pause & exit /b 1 )

echo [build-windows] glsurface.dll
cl %FLAGS% /Fo"%OBJ%\\" /Fe"%OUT%\glsurface.dll" "%ROOT%fastpygrid\csrc\glsurface.cpp" /link /IMPLIB:"%OBJ%\glsurface.lib" || ( echo [build-windows] glsurface FAILED & pause & exit /b 1 )

REM --- step 2: package the win_amd64 wheel ---
py -m pip install --quiet --upgrade build || ( echo [build-windows] could not install 'build'. Is Python on PATH? & exit /b 1 )
py -m build --wheel "%ROOT%." || exit /b 1

echo [build-windows] win_amd64 wheel -^> %ROOT%dist
exit /b 0

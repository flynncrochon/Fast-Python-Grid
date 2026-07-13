@echo off
REM Compile the Windows DLLs (gridcore.dll, glsurface.dll) straight into
REM fastpygrid\core\ with MSVC -- the Windows mirror of build-linux.bat/build.sh.
REM The .cpp #pragma comment(lib, ...) the opengl32/gdi32 libs, so cl needs no
REM explicit /link. Objects go to build\win-native\ to keep the tree clean.
REM
REM Note: build-local.bat (the wheel build) ALSO compiles these via CMake, so you
REM only need this for native-only iteration without packaging a wheel.
setlocal
set "ROOT=%~dp0"
set "OUT=%ROOT%fastpygrid\core"
set "OBJ=%ROOT%build\win-native"

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

echo [build-windows] gridcore.dll + glsurface.dll -^> %OUT%
exit /b 0

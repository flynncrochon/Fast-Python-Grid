@echo off
REM Build surface.dll (the D2D render surface) with MSVC. Run once; gpu.py loads
REM the DLL via ctypes. Tries VS 2022 then the VS 18 preview toolchain.
setlocal
set "VC2022=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "VC18=C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
if exist "%VC2022%" ( call "%VC2022%" >nul ) else if exist "%VC18%" ( call "%VC18%" >nul ) else (
    echo [build] No MSVC vcvars found. Install Visual Studio C++ tools.
    exit /b 1
)
cd /d "%~dp0"
cl /nologo /O2 /LD /EHsc surface.cpp /link /OUT:surface.dll
if errorlevel 1 ( echo [build] FAILED & exit /b 1 )
echo [build] Done: surface.dll
exit /b 0

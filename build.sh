#!/usr/bin/env bash
# Build the two Linux shared objects (gridcore.so, glsurface.so) straight into
# fastpygrid/core/, where coremodel.py / gpu.py load them via ctypes. The Linux
# counterpart to build.bat -- run it inside WSL or on any Linux box:
#
#   wsl bash build.sh          # from a Windows shell
#   ./build.sh                 # from inside WSL / Linux
#
# Needs: g++ and the dev headers -- on Ubuntu/WSL:
#   sudo apt install build-essential libgl1-mesa-dev libx11-dev libfreetype-dev
#
# This is a LOCAL test build (plain linux_x86_64). The PyPI manylinux wheel comes
# from the CI job (.github/workflows/publish.yml), not this script.
set -e
cd "$(dirname "$0")"

FLAGS="-shared -fPIC -O3 -std=c++17"
OUT=fastpygrid/core

echo "[build] gridcore.so"
g++ $FLAGS fastpygrid/csrc/gridcore.cpp -o "$OUT/gridcore.so"

echo "[build] glsurface.so"
g++ $FLAGS -I/usr/include/freetype2 fastpygrid/csrc/glsurface.cpp \
    -o "$OUT/glsurface.so" -lGL -lX11 -lfreetype

echo "[build] done -> $OUT/{gridcore,glsurface}.so"

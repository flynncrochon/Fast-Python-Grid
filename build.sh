#!/usr/bin/env bash
# Build the Linux wheel (linux_x86_64) into dist/. Two steps mirror build-windows.bat:
#   1. Compile gridcore.so + glsurface.so into fastpygrid/core/ so in-place bench/fuzz
#      scripts that import the source tree stay fresh.
#   2. Package the linux wheel via `python3 -m build` (scikit-build-core rebuilds the
#      .so through CMakeLists.txt). This is a LOCAL wheel (plain linux_x86_64); the
#      PyPI manylinux wheel comes from CI (.github/workflows/publish.yml), not here.
# Run inside WSL or on any Linux box:
#   wsl bash build.sh          # from a Windows shell
#   ./build.sh                 # from inside WSL / Linux
#
# Needs g++, the GL/X11/FreeType dev headers, and python3 + pip; on Ubuntu/WSL:
#   sudo apt install build-essential libgl1-mesa-dev libx11-dev libfreetype-dev python3-pip
set -e
cd "$(dirname "$0")"

FLAGS="-shared -fPIC -O3 -std=c++17"
OUT=fastpygrid/core

# -pthread: gridcore parallelizes sort/filter/find with std::thread, which needs
# pthread linked on glibc < 2.34.
echo "[build] gridcore.so"
g++ $FLAGS -pthread fastpygrid/csrc/gridcore.cpp -o "$OUT/gridcore.so"

echo "[build] glsurface.so"
g++ $FLAGS -I/usr/include/freetype2 fastpygrid/csrc/glsurface.cpp \
    -o "$OUT/glsurface.so" -lGL -lX11 -lfreetype

echo "[build] linux wheel"
python3 -m pip install --quiet --upgrade build
python3 -m build --wheel .

echo "[build] done -> $OUT/{gridcore,glsurface}.so ; linux wheel -> dist/"

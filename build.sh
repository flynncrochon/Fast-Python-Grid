#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

cmake -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --config Release
cmake --install build/native --prefix .

python3 -m pip install --quiet --upgrade build
python3 -m build --wheel .

echo "[build] done -> fastpygrid/core/{gridcore,glsurface}.so ; linux wheel -> dist/"

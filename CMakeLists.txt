cmake_minimum_required(VERSION 3.15)
project(fastpygrid_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# gridcore: the C++ data engine. Its .cpp #pragma comment(lib, ...) the Windows libs.
# No "lib" prefix -> ctypes loads it by name. It parallelizes sort/filter/find with
# std::thread, which needs pthread linked on glibc < 2.34 (e.g. the manylinux_2_28
# wheel container); Threads::Threads is a no-op on MSVC.
find_package(Threads REQUIRED)
add_library(gridcore SHARED fastpygrid/csrc/gridcore.cpp)
set_target_properties(gridcore PROPERTIES PREFIX "")
target_link_libraries(gridcore PRIVATE Threads::Threads)

# glsurface: the OpenGL 1.1 render backend (the only renderer). On Windows the .cpp
# #pragma comment(lib, ...) the opengl32/gdi32 libs. On Linux it needs GL, X11 and
# FreeType linked explicitly (the glyph-atlas text rasterizes via FreeType).
add_library(glsurface SHARED fastpygrid/csrc/glsurface.cpp)
set_target_properties(glsurface PROPERTIES PREFIX "")
if(NOT WIN32)
  find_package(OpenGL REQUIRED)
  find_package(X11 REQUIRED)
  find_package(Freetype REQUIRED)
  target_include_directories(glsurface PRIVATE ${FREETYPE_INCLUDE_DIRS})
  target_link_libraries(glsurface OpenGL::GL ${X11_LIBRARIES} ${FREETYPE_LIBRARIES})
endif()

# Install lays out the `fastpygrid` package: the .dll/.so drop into core/ beside the
# .py that loads them (csrc/ sources are not shipped). Destinations are under
# fastpygrid/ so this works both for build-local.bat (--prefix dist -> dist/fastpygrid)
# and the scikit-build-core wheel (prefix = wheel root -> fastpygrid/ in site-packages).
install(TARGETS gridcore glsurface RUNTIME DESTINATION fastpygrid/core LIBRARY DESTINATION fastpygrid/core)
# Also carry any prebuilt Linux .so sitting in the tree (from build.sh in WSL) so a
# Windows wheel build ships both platforms' binaries -> one py3-none-any wheel.
install(DIRECTORY fastpygrid/ DESTINATION fastpygrid FILES_MATCHING PATTERN "*.py" PATTERN "*.so")

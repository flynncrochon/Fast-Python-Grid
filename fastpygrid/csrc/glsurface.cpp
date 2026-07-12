// OpenGL 1.1 render backend for fastpygrid. Exposes the C-ABI
// (gpu_attach/render/resize/detach/probe) and decodes the packed wire buffer built
// by GpuCanvas.
//
// All GL drawing (ortho setup, quads, stencil fills, textured glyph quads, op-buffer
// decode) is shared. Two things are platform-specific and live behind #ifdef:
// creating the GL context + child window, and rasterizing a glyph to an RGB subpixel
// coverage bitmap (GDI ClearType on Windows, FreeType LCD on Linux).
//
// TEXT: GL 1.1 has no text primitive. Each codepoint is rasterized on demand into a
// GL_RGB atlas holding per-subpixel (R,G,B) coverage -- ClearType/LCD, ~3x horizontal
// resolution like Excel -- and composited in two blend passes per run so each subpixel
// blends independently: out = text*cov + dst*(1-cov). Cached per (size, bold, codepoint).
//
// Wire format (little-endian, colors 0xRRGGBB, -1 = none):
//   'R' rect : f32 x,y,w,h | i32 fill | i32 outline | f32 width
//   'L' line : f32 x1,y1,x2,y2 | i32 color | f32 width
//   'P' poly : i32 color | u16 npts | npts*(f32 x,f32 y)      (filled, may be concave)
//   'T' text : f32 x,y,w,h | i32 color | f32 size_px | u8 flags(1=bold,2=center)
//              | u16 nchars | nchars*u16 (UTF-16LE)
//   'X' text : f32 x,y,w,h | f32 origin_x | i32 color | f32 size_px | u8 bold | u16 nchars | UTF-16LE

#include <map>
#include <unordered_map>
#include <vector>
#include <algorithm>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <cstdlib>

// Swap interval: vsync OFF by default (the vblank wait capped animation fps at the
// refresh rate for no visual gain). FASTPYGRID_VSYNC=1 turns it back on. Read once,
// at context creation.
static int env_swap_interval() {
    // Vsync OFF by default: the vblank wait was pure idle time that capped animation
    // (zoom/scroll) fps at the refresh rate for no visual benefit. Opt back in with
    // FASTPYGRID_VSYNC=1.
    const char* e = std::getenv("FASTPYGRID_VSYNC");
    return (e && e[0] == '1') ? 1 : 0;
}

#ifdef _WIN32
  #define NOMINMAX               // keep windows.h from #defining min/max over std::min/max
  #include <windows.h>
  #include <GL/gl.h>
  #pragma comment(lib, "opengl32.lib")
  #pragma comment(lib, "gdi32.lib")
  #pragma comment(lib, "user32.lib")
  #define EXPORT extern "C" __declspec(dllexport)
#else
  #include <GL/gl.h>
  #include <GL/glx.h>
  #include <X11/Xlib.h>
  #include <ft2build.h>
  #include FT_FREETYPE_H
  #include FT_SYNTHESIS_H              // FT_GlyphSlot_Embolden (synthetic bold)
  #include FT_LCD_FILTER_H             // FT_Library_SetLcdFilter (subpixel LCD)
  #define EXPORT extern "C" __attribute__((visibility("default")))
#endif

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------
struct Glyph {
    bool drawable = false;         // false for whitespace/failed raster (advance only, no quad)
    int gw = 0, gh = 0;            // glyph bitmap size (px)
    float u0 = 0, v0 = 0, u1 = 0, v1 = 0;   // sub-rect inside the shared atlas texture
    int bx = 0, by = 0;            // left bearing, top bearing (baseline->top, +up)
    float uadv = 0;                // UNHINTED advance per 1px of font size (advance = uadv*fpx).
                                   // Unhinted so it scales linearly with zoom: hinted (grid-fit)
                                   // advances jump per integer px size and make ellipsis-trim
                                   // flip in/out across zoom. See text_width / batch_run.
};

// One texture holding every rasterized glyph, filled by a simple shelf packer. All
// text in a frame draws with a single bound texture, so thousands of glBindTexture
// calls per frame collapse to one.
struct Atlas {
    GLuint tex = 0;
    int size = 0;                  // square, power-of-two
    int px = 0, py = 0, rowh = 0;  // shelf cursor: next free x, shelf top y, current shelf height
    unsigned gen = 0;              // bumped on rebuild (full) so draw_run can redo a flushed run
};

struct GFont {
    int ascent = 0, descent = 0;
    int px = 0;                                   // raster pixel size (for unhinted-advance scaling)
    bool bold = false;
    std::unordered_map<uint32_t, Glyph> glyphs;   // hot: hit ~4x/char/frame, hash beats tree
#ifdef _WIN32
    HFONT hf = nullptr;
#else
    FT_Face face = nullptr;
#endif
};

// A live GL context + its per-context caches. Textures are context-bound, so the
// font/glyph cache lives here (one grid == one Ctx; the probe uses its own).
struct Ctx {
    int w = 0, h = 0;
    std::map<uint64_t, GFont> fonts;      // (size*4 | bold) -> GFont
    Atlas atlas;                         // shared glyph atlas (all fonts/sizes share it)
    GLuint bound_tex = 0;                // currently-bound texture, to skip redundant binds
    bool anim = false;                   // mid zoom-glide: raster at a snapped size + GPU-scale (see raster_size)
#ifdef _WIN32
    HWND hwnd = nullptr;
    HDC  hdc  = nullptr;
    HGLRC glrc = nullptr;
    HDC  memdc = nullptr;                // for GetGlyphOutline rasterization
#else
    Display* dpy = nullptr;
    Window   win = 0;
    GLXContext glrc = nullptr;
    bool owns_win = false;
#endif
};

static void rgb(int32_t c, GLfloat* out) {
    out[0] = ((c >> 16) & 0xff) / 255.f;
    out[1] = ((c >> 8) & 0xff) / 255.f;
    out[2] = (c & 0xff) / 255.f;
}

// Subpixel (ClearType/LCD) text: each glyph is rasterized to *per-channel* RGB
// coverage -- R/G/B lit independently -- giving ~3x horizontal resolution, the same
// thing Excel does. The atlas is GL_RGB and text draws in two blend passes (flush()).
//
// The GDI/FreeType white-on-black mask already has the renderer's gamma baked in, so
// we composite it as-is: an offscreen readback of the two-pass GL result is pixel-for-
// pixel identical to native GDI ClearType black-on-white. (An extra gamma pass over the
// mask only lightens/fringes it away from that reference -- verified, don't add one.)

// --- little-endian buffer readers (advance the cursor) ---
static float    rf(const uint8_t* p, size_t& i) { float v;    memcpy(&v, p + i, 4); i += 4; return v; }
static int32_t  ri(const uint8_t* p, size_t& i) { int32_t v;  memcpy(&v, p + i, 4); i += 4; return v; }
static uint16_t ru(const uint8_t* p, size_t& i) { uint16_t v; memcpy(&v, p + i, 2); i += 2; return v; }
static uint8_t  rb(const uint8_t* p, size_t& i) { return p[i++]; }

// ---------------------------------------------------------------------------
// Platform layer: context create/make-current/swap/destroy + glyph rasterize.
// Everything else in this file is platform-neutral GL 1.1.
// ---------------------------------------------------------------------------
#ifdef _WIN32

static const wchar_t* WCLASS = L"FastGridGLSurface";
static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_ERASEBKGND) return 1;             // GL owns every pixel
    if (m == WM_PAINT) { ValidateRect(h, nullptr); return 0; }
    if (m == WM_NCHITTEST) return HTTRANSPARENT;  // mouse falls through to the Tk/Qt parent
    return DefWindowProc(h, m, w, l);
}
static void ensure_class() {
    static bool done = false;
    if (done) return;
    done = true;
    WNDCLASSW wc = {};
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.lpszClassName = WCLASS;
    RegisterClassW(&wc);
}
static bool plat_make_context(Ctx& c, HWND parent, int w, int h, bool visible) {
    ensure_class();
    DWORD style = visible ? (WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS) : (WS_POPUP);
    c.hwnd = CreateWindowExW(0, WCLASS, L"", style, 0, 0, w, h,
                             visible ? parent : nullptr, nullptr,
                             GetModuleHandleW(nullptr), nullptr);
    if (!c.hwnd) return false;
    c.hdc = GetDC(c.hwnd);
    PIXELFORMATDESCRIPTOR pfd = {};
    pfd.nSize = sizeof(pfd); pfd.nVersion = 1;
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType = PFD_TYPE_RGBA;
    pfd.cColorBits = 32; pfd.cStencilBits = 8; pfd.cDepthBits = 0;
    int pf = ChoosePixelFormat(c.hdc, &pfd);
    if (!pf || !SetPixelFormat(c.hdc, pf, &pfd)) return false;
    c.glrc = wglCreateContext(c.hdc);
    if (!c.glrc) return false;
    wglMakeCurrent(c.hdc, c.glrc);
    // vsync per env (default ON). No-op if the driver lacks WGL_EXT_swap_control.
    typedef BOOL(WINAPI * SwapIntervalProc)(int);
    if (auto swap_interval = (SwapIntervalProc)wglGetProcAddress("wglSwapIntervalEXT"))
        swap_interval(env_swap_interval());
    c.memdc = CreateCompatibleDC(nullptr);
    c.w = w; c.h = h;
    return true;
}
static void plat_make_current(Ctx& c) {
    // Skip the redundant switch: one grid owns one context and stays current between
    // frames, so calling wglMakeCurrent every frame is pure overhead. Still switches
    // correctly when multiple contexts (a second grid or the probe) interleave.
    if (wglGetCurrentContext() != c.glrc || wglGetCurrentDC() != c.hdc)
        wglMakeCurrent(c.hdc, c.glrc);
}
static void plat_swap(Ctx& c)         { SwapBuffers(c.hdc); }
static void plat_destroy(Ctx& c) {
    wglMakeCurrent(nullptr, nullptr);
    if (c.memdc) DeleteDC(c.memdc);
    for (auto& kv : c.fonts) if (kv.second.hf) DeleteObject(kv.second.hf);
    if (c.glrc) wglDeleteContext(c.glrc);
    if (c.hdc && c.hwnd) ReleaseDC(c.hwnd, c.hdc);
    if (c.hwnd) DestroyWindow(c.hwnd);
}
static void plat_font_metrics(Ctx& c, GFont& f, int size, bool bold) {
    f.hf = CreateFontW(-size, 0, 0, 0, bold ? FW_BOLD : FW_NORMAL, 0, 0, 0,
                       DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                       CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    SelectObject(c.memdc, f.hf);
    TEXTMETRICW tm; GetTextMetricsW(c.memdc, &tm);
    f.ascent = tm.tmAscent; f.descent = tm.tmDescent; f.px = size; f.bold = bold;
}
// A large reference font (per weight) whose advances are effectively un-grid-fit:
// dividing a glyph's advance at this size by REF_PX gives its size-independent design
// ratio, so layout width scales linearly with zoom. Measuring at the raster size instead
// (GDI hints/rounds each advance to the pixel grid) makes width jump per integer size,
// which flipped the ellipsis-trim in and out as you zoomed. Created once.
static const int REF_PX = 512;
static HFONT ref_font(bool bold) {
    static HFONT n = nullptr, b = nullptr;
    HFONT& h = bold ? b : n;
    if (!h) h = CreateFontW(-REF_PX, 0, 0, 0, bold ? FW_BOLD : FW_NORMAL, 0, 0, 0,
                            DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                            CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    return h;
}
// Rasterize one codepoint into an RGB subpixel-coverage bitmap (3 bytes/px). Renders
// white text on a black 32bpp DIB with the ClearType HFONT: white-on-black means each
// output R/G/B *is* that subpixel's coverage. Returns false for glyphs with no ink
// (space): caller still records the advance.
static bool plat_raster(Ctx& c, GFont& f, uint32_t cp,
                        std::vector<uint8_t>& cov, int& gw, int& gh,
                        int& bx, int& by, float& uadv) {
    SelectObject(c.memdc, f.hf);
    GLYPHMETRICS gm; MAT2 mat = {{0,1},{0,0},{0,0},{0,1}};
    // Metrics only: black-box size, origin (left/top bearing) and advance.
    DWORD r = GetGlyphOutlineW(c.memdc, cp, GGO_METRICS, &gm, 0, nullptr, &mat);
    if (r == GDI_ERROR) { gw = gh = 0; bx = by = 0; uadv = 0; return false; }
    // Size-independent advance: measure at the big reference font, divide by REF_PX.
    SelectObject(c.memdc, ref_font(f.bold));
    int rw = 0;
    if (GetCharWidth32W(c.memdc, cp, cp, &rw)) uadv = (float)rw / REF_PX;
    else uadv = (float)gm.gmCellIncX / (f.px > 0 ? f.px : 1);   // fallback (non-TT)
    SelectObject(c.memdc, f.hf);                                // restore for outline/ExtTextOut below
    int obx = gm.gmptGlyphOrigin.x, oby = gm.gmptGlyphOrigin.y;
    int ogw = gm.gmBlackBoxX, ogh = gm.gmBlackBoxY;
    if (ogw == 0 || ogh == 0) { gw = gh = 0; return false; }   // whitespace

    const int PAD = 1;                            // 1px margin: ClearType bleeds ~1 subpixel sideways
    gw = ogw + 2 * PAD; gh = ogh + 2 * PAD;
    bx = obx - PAD;     by = oby + PAD;           // stays self-consistent with the padded box

    // Top-down 32bpp DIB, filled black; draw the glyph's ink box at (PAD, PAD).
    BITMAPINFO bmi = {};
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = gw; bmi.bmiHeader.biHeight = -gh;   // negative = top-down
    bmi.bmiHeader.biPlanes = 1; bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HBITMAP dib = CreateDIBSection(c.memdc, &bmi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (!dib || !bits) { if (dib) DeleteObject(dib); gw = gh = 0; return false; }
    memset(bits, 0, (size_t)gw * gh * 4);         // black background
    HGDIOBJ oldbmp = SelectObject(c.memdc, dib);
    SetBkMode(c.memdc, TRANSPARENT);
    SetTextColor(c.memdc, RGB(255, 255, 255));
    SetTextAlign(c.memdc, TA_LEFT | TA_BASELINE | TA_NOUPDATECP);
    wchar_t w = (wchar_t)cp;
    ExtTextOutW(c.memdc, PAD - obx, PAD + oby, 0, nullptr, &w, 1, nullptr);  // baseline-relative pen
    GdiFlush();

    cov.assign((size_t)gw * gh * 3, 0);
    const uint8_t* src = (const uint8_t*)bits;    // BGRX per pixel, top-down
    for (int yy = 0; yy < gh; yy++)
        for (int xx = 0; xx < gw; xx++) {
            const uint8_t* s = src + ((size_t)yy * gw + xx) * 4;
            uint8_t* d = &cov[((size_t)yy * gw + xx) * 3];
            d[0] = s[2]; d[1] = s[1]; d[2] = s[0];   // R,G,B coverage from BGRX, as-is
        }
    SelectObject(c.memdc, oldbmp);
    DeleteObject(dib);
    return true;
}

#else  // ---- Linux / X11 / GLX / FreeType ----

static FT_Library g_ft = nullptr;
static const char* FONT_CANDIDATES[] = {
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    nullptr};
static bool plat_make_context(Ctx& c, Window parent, int w, int h, bool visible) {
    c.dpy = XOpenDisplay(nullptr);
    if (!c.dpy) return false;
    int attrs[] = { GLX_RGBA, GLX_DOUBLEBUFFER, GLX_RED_SIZE, 8, GLX_GREEN_SIZE, 8,
                    GLX_BLUE_SIZE, 8, GLX_STENCIL_SIZE, 8, None };
    XVisualInfo* vi = glXChooseVisual(c.dpy, DefaultScreen(c.dpy), attrs);
    if (!vi) return false;
    Window root = visible ? parent : RootWindow(c.dpy, vi->screen);
    XSetWindowAttributes swa = {};
    swa.colormap = XCreateColormap(c.dpy, RootWindow(c.dpy, vi->screen), vi->visual, AllocNone);
    swa.event_mask = 0;                           // input goes to the Tk/Qt parent
    c.win = XCreateWindow(c.dpy, root, 0, 0, w, h, 0, vi->depth, InputOutput,
                          vi->visual, CWColormap | CWEventMask, &swa);
    c.owns_win = true;
    if (visible) XMapWindow(c.dpy, c.win);
    c.glrc = glXCreateContext(c.dpy, vi, nullptr, True);
    XFree(vi);
    if (!c.glrc) return false;
    glXMakeCurrent(c.dpy, c.win, c.glrc);
    c.w = w; c.h = h;
    return true;
}
static void plat_make_current(Ctx& c) {
    if (glXGetCurrentContext() != c.glrc)          // skip redundant per-frame switch (see Win note)
        glXMakeCurrent(c.dpy, c.win, c.glrc);
}
static void plat_swap(Ctx& c)         { glXSwapBuffers(c.dpy, c.win); }
static void plat_destroy(Ctx& c) {
    for (auto& kv : c.fonts) if (kv.second.face) FT_Done_Face(kv.second.face);
    if (c.glrc) { glXMakeCurrent(c.dpy, None, nullptr); glXDestroyContext(c.dpy, c.glrc); }
    if (c.win && c.owns_win) XDestroyWindow(c.dpy, c.win);
    if (c.dpy) XCloseDisplay(c.dpy);
}
static void plat_font_metrics(Ctx& c, GFont& f, int size, bool bold) {
    if (!g_ft) { FT_Init_FreeType(&g_ft); FT_Library_SetLcdFilter(g_ft, FT_LCD_FILTER_DEFAULT); }
    for (int i = 0; FONT_CANDIDATES[i] && !f.face; i++)
        FT_New_Face(g_ft, FONT_CANDIDATES[i], 0, &f.face);
    if (f.face) {
        FT_Set_Pixel_Sizes(f.face, 0, size);
        f.ascent = f.face->size->metrics.ascender >> 6;
        f.descent = -(f.face->size->metrics.descender >> 6);
        f.px = size;
    }
    f.bold = bold;
}
static bool plat_raster(Ctx&, GFont& f, uint32_t cp,
                        std::vector<uint8_t>& cov, int& gw, int& gh,
                        int& bx, int& by, float& uadv) {
    gw = gh = bx = by = 0; uadv = 0;
    if (!f.face || FT_Load_Char(f.face, cp, FT_LOAD_TARGET_LCD)) return false;   // LCD-hinted outline
    FT_GlyphSlot g = f.face->glyph;
    if (f.bold) FT_GlyphSlot_Embolden(g);                       // embolden outline before rendering
    // linearHoriAdvance (16.16) is the UNHINTED advance -> scales linearly with size.
    uadv = (float)(g->linearHoriAdvance / 65536.0) / (f.px > 0 ? f.px : 1);
    if (FT_Render_Glyph(g, FT_RENDER_MODE_LCD)) return false;   // subpixel raster (R,G,B per px)
    bx = g->bitmap_left; by = g->bitmap_top;
    gw = g->bitmap.width / 3; gh = g->bitmap.rows;              // LCD bitmap is 3x wide
    if (gw == 0 || gh == 0) { gw = gh = 0; return false; }
    cov.assign((size_t)gw * gh * 3, 0);
    for (int yy = 0; yy < gh; yy++) {
        const uint8_t* src = g->bitmap.buffer + (size_t)yy * g->bitmap.pitch;
        memcpy(&cov[(size_t)yy * gw * 3], src, (size_t)gw * 3);   // R,G,B coverage as-is
    }
    return true;
}
#endif

// ---------------------------------------------------------------------------
// Platform-neutral GL 1.1 drawing
// ---------------------------------------------------------------------------
// The px size to RASTERIZE a glyph at for a target zoom size `sz`. Settled: the exact
// int px, for crisp 1:1 ClearType. Mid-glide (c.anim): snap to a power of 2 -- the ~6
// snapped sizes (4..128) cover the whole zoom range, so they warm the atlas once and
// every later glide frame is a pure cache hit (no per-size GDI raster, which was the
// ~8 ms/frame zoom-fps cap). The quad is then GPU-scaled to `sz` (batch_run), factor
// <= ~1.41x, so text is only faintly soft while moving and snaps crisp on settle.
static int anim_raster_px(float sz) {
    if (sz < 1.f) sz = 1.f;
    int e = (int)lroundf(log2f(sz));
    if (e < 2) e = 2;                    // >= 4 px
    if (e > 7) e = 7;                    // <= 128 px
    return 1 << e;
}
static inline int raster_size(const Ctx& c, float sz) {
    if (c.anim) return anim_raster_px(sz);
    int r = (int)(sz + 0.5f);
    return r < 1 ? 1 : r;
}

static GFont& get_font(Ctx& c, int size, bool bold) {
    uint64_t key = ((uint64_t)size << 2) | (bold ? 1 : 0);
    auto it = c.fonts.find(key);
    if (it != c.fonts.end()) return it->second;
    GFont& f = c.fonts[key];
    plat_font_metrics(c, f, size, bold);
    return f;
}

// Create the atlas texture lazily (first glyph). RGB, holding per-subpixel coverage.
// GL_LINEAR sampled, so a 1px gap between glyphs stops neighbors bleeding at the quad
// edges. Glyph quads are drawn 1:1 pixel-snapped, so each texel maps to one screen
// pixel and LINEAR/NEAREST are identical here.
static void ensure_atlas(Ctx& c) {
    if (c.atlas.tex) return;
    c.atlas.size = 1024;                               // holds thousands of grid glyphs
    std::vector<uint8_t> zero((size_t)c.atlas.size * c.atlas.size * 3, 0);
    glGenTextures(1, &c.atlas.tex);
    glBindTexture(GL_TEXTURE_2D, c.atlas.tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, c.atlas.size, c.atlas.size, 0,
                 GL_RGB, GL_UNSIGNED_BYTE, zero.data());
    c.bound_tex = c.atlas.tex;
}

// Atlas full (many zoom sizes accumulated): flush every cached glyph and reset the
// packer so glyphs re-raster into a fresh atlas. The current frame only uses the one
// or two sizes actually on screen, so it re-fills tiny. gen++ marks already-packed
// glyphs as invalidated.
static void atlas_reset(Ctx& c) {
    c.atlas.px = c.atlas.py = c.atlas.rowh = 0;
    c.atlas.gen++;
    for (auto& kv : c.fonts) kv.second.glyphs.clear();
    std::vector<uint8_t> zero((size_t)c.atlas.size * c.atlas.size * 3, 0);
    glBindTexture(GL_TEXTURE_2D, c.atlas.tex); c.bound_tex = c.atlas.tex;
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, c.atlas.size, c.atlas.size,
                    GL_RGB, GL_UNSIGNED_BYTE, zero.data());
}

// NOTE: this may glBindTexture/glTexSubImage2D (and rebuild the atlas), so callers
// MUST pre-cache every glyph in a run BEFORE opening a glBegin/glEnd or vertex draw
// (texture ops are illegal mid-draw), and redo the run if the atlas gen changed.
static Glyph& get_glyph(Ctx& c, GFont& f, uint32_t cp) {
    auto it = f.glyphs.find(cp);
    if (it != f.glyphs.end()) return it->second;
    ensure_atlas(c);
    Glyph g;
    std::vector<uint8_t> cov;
    bool has = plat_raster(c, f, cp, cov, g.gw, g.gh, g.bx, g.by, g.uadv);
    if (has && g.gw > 0 && g.gh > 0) {
        Atlas& a = c.atlas;
        const int pad = 1;
        if (a.px + g.gw + pad > a.size) { a.px = 0; a.py += a.rowh + pad; a.rowh = 0; }  // next shelf
        if (a.py + g.gh + pad > a.size) atlas_reset(c);     // atlas full -> flush & start over (px,py=0)
        if (g.gw + pad <= a.size && g.gh + pad <= a.size) {  // fits a fresh atlas (guards a giant glyph)
            glBindTexture(GL_TEXTURE_2D, a.tex); c.bound_tex = a.tex;
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
            glTexSubImage2D(GL_TEXTURE_2D, 0, a.px, a.py, g.gw, g.gh,   // cov is RGB subpixel coverage
                            GL_RGB, GL_UNSIGNED_BYTE, cov.data());
            g.u0 = (float)a.px / a.size;         g.v0 = (float)a.py / a.size;
            g.u1 = (float)(a.px + g.gw) / a.size; g.v1 = (float)(a.py + g.gh) / a.size;
            g.drawable = true;
            a.px += g.gw + pad;
            a.rowh = std::max(a.rowh, g.gh);
        }
    }
    f.glyphs[cp] = g;
    return f.glyphs[cp];
}

// Width of a run at font size `fpx`, from UNHINTED per-px advances (uadv*fpx). Because
// uadv is size-independent, this scales linearly with zoom -- so a column that fits the
// text at one zoom fits it at every zoom. (Hinted advances jump per integer px size and
// made the ellipsis-trim flip in and out as you zoomed.)
static float text_width(Ctx& c, GFont& f, const uint16_t* s, int n, float fpx) {
    float w = 0;
    for (int i = 0; i < n; i++) w += get_glyph(c, f, s[i]).uadv;
    return w * fpx;
}

// Append a run of glyphs (pen baseline-left at penx,baseline) to the frame's text
// batch: xy + uv + per-vertex rgb. All text in the frame draws in the two-pass
// subpixel composite (see flush()). Assumes every glyph is already cached
// (precache_text ran), so get_glyph here never touches the atlas and UVs stay valid.
// `scale` maps the glyph's raster px (g.gw/gh/bx/by) to the target zoom px: 1.0 when
// rastered at the exact size (settled), or sz/snapped when GPU-scaling mid-glide.
static void batch_run(Ctx& c, GFont& f, const uint16_t* s, int n, float penx, float baseline,
                      float fpx, float scale, const GLfloat* col, std::vector<float>& pos,
                      std::vector<float>& uv, std::vector<float>& cbuf) {
    for (int i = 0; i < n; i++) {
        Glyph& g = get_glyph(c, f, s[i]);
        if (g.drawable) {
            // At scale 1.0 snap the quad to the pixel grid so each texel maps 1:1 to a
            // screen pixel (off-grid quads sample between texels under GL_LINEAR and smear,
            // reading as bold/fuzzy at small sizes). Mid-glide the quad is scaled anyway,
            // so only the origin is snapped.
            float x0 = std::floor(penx + g.bx * scale + 0.5f), y0 = std::floor(baseline - g.by * scale + 0.5f);
            float x1 = x0 + g.gw * scale, y1 = y0 + g.gh * scale;
            pos.insert(pos.end(), {x0, y0, x1, y0, x1, y1, x0, y1});
            uv.insert(uv.end(),   {g.u0, g.v0, g.u1, g.v0, g.u1, g.v1, g.u0, g.v1});
            for (int k = 0; k < 4; k++) cbuf.insert(cbuf.end(), {col[0], col[1], col[2]});
        }
        penx += g.uadv * fpx;
    }
}

// Filled polygon that may be concave (checkmark, funnel): the classic GL 1.1
// stencil-invert trick, scissored to the bbox so the per-poly stencil clear is cheap.
static void fill_poly(Ctx& c, const std::vector<float>& xy, GLfloat* col) {
    if (xy.size() < 6) return;
    float minx = xy[0], miny = xy[1], maxx = xy[0], maxy = xy[1];
    for (size_t i = 2; i < xy.size(); i += 2) {
        minx = std::min(minx, xy[i]);   maxx = std::max(maxx, xy[i]);
        miny = std::min(miny, xy[i + 1]); maxy = std::max(maxy, xy[i + 1]);
    }
    int sx = (int)std::floor(minx), sw = (int)std::ceil(maxx) - sx;
    int sy_top = (int)std::floor(miny), sh = (int)std::ceil(maxy) - sy_top;
    glEnable(GL_SCISSOR_TEST);
    glScissor(sx, c.h - (sy_top + sh), sw + 1, sh + 1);     // scissor y-flip (window is bottom-left)
    glClear(GL_STENCIL_BUFFER_BIT);
    glEnable(GL_STENCIL_TEST);
    glColorMask(GL_FALSE, GL_FALSE, GL_FALSE, GL_FALSE);
    glStencilFunc(GL_ALWAYS, 0, 1);
    glStencilOp(GL_KEEP, GL_KEEP, GL_INVERT);
    glBegin(GL_TRIANGLE_FAN);
    for (size_t i = 0; i < xy.size(); i += 2) glVertex2f(xy[i], xy[i + 1]);
    glEnd();
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glStencilFunc(GL_NOTEQUAL, 0, 1);
    glStencilOp(GL_KEEP, GL_KEEP, GL_KEEP);
    glColor3fv(col);
    glBegin(GL_QUADS);                                       // cover the bbox; stencil masks to interior
    glVertex2f(minx, miny); glVertex2f(maxx, miny);
    glVertex2f(maxx, maxy); glVertex2f(minx, maxy);
    glEnd();
    glDisable(GL_STENCIL_TEST);
    glDisable(GL_SCISSOR_TEST);
}

// Pass 1: raster every glyph the frame needs into the atlas BEFORE any batching, so
// the atlas can't reset mid-batch and invalidate already-queued UVs. Only 'T'/'X'
// carry text; U+2026 is pre-touched because the ellipsis-trim may append it. Cheap:
// a second walk of the buffer, all get_glyph hits after the first frame at a size.
static void precache_text(Ctx& c, const uint8_t* p, size_t n) {
    size_t i = 0;
    while (i < n) {
        char t = (char)p[i++];
        if (t == 'R') { i += 16 + 4 + 4 + 4; }
        else if (t == 'L') { i += 24; }
        else if (t == 'P') { ri(p, i); uint16_t np = ru(p, i); i += (size_t)np * 8; }
        else if (t == 'T') {
            i += 16; int32_t c2 = ri(p, i); (void)c2; float sz = rf(p, i);
            uint8_t fl = rb(p, i); uint16_t ln = ru(p, i);
            const uint16_t* ws = (const uint16_t*)(p + i); i += (size_t)ln * 2;
            GFont& f = get_font(c, raster_size(c, sz), fl & 1);
            for (int k = 0; k < ln; k++) get_glyph(c, f, ws[k]);
            get_glyph(c, f, 0x2026);
        } else if (t == 'X') {
            i += 20; int32_t xc = ri(p, i); (void)xc; float sz = rf(p, i);
            uint8_t fl = rb(p, i); uint16_t ln = ru(p, i);
            const uint16_t* ws = (const uint16_t*)(p + i); i += (size_t)ln * 2;
            GFont& f = get_font(c, raster_size(c, sz), fl & 1);
            for (int k = 0; k < ln; k++) get_glyph(c, f, ws[k]);
        } else if (t == 'F') { /* barrier: no text */ }
        else break;
    }
}

// Walk the packed op buffer, issuing GL calls.
// Opaque cell fills and glyph quads are accumulated into two frame-wide batches and
// flushed together (fills first, then text on top) whenever an op must layer above
// them (line/poly/scrolled-text/outlined-rect/barrier 'F') and at the end. In a full
// grid that turns ~2000 fills + ~2000 text runs into ~2 draw calls. Safe to defer
// fills ahead of text because cell backgrounds never overlap another cell's text; the
// 'F' barrier (emitted before overlay widgets) stops the reorder crossing into an
// overlay whose own fills must stay on top.
static void draw_ops(Ctx& c, const uint8_t* p, size_t n) {
    GLfloat col[3];
    static std::vector<float> fpos, fcol;              // fill quads: xy + rgb per vertex
    static std::vector<float> tpos, tuv, tcol;         // text quads: xy + uv + rgb per vertex
    fpos.clear(); fcol.clear(); tpos.clear(); tuv.clear(); tcol.clear();

    auto flush = [&]() {
        if (!fpos.empty()) {
            // Cell fills are opaque (glColor3fv alpha=1, selection wash pre-blended in
            // Python). Blending them is a wasted read-modify-write over the whole 5MP
            // fullscreen surface -- the frame's dominant fill-rate cost. Draw them with
            // blend OFF; text (below) re-enables it for its alpha coverage.
            glDisable(GL_BLEND);
            glEnableClientState(GL_VERTEX_ARRAY);
            glEnableClientState(GL_COLOR_ARRAY);
            glVertexPointer(2, GL_FLOAT, 0, fpos.data());
            glColorPointer(3, GL_FLOAT, 0, fcol.data());
            glDrawArrays(GL_QUADS, 0, (GLsizei)(fpos.size() / 2));
            glDisableClientState(GL_COLOR_ARRAY);
            glDisableClientState(GL_VERTEX_ARRAY);
            glEnable(GL_BLEND);
            fpos.clear(); fcol.clear();
        }
        if (!tpos.empty()) {
            // Subpixel text: cov is per-channel (R,G,B) coverage. Composite in two blend
            // passes to get out = text*cov + dst*(1-cov) independently per channel:
            //   pass A  dst *= (1 - cov)         GL_REPLACE (src=cov texel), ZERO/1-SRC_COLOR
            //   pass B  dst += text * cov        GL_MODULATE (src=color*cov), ONE/ONE
            glEnable(GL_TEXTURE_2D);
            if (c.bound_tex != c.atlas.tex) { glBindTexture(GL_TEXTURE_2D, c.atlas.tex); c.bound_tex = c.atlas.tex; }
            glEnableClientState(GL_VERTEX_ARRAY);
            glEnableClientState(GL_TEXTURE_COORD_ARRAY);
            glEnableClientState(GL_COLOR_ARRAY);
            glVertexPointer(2, GL_FLOAT, 0, tpos.data());
            glTexCoordPointer(2, GL_FLOAT, 0, tuv.data());
            glColorPointer(3, GL_FLOAT, 0, tcol.data());
            GLsizei nverts = (GLsizei)(tpos.size() / 2);
            glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_REPLACE);
            glBlendFunc(GL_ZERO, GL_ONE_MINUS_SRC_COLOR);
            glDrawArrays(GL_QUADS, 0, nverts);
            glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE);
            glBlendFunc(GL_ONE, GL_ONE);
            glDrawArrays(GL_QUADS, 0, nverts);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);   // restore frame default
            glDisableClientState(GL_COLOR_ARRAY);
            glDisableClientState(GL_TEXTURE_COORD_ARRAY);
            glDisableClientState(GL_VERTEX_ARRAY);
            glDisable(GL_TEXTURE_2D);
            tpos.clear(); tuv.clear(); tcol.clear();
        }
    };

    size_t i = 0;
    while (i < n) {
        char t = (char)p[i++];
        if (t == 'R') {
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            int32_t fill = ri(p, i), ol = ri(p, i); float lw = rf(p, i);
            if (fill >= 0 && ol < 0) {                 // common case: batch the opaque fill
                rgb(fill, col);
                fpos.insert(fpos.end(), {x, y, x + w, y, x + w, y + h, x, y + h});
                for (int k = 0; k < 4; k++) fcol.insert(fcol.end(), {col[0], col[1], col[2]});
            } else if (fill >= 0 || ol >= 0) {         // outlined rect (~selection ring): draw on top
                flush();
                if (fill >= 0) {
                    rgb(fill, col); glColor3fv(col);
                    glBegin(GL_QUADS);
                    glVertex2f(x, y); glVertex2f(x + w, y);
                    glVertex2f(x + w, y + h); glVertex2f(x, y + h);
                    glEnd();
                }
                if (ol >= 0) {
                    rgb(ol, col); glColor3fv(col); glLineWidth(lw);
                    glBegin(GL_LINE_LOOP);
                    glVertex2f(x + 0.5f, y + 0.5f);         glVertex2f(x + w - 0.5f, y + 0.5f);
                    glVertex2f(x + w - 0.5f, y + h - 0.5f); glVertex2f(x + 0.5f, y + h - 0.5f);
                    glEnd();
                }
            }
        } else if (t == 'L') {
            float x1 = rf(p, i), y1 = rf(p, i), x2 = rf(p, i), y2 = rf(p, i);
            int32_t c2 = ri(p, i); float lw = rf(p, i);
            flush();
            rgb(c2, col); glColor3fv(col); glLineWidth(lw);
            glBegin(GL_LINES); glVertex2f(x1, y1); glVertex2f(x2, y2); glEnd();
        } else if (t == 'P') {
            int32_t c2 = ri(p, i); uint16_t np = ru(p, i);
            std::vector<float> xy; xy.reserve(np * 2);
            for (int k = 0; k < np; k++) { xy.push_back(rf(p, i)); xy.push_back(rf(p, i)); }
            flush();
            rgb(c2, col); fill_poly(c, xy, col);
        } else if (t == 'T') {
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            int32_t c2 = ri(p, i); float sz = rf(p, i); uint8_t fl = rb(p, i); uint16_t ln = ru(p, i);
            const uint16_t* ws = (const uint16_t*)(p + i); i += (size_t)ln * 2;
            bool bold = fl & 1, center = fl & 2;
            int rsz = raster_size(c, sz);                        // exact px settled, power-of-2 snapped mid-glide
            float sc = sz / (float)rsz;                          // raster px -> exact zoom px (quad scale)
            GFont& f = get_font(c, rsz, bold);
            float baseline = y + (h + (f.ascent - f.descent) * sc) / 2.0f;   // vertically centered
            // pad scales with the font so text keeps its proportion at every zoom
            float padL = sz * (5.f / 13.f), padR = sz * (4.f / 13.f);
            // ellipsis-trim to the available width (atlas can raster U+2026 directly)
            std::vector<uint16_t> str(ws, ws + ln);
            float avail = center ? w : (w - padL - padR);
            if (text_width(c, f, str.data(), (int)str.size(), sz) > avail && !str.empty()) {
                uint16_t dot = 0x2026;
                float ell = get_glyph(c, f, dot).uadv * sz;
                while (!str.empty() && text_width(c, f, str.data(), (int)str.size(), sz) + ell > avail)
                    str.pop_back();
                str.push_back(dot);
            }
            float tw = text_width(c, f, str.data(), (int)str.size(), sz);
            float penx = center ? (x + (w - tw) / 2.0f) : (x + padL);
            rgb(c2, col);
            batch_run(c, f, str.data(), (int)str.size(), penx, baseline, sz, sc, col, tpos, tuv, tcol);
        } else if (t == 'X') {
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            float ox = rf(p, i); int32_t c2 = ri(p, i); float sz = rf(p, i);
            uint8_t fl = rb(p, i); uint16_t ln = ru(p, i);
            const uint16_t* ws = (const uint16_t*)(p + i); i += (size_t)ln * 2;
            int rsz = raster_size(c, sz);
            float sc = sz / (float)rsz;
            GFont& f = get_font(c, rsz, fl & 1);
            float baseline = y + (h + (f.ascent - f.descent) * sc) / 2.0f;
            flush();                                   // draw queued grid first, then clipped text
            glEnable(GL_SCISSOR_TEST);
            glScissor((int)x, c.h - (int)(y + h), (int)w, (int)h);    // clip the scrolled field
            rgb(c2, col);
            batch_run(c, f, ws, ln, ox, baseline, sz, sc, col, tpos, tuv, tcol);
            flush();
            glDisable(GL_SCISSOR_TEST);
        } else if (t == 'F') {
            flush();                                   // barrier: overlay widgets layer on top
        } else {
            break;                              // unknown tag: stop rather than run off the end
        }
    }
    flush();
}

static void frame_begin(Ctx& c, int clear_rgb) {
    plat_make_current(c);
    c.bound_tex = 0;                            // texture bindings not assumed to persist across frames
    glViewport(0, 0, c.w, c.h);
    glMatrixMode(GL_PROJECTION); glLoadIdentity();
    glOrtho(0, c.w, c.h, 0, -1, 1);             // top-left origin, y down == engine pixel coords
    glMatrixMode(GL_MODELVIEW); glLoadIdentity();
    glDisable(GL_DEPTH_TEST);
    glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    GLfloat cc[3] = {1, 1, 1};
    if (clear_rgb >= 0) rgb(clear_rgb, cc);
    glClearColor(cc[0], cc[1], cc[2], 1.0f);
    // Only the color buffer needs a frame-wide clear. The stencil buffer is used
    // solely by fill_poly, which clears its own scissored box before each use, so a
    // full-buffer stencil clear here is wasted bandwidth (it scales with resolution).
    glClear(GL_COLOR_BUFFER_BIT);
}

// ---------------------------------------------------------------------------
// C-ABI. The parent handle is an HWND on Windows and an X11 Window/XID on Linux
// (both are what host.hwnd()/winId() return there).
// ---------------------------------------------------------------------------
EXPORT void* gpu_attach(void* parent, int w, int h) {
    if (w < 1) w = 1; if (h < 1) h = 1;
    Ctx* c = new Ctx();
#ifdef _WIN32
    if (!plat_make_context(*c, (HWND)parent, w, h, true)) { delete c; return nullptr; }
#else
    if (!plat_make_context(*c, (Window)(uintptr_t)parent, w, h, true)) { delete c; return nullptr; }
#endif
    return c;
}

EXPORT void gpu_render(void* sp, const uint8_t* ops, int n, int clear_rgb, int animating) {
    Ctx* c = (Ctx*)sp;
    if (!c) return;
    c->anim = animating != 0;                   // mid zoom-glide: raster snapped + GPU-scale (raster_size)
    frame_begin(*c, clear_rgb);
    precache_text(*c, ops, (size_t)n);          // raster all glyphs before batching
    draw_ops(*c, ops, (size_t)n);
    plat_swap(*c);
}

EXPORT void gpu_resize(void* sp, int w, int h) {
    Ctx* c = (Ctx*)sp;
    if (!c) return;
    if (w < 1) w = 1; if (h < 1) h = 1;
    c->w = w; c->h = h;
#ifdef _WIN32
    MoveWindow(c->hwnd, 0, 0, w, h, FALSE);
#else
    XResizeWindow(c->dpy, c->win, w, h);
#endif
}

EXPORT void gpu_detach(void* sp) {
    Ctx* c = (Ctx*)sp;
    if (!c) return;
    plat_destroy(*c);
    delete c;
}

// Self-test: render ops to a hidden GL window and read one pixel back (0xAARRGGBB).
// Lets Python assert the GL draw path is pixel-correct with no visible window.
// 0xDEADnnnn = init failure. glReadPixels is bottom-left origin, so flip py.
EXPORT unsigned int gpu_probe_pixel(const uint8_t* ops, int n, int w, int h, int px, int py) {
    if (w < 1) w = 1; if (h < 1) h = 1;
    Ctx c;
#ifdef _WIN32
    if (!plat_make_context(c, nullptr, w, h, false)) { return 0xDEAD0001; }
#else
    if (!plat_make_context(c, 0, w, h, false)) { return 0xDEAD0001; }
#endif
    frame_begin(c, 0x000000);                    // black clear: unpainted reads back black
    precache_text(c, ops, (size_t)n);
    draw_ops(c, ops, (size_t)n);
    glFinish();
    unsigned char px4[4] = {0, 0, 0, 0};
    glReadPixels(px, h - 1 - py, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px4);
    unsigned int argb = ((unsigned)px4[3] << 24) | ((unsigned)px4[0] << 16)
                      | ((unsigned)px4[1] << 8) | px4[2];
    plat_destroy(c);
    return argb;
}

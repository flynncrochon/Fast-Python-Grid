// Direct2D/DirectWrite render surface for the fastgrid D2D backend.
//
// C-ABI (loaded from Python via ctypes -- no pybind11, no Python.h, so one DLL
// works across every Python on the machine). The whole display list crosses the
// boundary as ONE packed byte buffer per frame (same batching idea as the Tk
// renderer's fgblit), decoded here into D2D draw calls -- never per-primitive
// Python->native calls, which is the cost that path exists to avoid.
//
// Phase 1: draw_ops() + an offscreen pixel-readback self-test entry
// (gpu_probe_pixel). The HWND-embedding entry points come in phase 2 and reuse
// draw_ops() unchanged -- only the render target differs (HwndRenderTarget vs
// this WIC bitmap target).
//
// Op buffer wire format (little-endian; colors are 0xRRGGBB as int32, -1 = none):
//   'R' rect : f32 x,y,w,h ; i32 fill ; i32 outline ; f32 width
//   'L' line : f32 x1,y1,x2,y2 ; i32 color ; f32 width
//   'P' poly : i32 color ; u16 npts ; npts*(f32 x, f32 y)   (filled, closed)
//   'T' text : f32 x,y,w,h ; i32 color ; f32 size_px ; u8 flags(1=bold,2=center)
//              ; u16 nchars ; nchars*u16 (UTF-16LE)
#include <windows.h>
#include <d2d1.h>
#include <d2d1helper.h>
#include <dwrite.h>
#include <wincodec.h>
#include <map>
#include <cstring>
#include <cstdint>

#pragma comment(lib, "d2d1.lib")
#pragma comment(lib, "dwrite.lib")
#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "user32.lib")

static ID2D1Factory*   g_d2d = nullptr;
static IDWriteFactory* g_dw  = nullptr;
static std::map<uint64_t, IDWriteTextFormat*> g_formats;   // (size,bold,center) -> format

static bool ensure_factories() {
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);   // ignore RPC_E_CHANGED_MODE / S_FALSE
    if (!g_d2d && FAILED(D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, &g_d2d)))
        return false;
    if (!g_dw && FAILED(DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED,
            __uuidof(IDWriteFactory), reinterpret_cast<IUnknown**>(&g_dw))))
        return false;
    return true;
}

// --- little-endian buffer readers (advance the cursor) ---
static float    rf(const uint8_t* p, size_t& i) { float v;    memcpy(&v, p + i, 4); i += 4; return v; }
static int32_t  ri(const uint8_t* p, size_t& i) { int32_t v;  memcpy(&v, p + i, 4); i += 4; return v; }
static uint16_t ru(const uint8_t* p, size_t& i) { uint16_t v; memcpy(&v, p + i, 2); i += 2; return v; }
static uint8_t  rb(const uint8_t* p, size_t& i) { return p[i++]; }

static D2D1_COLOR_F col(int32_t c) {
    return D2D1::ColorF(((c >> 16) & 0xff) / 255.f, ((c >> 8) & 0xff) / 255.f, (c & 0xff) / 255.f);
}

// One trimmed, vertically-centered text format per (size, bold, center). DirectWrite
// does the ellipsis clipping, so Python ships the full string + the cell width and the
// GPU side elides -- no per-cell measure loop like the Tk/Qt backends need.
static IDWriteTextFormat* get_format(float size, bool bold, bool center) {
    uint64_t key = ((uint64_t)(int)(size * 10) << 2) | (bold ? 2 : 0) | (center ? 1 : 0);
    auto it = g_formats.find(key);
    if (it != g_formats.end()) return it->second;
    IDWriteTextFormat* f = nullptr;
    g_dw->CreateTextFormat(L"Segoe UI", nullptr,
        bold ? DWRITE_FONT_WEIGHT_BOLD : DWRITE_FONT_WEIGHT_NORMAL,
        DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_STRETCH_NORMAL, size, L"", &f);
    if (f) {
        f->SetParagraphAlignment(DWRITE_PARAGRAPH_ALIGNMENT_CENTER);
        f->SetTextAlignment(center ? DWRITE_TEXT_ALIGNMENT_CENTER : DWRITE_TEXT_ALIGNMENT_LEADING);
        f->SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP);
        IDWriteInlineObject* sign = nullptr;
        g_dw->CreateEllipsisTrimmingSign(f, &sign);
        DWRITE_TRIMMING trim = { DWRITE_TRIMMING_GRANULARITY_CHARACTER, 0, 0 };
        f->SetTrimming(&trim, sign);
        if (sign) sign->Release();
        g_formats[key] = f;
    }
    return f;
}

// Walk the packed op buffer, issuing D2D draw calls onto rt. Target-agnostic:
// the same routine serves the offscreen self-test and (phase 2) the live HWND.
static void draw_ops(ID2D1RenderTarget* rt, const uint8_t* p, size_t n) {
    ID2D1SolidColorBrush* br = nullptr;
    rt->CreateSolidColorBrush(D2D1::ColorF(0, 0, 0), &br);
    size_t i = 0;
    while (i < n) {
        char t = (char)p[i++];
        if (t == 'R') {
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            int32_t fill = ri(p, i), ol = ri(p, i); float lw = rf(p, i);
            D2D1_RECT_F rc = D2D1::RectF(x, y, x + w, y + h);
            if (fill >= 0) { br->SetColor(col(fill)); rt->FillRectangle(rc, br); }
            if (ol >= 0)   { br->SetColor(col(ol));   rt->DrawRectangle(rc, br, lw); }
        } else if (t == 'L') {
            float x1 = rf(p, i), y1 = rf(p, i), x2 = rf(p, i), y2 = rf(p, i);
            int32_t c = ri(p, i); float lw = rf(p, i);
            br->SetColor(col(c));
            rt->DrawLine(D2D1::Point2F(x1, y1), D2D1::Point2F(x2, y2), br, lw);
        } else if (t == 'P') {
            int32_t c = ri(p, i); uint16_t np = ru(p, i);
            if (np >= 1) {
                ID2D1PathGeometry* g = nullptr; ID2D1GeometrySink* s = nullptr;
                g_d2d->CreatePathGeometry(&g); g->Open(&s);
                float x0 = rf(p, i), y0 = rf(p, i);
                s->BeginFigure(D2D1::Point2F(x0, y0), D2D1_FIGURE_BEGIN_FILLED);
                for (int k = 1; k < np; k++) { float x = rf(p, i), y = rf(p, i); s->AddLine(D2D1::Point2F(x, y)); }
                s->EndFigure(D2D1_FIGURE_END_CLOSED); s->Close(); s->Release();
                br->SetColor(col(c)); rt->FillGeometry(g, br); g->Release();
            }
        } else if (t == 'T') {
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            int32_t c = ri(p, i); float sz = rf(p, i); uint8_t fl = rb(p, i); uint16_t ln = ru(p, i);
            const wchar_t* ws = (const wchar_t*)(p + i); i += (size_t)ln * 2;
            bool bold = fl & 1, center = fl & 2;
            IDWriteTextFormat* f = get_format(sz, bold, center);
            // Pad scales with the font (which scales with zoom) so the text stays the
            // same proportion of the cell at every zoom -- a fixed px pad would eat a
            // growing fraction when zoomed out and trigger a spurious "..." trim.
            float padL = sz * (5.f / 13.f), padR = sz * (4.f / 13.f);
            D2D1_RECT_F rc = center ? D2D1::RectF(x, y, x + w, y + h)
                                    : D2D1::RectF(x + padL, y, x + w - padR, y + h);
            if (f) { br->SetColor(col(c)); rt->DrawText(ws, ln, f, rc, br, D2D1_DRAW_TEXT_OPTIONS_CLIP); }
        } else if (t == 'X') {
            // scrolled text: draw left-aligned at an explicit origin x, clipped to
            // the field rect. For a custom text field that scrolls horizontally --
            // the layout rect is huge (no trimming), the clip does the cutting.
            float x = rf(p, i), y = rf(p, i), w = rf(p, i), h = rf(p, i);
            float ox = rf(p, i); int c = ri(p, i); float sz = rf(p, i); uint16_t ln = ru(p, i);
            const wchar_t* ws = (const wchar_t*)(p + i); i += (size_t)ln * 2;
            IDWriteTextFormat* f = get_format(sz, false, false);
            if (f) {
                rt->PushAxisAlignedClip(D2D1::RectF(x, y, x + w, y + h),
                                        D2D1_ANTIALIAS_MODE_ALIASED);
                br->SetColor(col(c));
                rt->DrawText(ws, ln, f, D2D1::RectF(ox, y, ox + 100000.f, y + h), br);
                rt->PopAxisAlignedClip();
            }
        } else {
            break;   // unknown tag: corrupt buffer, stop rather than run off the end
        }
    }
    if (br) br->Release();
}

// --- live HWND path (phase 2): a WS_CHILD window with an HwndRenderTarget,
// parented into the Tk frame. Same draw_ops() as the self-test; only the target
// differs. Its WndProc runs on Tk's own message pump (same thread), so no
// separate loop -- phase 3 will translate its input messages back to Python. ---
struct Surface { HWND hwnd; ID2D1HwndRenderTarget* rt; };
static const wchar_t* WCLASS = L"FastGridD2DSurface";

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_ERASEBKGND) return 1;              // we own every pixel; no GDI erase flicker
    if (m == WM_PAINT) { ValidateRect(h, nullptr); return 0; }  // present happens in gpu_render
    // Transparent to hit-testing so mouse falls through to the Tk parent frame --
    // Tk then delivers <Button>/<Motion>/<MouseWheel>/<Key> like any widget, and the
    // host reuses the Tk renderer's event wiring instead of a native input bridge.
    if (m == WM_NCHITTEST) return HTTRANSPARENT;
    return DefWindowProc(h, m, w, l);
}

// Force 96 DPI (1 unit == 1 physical pixel). The geometry is already pre-scaled to
// physical px in Python (like the Tk canvas), and Tk mouse events are in physical
// px too -- so the target must NOT re-apply the desktop DPI, or draws land at 2x on
// a 200% display and the selection/dividers offset + thicken vs where you clicked.
static D2D1_RENDER_TARGET_PROPERTIES rt_props() {
    return D2D1::RenderTargetProperties(
        D2D1_RENDER_TARGET_TYPE_DEFAULT,
        D2D1::PixelFormat(DXGI_FORMAT_UNKNOWN, D2D1_ALPHA_MODE_UNKNOWN),
        96.0f, 96.0f);
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

extern "C" __declspec(dllexport)
void* gpu_attach(void* parent, int w, int h) {
    if (!ensure_factories()) return nullptr;
    ensure_class();
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    // WS_CLIPSIBLINGS: the D2D Present must NOT paint over sibling Tk widgets
    // stacked above it (the in-cell editor Entry) -- without it the editor flickers
    // away under the surface. HTTRANSPARENT already lets input reach the editor.
    HWND ch = CreateWindowExW(0, WCLASS, L"", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
                              0, 0, w, h, (HWND)parent, nullptr,
                              GetModuleHandleW(nullptr), nullptr);
    if (!ch) return nullptr;
    ID2D1HwndRenderTarget* rt = nullptr;
    D2D1_HWND_RENDER_TARGET_PROPERTIES hp =
        D2D1::HwndRenderTargetProperties(ch, D2D1::SizeU(w, h));
    if (FAILED(g_d2d->CreateHwndRenderTarget(rt_props(), hp, &rt))) {
        DestroyWindow(ch);
        return nullptr;
    }
    return new Surface{ ch, rt };
}

extern "C" __declspec(dllexport)
void gpu_render(void* sp, const uint8_t* ops, int n, int clear_rgb) {
    Surface* s = (Surface*)sp;
    if (!s || !s->rt) return;
    s->rt->BeginDraw();
    s->rt->Clear(clear_rgb >= 0 ? col(clear_rgb) : D2D1::ColorF(D2D1::ColorF::White));
    draw_ops(s->rt, ops, (size_t)n);
    if (s->rt->EndDraw() == D2DERR_RECREATE_TARGET) {   // device lost -> rebuild target
        s->rt->Release();
        s->rt = nullptr;
        RECT rc; GetClientRect(s->hwnd, &rc);
        D2D1_HWND_RENDER_TARGET_PROPERTIES hp = D2D1::HwndRenderTargetProperties(
            s->hwnd, D2D1::SizeU(rc.right ? rc.right : 1, rc.bottom ? rc.bottom : 1));
        ID2D1HwndRenderTarget* rt = nullptr;
        if (SUCCEEDED(g_d2d->CreateHwndRenderTarget(rt_props(), hp, &rt)))
            s->rt = rt;
    }
}

extern "C" __declspec(dllexport)
void gpu_resize(void* sp, int w, int h) {
    Surface* s = (Surface*)sp;
    if (!s) return;
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    MoveWindow(s->hwnd, 0, 0, w, h, FALSE);
    if (s->rt) s->rt->Resize(D2D1::SizeU(w, h));
}

extern "C" __declspec(dllexport)
void gpu_detach(void* sp) {
    Surface* s = (Surface*)sp;
    if (!s) return;
    if (s->rt) s->rt->Release();
    if (s->hwnd) DestroyWindow(s->hwnd);
    delete s;
}

// Self-test: render ops to an offscreen WIC bitmap and return the ARGB of one pixel
// (0xAARRGGBB). Lets Python assert the draw path is pixel-correct with no window.
// 0xDEADnnnn returns are init failures. NOT the live render path.
extern "C" __declspec(dllexport)
unsigned int gpu_probe_pixel(const uint8_t* ops, int n, int w, int h, int px, int py) {
    if (!ensure_factories()) return 0xDEAD0001;
    IWICImagingFactory* wic = nullptr;
    if (FAILED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(&wic)))) return 0xDEAD0002;
    IWICBitmap* bmp = nullptr;
    if (FAILED(wic->CreateBitmap(w, h, GUID_WICPixelFormat32bppPBGRA,
            WICBitmapCacheOnLoad, &bmp))) { wic->Release(); return 0xDEAD0003; }
    D2D1_RENDER_TARGET_PROPERTIES props = D2D1::RenderTargetProperties(
        D2D1_RENDER_TARGET_TYPE_DEFAULT,
        D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED));
    ID2D1RenderTarget* rt = nullptr;
    if (FAILED(g_d2d->CreateWicBitmapRenderTarget(bmp, props, &rt))) {
        bmp->Release(); wic->Release(); return 0xDEAD0004;
    }
    rt->BeginDraw();
    rt->Clear(D2D1::ColorF(0, 0, 0, 1));   // sentinel: unpainted pixels read back black
    draw_ops(rt, ops, (size_t)n);
    HRESULT hr = rt->EndDraw();
    unsigned int argb = 0;
    if (SUCCEEDED(hr)) {
        BYTE b[4] = { 0, 0, 0, 0 }; WICRect r = { px, py, 1, 1 };   // PBGRA: b0=B b1=G b2=R b3=A
        if (SUCCEEDED(bmp->CopyPixels(&r, 4, 4, b)))
            argb = ((unsigned)b[3] << 24) | ((unsigned)b[2] << 16) | ((unsigned)b[1] << 8) | b[0];
    }
    rt->Release(); bmp->Release(); wic->Release();
    return argb;
}

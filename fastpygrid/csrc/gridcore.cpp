// gridcore: C++ data core for GridModel. Owns ALL cell text (header + data
// rows), the bulk text ops (copy/delete/paste/find), a bulk column fetch (so
// Python's filter/sort/distinct touch one column without per-cell ctypes), and
// undo/redo of cell edits. Python keeps view/filter/sort orchestration and
// presentation metadata (styles/choices/lines/readonly).
//
// Addressing: cells is row-major over (hdr + ndata) rows. A "grid row" gr is
// gr<hdr -> header row gr, else data index di=gr-hdr -> source row via the view
// Python pushes (identity when plain). Undo records (combined_row, col, old, new).
//
// C ABI, ctypes-loaded (mirrors _d2d/surface.dll). Length-prefixed packing is
// used wherever returned strings can contain \t or \n.
#include <vector>
#include <string>
#include <algorithm>
#include <cstring>
#include <cstdint>
#include <cstdlib>
#include <cerrno>
#include <charconv>
#include <thread>
#include <tuple>
#include <cctype>
#include <array>
#include <unordered_map>
#include <unordered_set>

// Undo entry as struct-of-arrays: one flat index per changed cell + the old
// text (moved in from the cell during the op, no copy on the hot path). `nw`
// is populated for paste/set, left EMPTY for a pure delete (all-new == ""), so
// a full-grid delete never allocates 2.4M empty strings. undo/redo copy back
// from the log (the cold path).
struct Edit {
    std::vector<int> idx;                // flat cell index = row*cols + col
    std::vector<std::string> old;        // value before the op
    std::vector<std::string> nw;         // value after (empty => all "", i.e. a delete)
    int tgt_gr = -1, tgt_col = -1;
    int pre_rows = 0, post_rows = 0;     // combined-row count before/after (materialisation)
};

struct Core {
    int cols, hdr;
    std::vector<std::string> d;          // row-major, (hdr+ndata)*cols
    std::vector<int> view;               // source data indices, empty => plain
    bool plain = true;
    std::string out;                     // scratch for returned buffers
    std::vector<Edit> undo, redo;
    Edit cur;                            // edit being accumulated by the current op

    // Sparse per-cell display styles (was Python's _styles). Key packs (src_row, col):
    // data src>=0 follows sort/filter, header src=-1-gr. Value = {fg, bg, bold}; fg/bg are
    // 0xRRGGBB or -1 (unset -> default), bold 0/1. Only styled cells are present.
    std::unordered_map<int64_t, std::array<int, 3>> styles;
    static int64_t skey(int src, int col) { return ((int64_t)src << 20) | (uint32_t)col; }

    int nrows() const { return cols ? (int)(d.size() / cols) : 0; }
    int ndata() const { return nrows() - hdr; }
    std::string& at(int row, int col) { return d[(size_t)row * cols + col]; }
    std::string& at_flat(int i) { return d[i]; }

    // grid row -> combined row index, or -1 if it maps past the data
    int combined(int gr) const {
        if (gr < hdr) return gr;
        int di = gr - hdr;
        int src = plain ? di : (di < (int)view.size() ? view[di] : -1);
        if (src < 0) return -1;
        int row = hdr + src;
        return row < nrows() ? row : -1;
    }
};

static inline bool special(char c) { return c == '\t' || c == '\n' || c == '\r'; }
// ASCII fold only (A-Z -> a-z). Branchless, avoids libc tolower's locale lookup on
// the ~millions-of-chars find/sort/filter hot paths. Non-ASCII bytes pass through,
// matching Python str.lower() on ASCII and C-locale tolower on the rest.
static inline char lc(char c) { return (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c; }

// ASCII-lower lexicographic compare, no allocation. Bytewise on the folded bytes,
// which for UTF-8 equals codepoint order == Python's compare of the .lower() strings
// (ASCII fold only; non-ASCII case may differ, as elsewhere).
static int casecmp(const std::string& a, const std::string& b) {
    size_t n = a.size() < b.size() ? a.size() : b.size();
    for (size_t i = 0; i < n; i++) {
        unsigned char ca = (unsigned char)lc(a[i]), cb = (unsigned char)lc(b[i]);
        if (ca != cb) return ca < cb ? -1 : 1;
    }
    return a.size() < b.size() ? -1 : (a.size() > b.size() ? 1 : 0);
}

// Case-insensitive substring, no allocation. `n` is already lowercased; lowercase
// the haystack on the fly. ASCII fold only (matches gc_find's convention; Python's
// str.lower() is Unicode-aware, so non-ASCII case may differ -- data is ASCII in
// practice). Empty needle -> true (mirrors Python's "" in s).
static bool icontains(const std::string& h, const std::string& n) {
    size_t hn = h.size(), nn = n.size();
    if (nn == 0) return true;
    if (hn < nn) return false;
    for (size_t i = 0; i + nn <= hn; i++) {
        size_t k = 0;
        while (k < nn && lc(h[i + k]) == n[k]) k++;
        if (k == nn) return true;
    }
    return false;
}

// Parse like Python float(s.replace(",","")): strip ALL commas, then the whole
// whitespace-trimmed remainder must be a valid float (strtod consumes it all).
// Handles sign/exp/inf/nan. Diverges only on Python's digit underscores ("1_000"),
// which are rare in numeric grid data.
static bool parse_num(const std::string& s, double& out) {
    const char *b, *e;
    std::string t;
    if (s.find(',') == std::string::npos) {                     // common case: no copy
        b = s.data(); e = b + s.size();
    } else {
        t.reserve(s.size());
        for (char c : s) if (c != ',') t.push_back(c);
        b = t.data(); e = b + t.size();
    }
    while (b < e && isspace((unsigned char)*b)) b++;            // Python float() strips
    while (e > b && isspace((unsigned char)e[-1])) e--;         // surrounding whitespace
    if (b < e && *b == '+') b++;                                // ...and accepts a leading '+'
    if (b >= e) return false;
    double v;
    auto r = std::from_chars(b, e, v);                          // locale-free, fast (vs strtod)
    if (r.ec != std::errc() || r.ptr != e) return false;       // bad parse or trailing garbage
    out = v; return true;
}

// Worker count for a parallel scan of `work` items: serial under a threshold (thread
// setup would dominate small views -- and keeps the fuzz's tiny models single-threaded),
// else the hardware count capped so we don't oversubscribe a big box.
static unsigned nthreads(size_t work) {
    if (work < 20000) return 1;
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    return hw < 8 ? hw : 8;               // cap: enough parallelism, bounded spawn cost
}

// Portable parallel "build then sort" over v[0..n): each chunk BUILDS its own elements
// (build(i) fills v[i]) and std::sorts itself in ONE pass -- one parallel phase instead
// of a separate key-build + sort, fewer thread spawns and the build stays cache-hot into
// the sort. Then adjacent runs merge bottom-up (independent merges run in parallel). No
// TBB (std::execution::par would need it linked on libstdc++/manylinux). Caller runs the
// first slice/merge itself (one fewer spawn). `cmp` must be a strict-weak TOTAL order, so
// the result is unique and identical to a single std::sort -- callers tie-break by data
// index. Serial under nthreads()'s threshold (keeps the fuzz's tiny models single-thread).
template <class T, class Build, class Cmp>
static void psort_build(std::vector<T>& v, Build build, Cmp cmp) {
    size_t n = v.size();
    unsigned Tn = nthreads(n);
    auto do_chunk = [&](size_t lo, size_t hi) {
        for (size_t i = lo; i < hi; i++) build(i);
        std::sort(v.begin() + lo, v.begin() + hi, cmp);
    };
    if (Tn <= 1) { do_chunk(0, n); return; }
    size_t chunk = (n + Tn - 1) / Tn;
    std::vector<std::thread> th;
    for (size_t lo = chunk; lo < n; lo += chunk) {              // spawn all but first slice
        size_t hi = lo + chunk > n ? n : lo + chunk;
        th.emplace_back([&, lo, hi]() { do_chunk(lo, hi); });
    }
    do_chunk(0, chunk > n ? n : chunk);                         // first slice on caller
    for (auto& x : th) x.join();
    for (size_t width = chunk; width < n; width *= 2) {         // bottom-up merge
        std::vector<std::thread> mth;
        size_t first_mid = 0, first_hi = 0;
        bool have_first = false;
        for (size_t lo = 0; lo < n; lo += 2 * width) {
            size_t mid = lo + width > n ? n : lo + width;
            size_t hi = lo + 2 * width > n ? n : lo + 2 * width;
            if (mid >= hi) continue;
            if (!have_first) { first_mid = mid; first_hi = hi; have_first = true; continue; }
            mth.emplace_back([&, lo, mid, hi]() {
                std::inplace_merge(v.begin() + lo, v.begin() + mid, v.begin() + hi, cmp); });
        }
        if (have_first)                                          // first merge on caller
            std::inplace_merge(v.begin(), v.begin() + first_mid, v.begin() + first_hi, cmp);
        for (auto& x : mth) x.join();
    }
}

// Iterate a candidate set of data indices. ncand<0 => the full 0..nd-1 range (no
// filter has narrowed it yet); ncand>=0 => exactly cand[0..ncand) (0 = empty set,
// cand may be NULL). Template -> must live outside the extern "C" block below.
template <class F>
static void each_cand(const int* cand, int ncand, int nd, F f) {
    if (ncand < 0) for (int di = 0; di < nd; di++) f(di);
    else           for (int i = 0; i < ncand; i++) f(cand[i]);
}

// append length-prefixed (u32 LE + bytes) to buf
static void pack_str(std::string& buf, const std::string& s) {
    uint32_t n = (uint32_t)s.size();
    buf.append((const char*)&n, 4);
    buf.append(s);
}

// --- little-endian wire writers (mirror GpuCanvas' struct.pack for the R/T ops) ---
static inline void put_f32(std::string& o, float v)   { o.append((const char*)&v, 4); }
static inline void put_i32(std::string& o, int32_t v) { o.append((const char*)&v, 4); }
static inline void put_u16(std::string& o, uint16_t v){ o.append((const char*)&v, 2); }

// Decode UTF-8 -> UTF-16 code units (surrogate pairs for astral). Matches Python's
// str.encode('utf-16-le') of the same text, so gc_paint_body's 'T' bytes are identical
// to GpuCanvas.text()'s. Invalid bytes -> U+FFFD (data is valid UTF-8 in practice).
static void utf8_to_utf16(const std::string& s, std::vector<uint16_t>& out) {
    size_t i = 0, n = s.size();
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        uint32_t cp; int len;
        if (c < 0x80)            { cp = c;        len = 1; }
        else if ((c >> 5) == 0x6){ cp = c & 0x1f; len = 2; }
        else if ((c >> 4) == 0xe){ cp = c & 0x0f; len = 3; }
        else if ((c >> 3) == 0x1e){cp = c & 0x07; len = 4; }
        else                     { cp = 0xFFFD;  len = 1; }
        if (i + len > n) { cp = 0xFFFD; len = 1; }
        else for (int k = 1; k < len; k++) cp = (cp << 6) | ((unsigned char)s[i + k] & 0x3f);
        i += len;
        if (cp <= 0xFFFF) out.push_back((uint16_t)cp);
        else { cp -= 0x10000; out.push_back((uint16_t)(0xD800 + (cp >> 10)));
                               out.push_back((uint16_t)(0xDC00 + (cp & 0x3FF))); }
    }
}

// `over` at opacity a over opaque base (both 0xRRGGBB) -> 0xRRGGBB. Mirrors paint._blend
// (round-half-up here vs Python's round-half-even: at most 1/255 off, and only the selection
// wash over a styled cell -- cosmetic, not fuzz-checked; the zebra washes stay Python-exact).
static inline int blend_col(int base, int over, double a) {
    int br = (base >> 16) & 0xFF, bg = (base >> 8) & 0xFF, bb = base & 0xFF;
    int orr = (over >> 16) & 0xFF, og = (over >> 8) & 0xFF, ob = over & 0xFF;
    int r = (int)(br * (1 - a) + orr * a + 0.5);
    int g = (int)(bg * (1 - a) + og * a + 0.5);
    int b = (int)(bb * (1 - a) + ob * a + 0.5);
    return (r << 16) | (g << 8) | b;
}

// Merge a style attr triple into c->styles[key] (mask 1=fg,2=bg,4=bold). A missing entry
// starts unset {-1,-1,0} so a partial set (only fg / only bold) leaves the others default.
static void apply_style(Core* c, int src, int col, int fg, int bg, int bold, int mask) {
    int64_t key = Core::skey(src, col);
    auto it = c->styles.find(key);
    if (it == c->styles.end())
        it = c->styles.emplace(key, std::array<int, 3>{-1, -1, 0}).first;
    if (mask & 1) it->second[0] = fg;
    if (mask & 2) it->second[1] = bg;
    if (mask & 4) it->second[2] = bold;
}

#ifdef _WIN32
  #define EXPORT __declspec(dllexport)
#else
  #define EXPORT __attribute__((visibility("default")))
#endif

extern "C" {

EXPORT void* gc_new(int cols, int hdr) {
    Core* c = new Core();
    c->cols = cols; c->hdr = hdr;
    c->d.resize((size_t)hdr * cols);     // header rows start blank, loaded via gc_set_raw
    return c;
}
EXPORT void gc_free(void* h) { delete (Core*)h; }

EXPORT int gc_ndata(void* h) { return ((Core*)h)->ndata(); }

// Grow the COLUMN count (editing a cell past the last column extends
// the sheet). Re-strides the row-major buffer (every row gains blank trailing
// cells) and remaps the undo/redo flat indices (row*cols+col) to the new stride
// so history survives the widen. No-op if new_cols <= cols. No undo entry itself.
EXPORT void gc_grow_cols(void* h, int new_cols) {
    Core* c = (Core*)h;
    int old = c->cols;
    if (new_cols <= old) return;
    int rows = c->nrows();
    std::vector<std::string> nd((size_t)rows * new_cols);
    for (int r = 0; r < rows; r++)
        for (int col = 0; col < old; col++)
            nd[(size_t)r * new_cols + col] = std::move(c->at(r, col));
    c->d.swap(nd);
    c->cols = new_cols;
    auto remap = [&](std::vector<Edit>& st) {
        for (auto& e : st)
            for (auto& i : e.idx) i = (i / old) * new_cols + (i % old);
    };
    remap(c->undo); remap(c->redo);
}

// Bulk-load data rows from a length-prefixed buffer (u32 len + bytes per cell,
// row-major, nrows*cols cells). Safe for any bytes (tabs/newlines in cells).
EXPORT void gc_load_packed(void* h, const char* buf, int nrows) {
    Core* c = (Core*)h;
    c->d.assign((size_t)(c->hdr + nrows) * c->cols, std::string());
    const char* p = buf;
    int total = nrows * c->cols;
    for (int i = 0; i < total; i++) {
        uint32_t n; memcpy(&n, p, 4); p += 4;
        c->d[(size_t)c->hdr * c->cols + i].assign(p, n); p += n;
    }
}

EXPORT const char* gc_cell(void* h, int row, int col) {
    Core* c = (Core*)h;
    if (row >= 0 && row < c->nrows() && col >= 0 && col < c->cols)
        return c->at(row, col).c_str();
    return "";
}
// low-level set by combined row (no undo), used for header load, materialise, replay
EXPORT int gc_set_raw(void* h, int row, int col, const char* s) {
    Core* c = (Core*)h;
    if (row < 0 || row >= c->nrows() || col < 0 || col >= c->cols) return 0;
    std::string& cell = c->at(row, col);
    if (cell == s) return 0;
    cell.assign(s);
    return 1;
}

EXPORT void gc_set_view(void* h, const int* arr, int n) {
    Core* c = (Core*)h;
    if (n < 0) { c->plain = true; c->view.clear(); }
    else { c->plain = false; c->view.assign(arr, arr + n); }
}

// ---- undo bookkeeping (hot path: move the old cell text into the log, no copy) ----
static inline void begin(Core* c, int tgt_gr, int tgt_col) {
    c->cur.idx.clear(); c->cur.old.clear(); c->cur.nw.clear();
    c->cur.tgt_gr = tgt_gr; c->cur.tgt_col = tgt_col;
    c->cur.pre_rows = c->nrows();
}
static inline void reserve(Core* c, int n) {     // avoid the ~20 reallocations of a growing log
    c->cur.idx.reserve(n); c->cur.old.reserve(n);
}
static inline void rec_del(Core* c, int flat, std::string& cell) {   // nw stays empty (all "")
    c->cur.idx.push_back(flat);
    c->cur.old.push_back(std::move(cell));       // steal the cell's text, caller clears
}
static inline void rec_set(Core* c, int flat, std::string old, std::string nw) {
    c->cur.idx.push_back(flat);
    c->cur.old.push_back(std::move(old));
    c->cur.nw.push_back(std::move(nw));
}
static int commit(Core* c) {                     // returns #changes
    if (c->cur.idx.empty()) return 0;
    c->cur.post_rows = c->nrows();
    c->undo.push_back(std::move(c->cur));
    if (c->undo.size() > 200) c->undo.erase(c->undo.begin());
    c->redo.clear();
    return (int)c->undo.back().idx.size();
}

// ---- COPY: grid rect -> TSV (\t\n\r in a cell -> space) ----
EXPORT const char* gc_copy(void* h, int r1, int c1, int r2, int c2, int* out_len) {
    Core* c = (Core*)h;
    if (c1 < 0) c1 = 0;
    if (c2 >= c->cols) c2 = c->cols - 1;
    if (r1 < 0) r1 = 0;
    std::string& o = c->out; o.clear();
    for (int gr = r1; gr <= r2; gr++) {
        if (gr > r1) o.push_back('\n');
        int row = c->combined(gr);
        for (int col = c1; col <= c2; col++) {
            if (col > c1) o.push_back('\t');
            if (row < 0) continue;                // blank pad row
            for (char ch : c->at(row, col)) o.push_back(special(ch) ? ' ' : ch);
        }
    }
    *out_len = (int)o.size();
    return o.data();
}

// Batch-read the text of a viewport block in ONE call: for each requested data row
// (grid index, view-resolved via combined()) x each requested column, emit u32 byte-
// length + UTF-8 bytes, row-major. Pad/oob cells emit length 0. Lets the renderer
// prefetch a whole frame's cell text with a single FFI instead of one gc_cell per
// cell (~1900/frame). Returns c->out (valid until the next call that reuses it).
EXPORT const char* gc_block(void* h, const int* rows, int nr,
                                           const int* cols, int nc, int* out_len) {
    Core* c = (Core*)h;
    std::string& o = c->out; o.clear();
    for (int i = 0; i < nr; i++) {
        int row = c->combined(rows[i]);
        for (int j = 0; j < nc; j++) {
            int col = cols[j];
            uint32_t n = 0;
            const std::string* s = nullptr;
            if (row >= 0 && col >= 0 && col < c->cols) { s = &c->at(row, col); n = (uint32_t)s->size(); }
            o.append((const char*)&n, 4);
            if (n) o.append(*s);
        }
    }
    *out_len = (int)o.size();
    return o.data();
}

// ---- per-cell STYLES (fg/bg/bold), owned by the core (was Python's _styles) ----
// Keyed (src_row, col): data src>=0 follows sort/filter, header src=-1-gr. Colors are
// 0xRRGGBB or -1 (unset). gc_paint_body reads this every frame; color filter/sort/distinct
// below run over the same map, so those no longer need Python.
EXPORT void gc_set_style(void* h, int src, int col, int fg, int bg, int bold, int mask) {
    apply_style((Core*)h, src, col, fg, bg, bold, mask);
}
// Bulk apply n records of [src,col,fg,bg,bold,mask] in one call -- kills the per-call FFI
// cost of styling many cells at once (e.g. demo startup).
EXPORT void gc_set_styles(void* h, const int* recs, int n) {
    Core* c = (Core*)h;
    for (int i = 0; i < n; i++) { const int* e = recs + i * 6;
        apply_style(c, e[0], e[1], e[2], e[3], e[4], e[5]); }
}
EXPORT int gc_get_style(void* h, int src, int col, int* out3) {
    Core* c = (Core*)h;
    auto it = c->styles.find(Core::skey(src, col));
    if (it == c->styles.end()) { out3[0] = -1; out3[1] = -1; out3[2] = 0; return 0; }
    out3[0] = it->second[0]; out3[1] = it->second[1]; out3[2] = it->second[2];
    return 1;
}

// Color filter: keep candidate data rows whose col cell has fg/bg (which 0=fg,1=bg) equal
// to `color` (0xRRGGBB, or -1 = uncolored -> matches an unset attr / no style). Mirrors
// a cell's fg/bg (which 0/1) == color (uncolored == -1). cand = source data indices.
EXPORT const char* gc_style_filter(void* h, int col, int which, int color,
                                   const int* cand, int ncand, int* out_n) {
    Core* c = (Core*)h; int nd = c->ndata();
    std::string& o = c->out; o.clear();
    each_cand(cand, ncand, nd, [&](int di) {
        auto it = c->styles.find(Core::skey(di, col));
        int cc = (it != c->styles.end()) ? it->second[which] : -1;
        if (cc == color) o.append((const char*)&di, 4);
    });
    *out_n = (int)(o.size() / 4);
    return o.data();
}

// Color sort: stable partition, cells matching `color` first (ascending) or last (else).
// Stable partition: match + rest, original order preserved within each group.
EXPORT const char* gc_style_sort(void* h, int col, int which, int color, int ascending,
                                 const int* cand, int ncand, int* out_n) {
    Core* c = (Core*)h; int nd = c->ndata();
    std::string match, rest;
    each_cand(cand, ncand, nd, [&](int di) {
        auto it = c->styles.find(Core::skey(di, col));
        int cc = (it != c->styles.end()) ? it->second[which] : -1;
        (cc == color ? match : rest).append((const char*)&di, 4);
    });
    std::string& o = c->out; o.clear();
    if (ascending) { o.append(match); o.append(rest); }
    else           { o.append(rest);  o.append(match); }
    *out_n = (int)(o.size() / 4);
    return o.data();
}

// Distinct fg/bg colors in a column's DATA cells (header src<0 skipped), ascending. Int
// order == lowercase-hex order, so this matches Python's sorted() of the hex strings.
EXPORT const char* gc_distinct_colors(void* h, int col, int which, int* out_n) {
    Core* c = (Core*)h;
    std::unordered_set<int> seen;
    std::vector<int> vals;
    for (auto& kv : c->styles) {
        int kcol = (int)(kv.first & 0xFFFFF);
        int src = (int)(kv.first >> 20);
        if (kcol != col || src < 0) continue;
        int cc = kv.second[which];
        if (cc >= 0 && seen.insert(cc).second) vals.push_back(cc);
    }
    std::sort(vals.begin(), vals.end());
    std::string& o = c->out; o.clear();
    o.append((const char*)vals.data(), vals.size() * 4);
    *out_n = (int)vals.size();
    return o.data();
}

// Emit the wire ops (one 'R' fill + optional 'T' text per cell) for the visible BODY
// data cells, in paint.py's exact emission order: caller passes columns already ordered
// (scrollable then frozen), each iterated over all visible data rows. The ~1900-cell hot
// loop moved off Python. Styles come from c->styles (keyed source_row,col); the selection
// wash over a styled bg is blended here (sel_tint/sel_wash_a). Find highlight is native
// too (needle already lowered by the caller when !cs) -- match state per cell mirrors
// GridModel.find_state / paint._body_cells precedence: active > selection wash > match.
// Layout mirrors GpuCanvas.rect(x,y,w-1,h-1,fill=bg) + text(x,y,w,h,txt,fg,bold).
//   cols/colx/colw : ncol visible columns (col index, x, width)
//   grs/rowy       : nrow visible data rows (grid index, y)
//   sel            : nsel * [r1,c1,r2,c2]  (normalized selection, grid coords)
//   fscope         : nfscope * [r1,c1,r2,c2] find scope, or NULL (whole grid)
//   active_gr/col  : the active-match cell (grid coords), or -1 (none)
EXPORT const char* gc_paint_body(void* h,
        const int* cols, const float* colx, const float* colw, int ncol,
        const int* grs, const float* rowy, int nrow,
        float row_h, int H, float fpx, float rect_w,
        const int* sel, int nsel, int single_cell,
        int col_txt, int col_zebra, int col_bg, int wash_even, int wash_odd,
        int sel_tint, float sel_wash_a,
        const char* needle, int nlen, int cs,
        const int* fscope, int nfscope, int active_gr, int active_col,
        int find_match, int find_active, int* out_len) {
    Core* c = (Core*)h;
    std::string nd(needle ? needle : "", (needle && nlen > 0) ? nlen : 0);   // pre-lowered if !cs
    std::string& o = c->out; o.clear();
    std::vector<uint16_t> u16;
    for (int k = 0; k < ncol; k++) {
        int col = cols[k]; float x = colx[k], w = colw[k];
        bool valid_col = (col >= 0 && col < c->cols);
        for (int r = 0; r < nrow; r++) {
            int gr = grs[r]; float y = rowy[r];
            int src = c->combined(gr - H);
            bool zeb = ((gr - H) % 2 == 0);
            const std::string* txt = (valid_col && src >= 0) ? &c->at(src, col) : nullptr;
            int fg = col_txt, base = -1, bold = 0;
            if (src >= 0) {
                auto it = c->styles.find(Core::skey(src, col));
                if (it != c->styles.end()) {
                    if (it->second[0] >= 0) fg = it->second[0];
                    base = it->second[1];
                    bold = it->second[2];
                }
            }
            bool washed = false;
            if (!single_cell)
                for (int s = 0; s < nsel; s++) {
                    const int* R = sel + s * 4;
                    if (R[0] <= gr && gr <= R[2] && R[1] <= col && col <= R[3]) { washed = true; break; }
                }
            // find state: 2 = active match, 1 = match, 0 = none (body data cells only)
            int fstate = 0;
            if (active_gr == gr && active_col == col) fstate = 2;
            else if (nlen > 0 && txt && !txt->empty()) {
                bool inscope = true;
                if (fscope && nfscope) {
                    inscope = false;
                    for (int s = 0; s < nfscope; s++) { const int* R = fscope + s * 4;
                        if (R[0] <= gr && gr <= R[2] && R[1] <= col && col <= R[3]) { inscope = true; break; } }
                }
                if (inscope && (cs ? txt->find(nd) != std::string::npos : icontains(*txt, nd)))
                    fstate = 1;
            }
            int bg;
            if (fstate == 2)      bg = find_active;
            else if (washed)      bg = (base >= 0 ? blend_col(base, sel_tint, sel_wash_a)
                                                  : (zeb ? wash_odd : wash_even));
            else if (fstate == 1) bg = find_match;
            else                  bg = (base >= 0 ? base : (zeb ? col_zebra : col_bg));
            o.push_back('R'); put_f32(o, x); put_f32(o, y); put_f32(o, w - 1); put_f32(o, row_h - 1);
            put_i32(o, bg); put_i32(o, -1); put_f32(o, rect_w);
            if (txt && !txt->empty()) {
                u16.clear(); utf8_to_utf16(*txt, u16);
                o.push_back('T'); put_f32(o, x); put_f32(o, y); put_f32(o, w); put_f32(o, row_h);
                put_i32(o, fg); put_f32(o, fpx); o.push_back((char)(uint8_t)(bold ? 1 : 0));
                put_u16(o, (uint16_t)u16.size());
                o.append((const char*)u16.data(), u16.size() * 2);
            }
        }
    }
    *out_len = (int)o.size();
    return o.data();
}

static bool has(const int* a, int n, int v) {
    for (int i = 0; i < n; i++) if (a[i] == v) return true;
    return false;
}

// ---- DELETE: clear grid rects (flat r1,c1,r2,c2,...), skip readonly cols/rows.
// rows are DATA grid indices, source key = source row index. One undo entry.
// Returns #cells changed, first cleared cell written to out_tgt (gr,col).
EXPORT int gc_delete(void* h, const int* rects, int nrects,
                                    const int* ro_cols, int n_rc,
                                    const int* ro_rows, int n_rr, int* out_tgt) {
    Core* c = (Core*)h;
    out_tgt[0] = -1; out_tgt[1] = -1;
    begin(c, -1, -1);
    long long area = 0;                               // upper bound on cells cleared
    for (int s = 0; s < nrects * 4; s += 4)
        area += (long long)(rects[s + 2] - rects[s] + 1) * (rects[s + 3] - rects[s + 1] + 1);
    if (area > 0 && area < (1 << 26)) reserve(c, (int)area);
    bool have_tgt = false;
    for (int s = 0; s < nrects * 4; s += 4) {
        int r1 = rects[s], c1 = rects[s + 1], r2 = rects[s + 2], c2 = rects[s + 3];
        if (c1 < 0) c1 = 0;
        if (c2 >= c->cols) c2 = c->cols - 1;
        if (r1 < 0) r1 = 0;
        for (int gr = r1; gr <= r2; gr++) {
            int row = c->combined(gr);
            if (row < 0) continue;
            int src_key = row;                        // data source row index
            if (n_rr && has(ro_rows, n_rr, src_key)) continue;
            for (int col = c1; col <= c2; col++) {
                if (n_rc && has(ro_cols, n_rc, col)) continue;
                std::string& cell = c->at(row, col);
                if (!cell.empty()) {
                    if (!have_tgt) { out_tgt[0] = gr; out_tgt[1] = col; have_tgt = true; }
                    rec_del(c, row * c->cols + col, cell);
                    cell.clear();
                }
            }
        }
    }
    return commit(c);
}

// ---- PASTE: TSV block at grid (r0,c0), plain-view materialises past data.
// Parses the raw clipboard in C++ (no Python split): trims trailing all-blank
// rows itself and returns the block dims in out_dims[0]=rows, [1]=maxcols. ----
// sel_nr/sel_nc = selection size, a 1x1 clipboard over a bigger selection fills it.
EXPORT int gc_paste(void* h, int r0, int c0, const char* text, int len,
                                   const int* ro_cols, int n_rc,
                                   const int* ro_rows, int n_rr,
                                   int sel_nr, int sel_nc, int* out_dims) {
    Core* c = (Core*)h;
    // trim trailing all-whitespace lines (matches _parse_clip's trailing trim)
    while (len > 0) {
        int ls = len;
        while (ls > 0 && text[ls - 1] != '\n' && text[ls - 1] != '\r') ls--;
        bool blank = true;
        for (int i = ls; i < len; i++) if (!isspace((unsigned char)text[i])) { blank = false; break; }
        if (!blank) break;
        len = ls;
        while (len > 0 && (text[len - 1] == '\n' || text[len - 1] == '\r')) len--;
    }
    out_dims[0] = 0; out_dims[1] = 0;
    if (len == 0) return 0;
    begin(c, r0, c0);
    // 1x1 clipboard (no delimiters left after trim) over a multi-cell selection -> fill
    bool onecell = true;
    for (int i = 0; i < len; i++) if (special(text[i])) { onecell = false; break; }
    if (onecell && (sel_nr > 1 || sel_nc > 1)) {
        std::string v(text, len);
        bool vblank = true;
        for (char ch : v) if (!isspace((unsigned char)ch)) { vblank = false; break; }
        reserve(c, sel_nr * sel_nc);
        for (int i = 0; i < sel_nr; i++) {
            int gr = r0 + i;
            for (int j = 0; j < sel_nc; j++) {
                int col = c0 + j;
                if (col < 0 || col >= c->cols || (n_rc && has(ro_cols, n_rc, col))) continue;
                int row = c->combined(gr);
                if (row < 0 && c->plain && gr >= c->hdr) {
                    if (n_rr && has(ro_rows, n_rr, gr)) continue;
                    if (vblank) continue;
                    int need = gr + 1;
                    if (need > c->nrows()) c->d.resize((size_t)need * c->cols);
                    row = c->combined(gr);
                }
                if (row < 0) continue;
                if (n_rr && has(ro_rows, n_rr, row)) continue;
                std::string& cell = c->at(row, col);
                if (cell != v) { rec_set(c, row * c->cols + col, std::move(cell), v); cell = v; }
            }
        }
        out_dims[0] = sel_nr; out_dims[1] = sel_nc;
        return commit(c);
    }
    int gr = r0, col = c0, nrow = 1, maxc = 0;
    const char* end = text + len;
    const char* field = text;
    auto put = [&](const char* s, const char* e) {
        if (col < 0 || col >= c->cols || (n_rc && has(ro_cols, n_rc, col))) return;
        int row = c->combined(gr);
        if (row < 0 && c->plain && gr >= c->hdr) {          // pad row
            if (n_rr && has(ro_rows, n_rr, gr)) return;      // readonly row -> no materialise
            bool blank = true;                               // GridModel: no materialise for blank
            for (const char* q = s; q < e; q++) if (!isspace((unsigned char)*q)) { blank = false; break; }
            if (blank) return;
            int need = c->hdr + (gr - c->hdr) + 1;
            if (need > c->nrows()) c->d.resize((size_t)need * c->cols);
            row = c->combined(gr);
        }
        if (row < 0) return;
        if (n_rr && has(ro_rows, n_rr, row)) return;      // src key = data source row
        std::string& cell = c->at(row, col);
        std::string nv(s, e - s);
        if (cell != nv) { rec_set(c, row * c->cols + col, std::move(cell), nv); cell = std::move(nv); }
    };
    long long area = (long long)(len / 2 + 1);        // rough upper bound on fields
    if (area > 0 && area < (1 << 26)) reserve(c, (int)area);
    // An empty line (col back at c0, empty field) is zero cells, not one empty
    // cell; matches csv, where a blank line parses to [] and writes nothing.
    for (const char* p = text; p < end; p++) {
        char ch = *p;
        if (ch == '\t') { put(field, p); col++; field = p + 1; }
        else if (ch == '\n' || ch == '\r') {
            if (!(col == c0 && field == p)) put(field, p);
            if (col - c0 + 1 > maxc) maxc = col - c0 + 1;
            gr++; nrow++; col = c0;
            if (ch == '\r' && p + 1 < end && p[1] == '\n') p++;
            field = p + 1;
        }
    }
    if (!(col == c0 && field == end)) put(field, end);   // trailing field ('a\t' -> ['a',''])
    if (col - c0 + 1 > maxc) maxc = col - c0 + 1;         // last line's width
    out_dims[0] = nrow; out_dims[1] = maxc;
    return commit(c);
}

// ---- SET one grid cell (raw string, undo-recorded, readonly + materialise) ----
EXPORT int gc_set_cell(void* h, int gr, int col, const char* s,
                                      const int* ro_cols, int n_rc,
                                      const int* ro_rows, int n_rr) {
    Core* c = (Core*)h;
    if (col < 0 || col >= c->cols) return 0;
    if (n_rc && has(ro_cols, n_rc, col)) return 0;
    int pre = c->nrows();                                 // capture BEFORE any materialise
    int row = c->combined(gr);
    std::string nv(s);
    if (row < 0 && c->plain && gr >= c->hdr) {
        if (n_rr && has(ro_rows, n_rr, gr)) return 0;     // readonly row -> no materialise
        if (nv.empty()) return 0;                         // no growth for a blank write
        int need = gr + 1;
        if (need > c->nrows()) c->d.resize((size_t)need * c->cols);
        row = c->combined(gr);
    }
    if (row < 0) return 0;
    if (n_rr && has(ro_rows, n_rr, row)) return 0;        // src key = data source row
    std::string& cell = c->at(row, col);
    if (cell == nv) return 0;
    begin(c, gr, col);
    c->cur.pre_rows = pre;                                // undo shrinks materialised rows
    rec_set(c, row * c->cols + col, std::move(cell), nv);
    cell = std::move(nv);
    return commit(c);
}

// ---- UNDO / REDO of cell edits. Returns 1 if applied, target in out_tgt.
// Copy (not move) from the log so entries survive repeated undo/redo cycles.
EXPORT int gc_undo(void* h, int* out_tgt) {
    Core* c = (Core*)h;
    if (c->undo.empty()) return 0;
    Edit e = std::move(c->undo.back()); c->undo.pop_back();
    for (size_t k = e.idx.size(); k-- > 0; )
        c->at_flat(e.idx[k]) = e.old[k];
    if (c->nrows() > e.pre_rows) c->d.resize((size_t)e.pre_rows * c->cols);
    out_tgt[0] = e.tgt_gr; out_tgt[1] = e.tgt_col;
    c->redo.push_back(std::move(e));
    return 1;
}
EXPORT int gc_redo(void* h, int* out_tgt) {
    Core* c = (Core*)h;
    if (c->redo.empty()) return 0;
    Edit e = std::move(c->redo.back()); c->redo.pop_back();
    if (c->nrows() < e.post_rows) c->d.resize((size_t)e.post_rows * c->cols);
    bool del = e.nw.empty();                          // pure delete -> new is all ""
    for (size_t k = 0; k < e.idx.size(); k++)
        c->at_flat(e.idx[k]) = del ? std::string() : e.nw[k];
    out_tgt[0] = e.tgt_gr; out_tgt[1] = e.tgt_col;
    c->undo.push_back(std::move(e));
    return 1;
}

// ---- FIND: substring over grid cells. Returns packed (row,col) int pairs. ----
// scope: flat [r1,c1,r2,c2,...] rects, or NULL for full scan. case_sensitive 0/1.
// Walks header rows + data rows in view order (grid-row coords, like paint).
EXPORT const char* gc_find(void* h, const char* needle, int nlen, int cs,
                                          const int* scope, int nscope, int limit,
                                          int* out_count, int* out_capped) {
    Core* c = (Core*)h;
    std::string nd(needle, nlen);
    if (!cs) for (char& ch : nd) ch = (char)tolower((unsigned char)ch);
    std::string& o = c->out; o.clear();
    *out_capped = 0;
    int count = 0;
    int H = c->hdr, ndat = c->ndata(), gtot = H + ndat, nc = c->cols;
    auto emit = [&](int gr, int col) {
        o.append((const char*)&gr, 4); o.append((const char*)&col, 4); count++;
    };
    auto match = [&](int row, int col) -> bool {
        const std::string& v = c->at(row, col);
        if ((int)v.size() < nlen) return false;
        // no per-cell allocation (this runs over every grid cell, ~millions):
        // case-sensitive uses find directly; insensitive folds on the fly.
        return cs ? v.find(nd) != std::string::npos : icontains(v, nd);
    };
    if (scope && nscope) {
        // dedup via a visited flag would need a set, scopes are small rects, so
        // walk each rect and rely on Python passing non-overlapping scope (matches use).
        for (int s = 0; s < nscope; s += 4) {
            int r1 = scope[s], c1 = scope[s + 1], r2 = scope[s + 2], c2 = scope[s + 3];
            if (r1 < 0) r1 = 0; if (c1 < 0) c1 = 0;
            if (r2 >= gtot) r2 = gtot - 1; if (c2 >= nc) c2 = nc - 1;
            for (int gr = r1; gr <= r2; gr++) {
                int row = c->combined(gr);
                if (row < 0) continue;
                for (int col = c1; col <= c2; col++)
                    if (match(row, col)) { emit(gr, col); }
            }
        }
    } else {
        unsigned T = nthreads((size_t)gtot * nc);
        if (T <= 1) {
            for (int gr = 0; gr < gtot; gr++) {
                int row = c->combined(gr);
                if (row < 0) continue;
                for (int col = 0; col < nc; col++)
                    if (match(row, col)) {
                        emit(gr, col);
                        if (count >= limit) { *out_capped = 1; *out_count = count; return o.data(); }
                    }
            }
        } else {
            // Partition data rows across threads; each collects (gr,col) pairs into its
            // own buffer (read-only over the grid, so no locking), then concatenate in
            // row order -> identical bytes to the serial scan. Cap applied after.
            std::vector<std::string> bufs(T);
            std::vector<std::thread> th;
            int chunk = (gtot + (int)T - 1) / (int)T;
            for (unsigned t = 0; t < T; t++) {
                int lo = (int)t * chunk, hi = lo + chunk;
                if (hi > gtot) hi = gtot;
                if (lo >= hi) break;
                th.emplace_back([&, t, lo, hi]() {
                    std::string& b = bufs[t];
                    for (int gr = lo; gr < hi; gr++) {
                        int row = c->combined(gr);
                        if (row < 0) continue;
                        for (int col = 0; col < nc; col++)
                            if (match(row, col)) {
                                b.append((const char*)&gr, 4); b.append((const char*)&col, 4);
                            }
                    }
                });
            }
            for (auto& x : th) x.join();
            for (auto& b : bufs) o.append(b);
            count = (int)(o.size() / 8);
            if (count > limit) { o.resize((size_t)limit * 8); count = limit; *out_capped = 1; }
        }
    }
    *out_count = count;
    return o.data();
}

// ---- VIEW (filter + sort) over DATA rows, off Python's per-cell FFI loop. ----
// Each takes a candidate list of DATA indices (0..ndata-1), or cand=NULL => all
// data rows, and returns the kept/ordered DATA indices packed as int32 into
// c->out (count in *out_n). Chained by CoreModel._rebuild: value filter -> text
// filter -> sort, each fed the previous result. Data index di -> cell at row hdr+di.
// (Color filter/sort now run natively too: gc_style_filter / gc_style_sort above.)

// value filter: keep rows whose col cell text is in `allowed` (packed u32-len + bytes).
EXPORT const char* gc_filter_set(void* h, int col, const char* allowed, int nallowed,
                                 const int* cand, int ncand, int* out_n) {
    Core* c = (Core*)h; int H = c->hdr, nd = c->ndata();
    std::unordered_set<std::string> set; set.reserve(nallowed * 2);
    const char* p = allowed;
    for (int i = 0; i < nallowed; i++) { uint32_t n; memcpy(&n, p, 4); p += 4; set.emplace(p, n); p += n; }
    std::string& o = c->out; o.clear();
    each_cand(cand, ncand, nd, [&](int di) {
        if (set.count(c->at(H + di, col))) o.append((const char*)&di, 4);
    });
    *out_n = (int)(o.size() / 4);
    return o.data();
}

// text filter, ASCII-lower both sides.
// op: 0 contains, 1 equals, 2 not_equals, 3 begins, 4 ends, 5 not_contains.
EXPORT const char* gc_filter_text(void* h, int col, int op, const char* needle, int nlen,
                                  const int* cand, int ncand, int* out_n) {
    Core* c = (Core*)h; int H = c->hdr, nd = c->ndata();
    std::string rhs(needle, nlen); for (char& ch : rhs) ch = lc(ch);
    std::string lhs;
    auto keep = [&](int di) -> bool {
        const std::string& raw = c->at(H + di, col);
        lhs.assign(raw); for (char& ch : lhs) ch = lc(ch);       // reused buffer, no per-cell alloc
        size_t ls = lhs.size(), rs = rhs.size();
        switch (op) {
            case 1: return lhs == rhs;
            case 2: return lhs != rhs;
            case 3: return ls >= rs && lhs.compare(0, rs, rhs) == 0;
            case 4: return ls >= rs && lhs.compare(ls - rs, rs, rhs) == 0;
            case 5: return lhs.find(rhs) == std::string::npos;
            default: return lhs.find(rhs) != std::string::npos;   // contains
        }
    };
    std::string& o = c->out; o.clear();
    each_cand(cand, ncand, nd, [&](int di) { if (keep(di)) o.append((const char*)&di, 4); });
    *out_n = (int)(o.size() / 4);
    return o.data();
}

// sort candidates by col. Semantics: blanks ("") always last in original order;
// text -> stable by ASCII-lower; numeric -> parseable floats first (stable by value),
// then unparsed (original order), then blanks. ascending flips only the compared group
// (reverse=!asc), stably (ties keep original order).
EXPORT const char* gc_sort(void* h, int col, int ascending, int numeric,
                           const int* cand, int ncand, int* out_n) {
    Core* c = (Core*)h; int H = c->hdr, nd = c->ndata();
    auto cell = [&](int di) -> const std::string& { return c->at(H + di, col); };
    size_t ncnd = ncand < 0 ? (size_t)nd : (size_t)ncand;
    auto cdi = [&](size_t i) { return cand ? cand[i] : (int)i; };   // i-th candidate's data index
    // Build each candidate's key AND sort in ONE parallel pass (psort_build). A `rank`
    // in the comparator folds the blank/unparsed tiers to the end -- so no separate
    // serial partition or blank gather. The di tie-break makes the comparator a TOTAL
    // order, so the parallel sort is deterministic and stable (blanks last, then within a
    // tier ties keep original ascending-di order, both directions).
    std::vector<int> out;
    out.reserve(ncnd);
    if (numeric) {
        // rank 0 = parseable float (sorts by value), 1 = non-numeric, 2 = blank. Python
        // puts non-numbers after numbers and blanks last, in original order, never
        // reversed -- so rank orders those tiers identically in both directions.
        struct NK { double v; int di; char rank; };
        std::vector<NK> el(ncnd);
        psort_build(el,
            [&](size_t i) { int di = cdi(i); const std::string& s = cell(di); double v = 0;
                            char rank = s.empty() ? 2 : (parse_num(s, v) ? 0 : 1);
                            el[i] = {rank == 0 ? v : 0.0, di, rank}; },
            [ascending](const NK& a, const NK& b) {
                if (a.rank != b.rank) return a.rank < b.rank;
                if (a.rank == 0 && a.v != b.v) return ascending ? a.v < b.v : a.v > b.v;
                return a.di < b.di; });
        for (auto& e : el) out.push_back(e.di);
    } else {
        // rank 0 = non-blank (sorts by ASCII-lower key), 1 = blank (last, original order).
        struct TK { std::string k; int di; char rank; };
        std::vector<TK> el(ncnd);
        psort_build(el,
            [&](size_t i) { int di = cdi(i); const std::string& s = cell(di);
                            if (s.empty()) { el[i] = {std::string(), di, 1}; return; }
                            std::string k = s; for (char& ch : k) ch = lc(ch);
                            el[i] = {std::move(k), di, 0}; },
            [ascending](const TK& a, const TK& b) {
                if (a.rank != b.rank) return a.rank < b.rank;
                int r = a.k.compare(b.k);
                if (r) return ascending ? r < 0 : r > 0;
                return a.di < b.di; });
        for (auto& e : el) out.push_back(e.di);
    }
    std::string& o = c->out; o.clear();
    o.append((const char*)out.data(), out.size() * 4);
    *out_n = (int)out.size();
    return o.data();
}

}

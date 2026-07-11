"""Palette + cell flags. The ONE place colours live -- both renderers import
these, so the grid looks identical under Qt and Tk.

A dark two-tier header (column-letter band + field-name row) over a light zebra body.
"""

# --- body ---------------------------------------------------------------
BG = "#ffffff"          # body background
ZEBRA = "#f0eee6"       # alternate body row
TXT = "#202020"         # body text
GRID = "#d5d2c8"        # grid lines

# --- header / bands (dark) ---------------------------------------------
LETTER_BG = "#262624"   # column-letter band + void
HEADER_BG = "#1a1917"   # gutter (row numbers) + corner
LETTER_FG = "#c9c6be"   # letter-band / gutter text
FIELD_BG = "#30302e"    # field-name header row
FIELD_FG = "#e8e5dc"    # field-name header text
SEL_HDR = "#635850"     # selected header/gutter highlight

# --- selection / accent ------------------------------------------------
ACCENT = "#c2734d"
SEL_TINT = "#d6d5d1"    # multi-cell selection wash (single cell = no fill)
SEL_RING = "#e07b45"    # selection border
EDIT_SEL = "#f0c8b0"    # in-cell editor text-selection wash (light, keeps black text readable)
DIVIDER = "#000000"     # frozen-pane divider
SECTION = "#000000"     # thick section divider (set_vline / set_hline)
SECTION_W = 2           # its stroke width in px (DPI-scaled by the renderer)

# --- find --------------------------------------------------------------
FIND_MATCH = "#fef08a"  # pale-yellow match
FIND_ACTIVE = "#fbbf24" # amber active match

# --- filter button -----------------------------------------------------
BTN_BG = "#ffffff"
BTN_BORDER = "#b8b8b8"
FUNNEL = "#b45309"      # active-filter funnel
ARROW_SORT = "#c2734d"  # sort arrow when sorted
ARROW_IDLE = "#6a675f"  # sort arrow at rest

# --- cell flags (the `flags` int in a display-list cell tuple) ---------
FLAG_BOLD = 1
FLAG_CENTER = 2

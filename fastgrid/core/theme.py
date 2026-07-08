"""Palette + cell flags. The ONE place colours live -- both renderers import
these, so the grid looks identical under Qt and Tk.

Colours mirror the reference paintgrid grid (revival_qt): a dark two-tier header
(column-letter band + field-name row) over a light zebra body.
"""

# --- body ---------------------------------------------------------------
BG = "#ffffff"          # body background            (C_BODY_BG)
ZEBRA = "#f0eee6"       # alternate body row         (warm)
TXT = "#202020"         # body text                  (_DEFAULT_FG)
GRID = "#d5d2c8"        # grid lines                 (C_GRID)

# --- header / bands (dark) ---------------------------------------------
LETTER_BG = "#262624"   # column-letter band + void  (C_LETTER_BG)
HEADER_BG = "#1a1917"   # gutter (row numbers) + corner (C_HEADER_BG)
LETTER_FG = "#c9c6be"   # letter-band / gutter text  (C_LETTER_FG)
FIELD_BG = "#30302e"    # field-name header row       (theme.PANEL)
FIELD_FG = "#e8e5dc"    # field-name header text      (theme.FG)
SEL_HDR = "#635850"     # selected header/gutter highlight (C_SEL_HDR)

# --- selection / accent ------------------------------------------------
ACCENT = "#c2734d"
SEL_TINT = "#d6d5d1"    # multi-cell selection wash  (neutral grey; single cell = no fill)
SEL_RING = "#e07b45"    # selection border           (C_SEL_RING)
DIVIDER = "#000000"     # frozen-pane divider        (C_DIVIDER)

# --- find --------------------------------------------------------------
FIND_MATCH = "#fef08a"  # pale-yellow match          (C_FIND_MATCH)
FIND_ACTIVE = "#fbbf24" # amber active match         (C_FIND_ACT)

# --- filter button -----------------------------------------------------
BTN_BG = "#ffffff"      # C_BTN_BG
BTN_BORDER = "#b8b8b8"  # C_BTN_BORDER
FUNNEL = "#b45309"      # active-filter funnel       (C_FUNNEL)
ARROW_SORT = "#c2734d"  # sort arrow when sorted     (C_ARROW_SORT)
ARROW_IDLE = "#6a675f"  # sort arrow at rest         (C_ARROW_IDLE)

# --- cell flags (the `flags` int in a display-list cell tuple) ---------
FLAG_BOLD = 1
FLAG_CENTER = 2

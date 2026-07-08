"""Qt demo — the fastgrid Qt renderer (PySide6).

    python scripts/demo_qt.py                 # tabs: Editable + Read-only, 100k rows
    python scripts/demo_qt.py --rows 500000   # stress it
    python scripts/demo_qt.py --view-only     # both tabs read-only
"""
import sys

from _data import HEADERS, COL_W, gen_rows, rows_arg, style_demo


def _tab(editable):
    from fastgrid.core.model import GridModel
    from fastgrid.renderer.qt import QtGrid
    model = GridModel(HEADERS, gen_rows(_tab.rows), editable=editable)
    style_demo(model)                          # per-cell fg/bold/bg showcase
    return QtGrid(model, editable=editable, frozen=2, col_w=COL_W)


def main():
    from PySide6 import QtWidgets
    _tab.rows = rows_arg(sys.argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tabs = QtWidgets.QTabWidget()
    tabs.setWindowTitle("fastgrid (qt) — %s rows" % f"{_tab.rows:,}")
    tabs.resize(980, 620)
    editable = "--view-only" not in sys.argv
    tabs.addTab(_tab(editable), "Editable" if editable else "Read-only")
    tabs.addTab(_tab(False), "Read-only")
    tabs.show()
    app.exec()


if __name__ == "__main__":
    main()

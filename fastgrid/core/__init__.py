"""GUI-free core: model + geometry + selection + display-list paint.
Import nothing from any toolkit here."""
from . import selection, theme
from .model import GridModel
from .geometry import Geometry
from .paint import paint, DisplayList

__all__ = ["GridModel", "Geometry", "paint", "DisplayList", "selection", "theme"]

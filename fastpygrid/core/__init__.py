"""GUI-free core: model + geometry + selection + display-list paint. No toolkit imports."""
from . import selection, theme
from .model import GridModel
from .geometry import Geometry
from .paint import paint, DisplayList

__all__ = ["GridModel", "Geometry", "paint", "DisplayList", "selection", "theme"]

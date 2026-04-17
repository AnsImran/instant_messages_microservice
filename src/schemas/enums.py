"""
Enumerations for Adaptive Card styling options.

These mirror the vocabulary defined by the Adaptive Card 1.4 specification that
Microsoft Teams understands. Using real enums (instead of free-form strings) means:
  * the OpenAPI docs list every valid value,
  * bad inputs are rejected at the schema layer with a clear error,
  * the service layer can switch on the values without worrying about typos.
"""

from enum import Enum


class TextWeight(str, Enum):
    """How heavy the text looks on screen."""

    LIGHTER = "lighter"
    DEFAULT = "default"
    BOLDER  = "bolder"


class TextAlign(str, Enum):
    """Horizontal alignment of text within its container."""

    LEFT   = "left"
    CENTER = "center"
    RIGHT  = "right"


class TextColor(str, Enum):
    """Themed text color names supported by Adaptive Cards in Teams."""

    DEFAULT   = "default"
    ACCENT    = "accent"
    GOOD      = "good"
    WARNING   = "warning"
    ATTENTION = "attention"
    DARK      = "dark"
    LIGHT     = "light"


class TextSize(str, Enum):
    """Size bucket for text (five discrete steps)."""

    SMALL       = "small"
    DEFAULT     = "default"
    MEDIUM      = "medium"
    LARGE       = "large"
    EXTRA_LARGE = "extraLarge"


class BannerStyle(str, Enum):
    """
    Background/emphasis style for a banner Container.

    Teams maps these themed names to colors:
      * attention -> red/orange  (error, urgency)
      * warning   -> amber       (caution)
      * good      -> green       (success)
      * accent    -> blue        (informational)
      * emphasis  -> neutral gray
      * default   -> same background as the rest of the card (no emphasis)
    """

    DEFAULT   = "default"
    EMPHASIS  = "emphasis"
    GOOD      = "good"
    ATTENTION = "attention"
    WARNING   = "warning"
    ACCENT    = "accent"

"""Clip styles (templates) — spec §6.

Each style is a self-contained module in this package that:

  1. defines a ClipStyle subclass,
  2. instantiates it as a module-level `STYLE` constant,
  3. calls register_style(STYLE) at import time.

This __init__ auto-discovers every sibling module, so ADDING A STYLE =
ONE NEW FILE in this package. No API, database-schema, or UI changes are
needed anywhere else. The API exposes the registry via GET /api/styles so
the UI populates itself dynamically.
"""

from app.styles.base import autodiscover  # noqa: E402

_LOADED = autodiscover()

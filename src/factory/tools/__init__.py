"""Importing this package registers every built-in tool.

Tool modules populate ``REGISTRY`` as a side effect of import (see
``registry.py``'s module docstring). Nothing outside the ``tools`` package should
need to import ``builtin`` directly — importing ``factory.tools`` is enough — so
that a spawned agent always sees the full catalogue instead of an empty registry.
"""

from __future__ import annotations

from factory.tools import (
    builtin,  # noqa: F401 - import populates REGISTRY
    discovery,  # noqa: F401 - import populates REGISTRY
    growth,  # noqa: F401 - import populates REGISTRY
)

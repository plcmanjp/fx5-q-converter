"""Converter fail-closed GX Works3 FX5U to GX Works2 Q CSV converter."""

from typing import Any

__all__ = ["ConversionError", "convert"]


def __getattr__(name: str) -> Any:
    """Load the public core API lazily so ``python -m converter.core`` is warning-free."""
    if name in __all__:
        from . import core

        return getattr(core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

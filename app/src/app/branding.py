"""Product-facing identity. Internal project/package identifiers stay stable."""
APP_NAME = "Q-FX Converter"
EXECUTABLE_NAME = "Q-FX-Converter"


def application_version():
    """Read the same VERSION in source and the frozen bundle."""
    from pathlib import Path
    import sys
    import re
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    value = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Invalid application VERSION")
    return value

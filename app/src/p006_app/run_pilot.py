from __future__ import annotations

import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]
SRC = APP_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from p006_app.pilot_app import configure_logging, create_app, load_config


def main() -> int:
    config = load_config()
    configure_logging(config)
    app = create_app(config=config)
    app.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

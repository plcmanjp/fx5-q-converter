"""Standalone Qt entry; no comparison runner, fixture or web server dependency."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from p006_app.qt_app import main
    raise SystemExit(main())

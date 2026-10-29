"""Standalone Qt entry; no comparison runner, fixture or web server dependency."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--gxw-diagnostics-worker":
        from converter.gxw_diagnostics import worker_main
        raise SystemExit(worker_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--gxw-diagnostics":
        from converter.gxw_diagnostics import cli_main
        raise SystemExit(cli_main(sys.argv[2:]))
    from app.qt_app import main
    raise SystemExit(main())

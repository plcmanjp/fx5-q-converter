"""Run the Q-FX Converter native Qt workspace."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app.qt_app import main
    raise SystemExit(main())

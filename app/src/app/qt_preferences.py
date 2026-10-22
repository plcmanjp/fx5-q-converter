"""Local UI preferences contain paths and window size, never approvals or source bytes."""
from pathlib import Path
from PySide6.QtCore import QSettings


class Preferences:
    def __init__(self, path=None):
        self.store = QSettings(str(path), QSettings.Format.IniFormat) if path else QSettings("PLCWorkspace", "P006Qt")

    def recent(self):
        value = self.store.value("recent", [])
        if isinstance(value, str):
            value = [value]
        return [p for p in value[:10] if isinstance(p, str) and len(p) <= 32767 and Path(p).suffix.lower() in {".gx3", ".gxw"}] if isinstance(value, list) else []

    def remember(self, path):
        if path and Path(path).suffix.lower() in {".gx3", ".gxw"}:
            self.store.setValue("recent", [path] + [p for p in self.recent() if p != path][:9])
            self.store.sync()

    def size(self):
        try:
            width, height = int(self.store.value("width", 1050)), int(self.store.value("height", 840))
            return max(640, min(width, 2560)), max(520, min(height, 1440))
        except (TypeError, ValueError):
            return 1050, 840

    def save_size(self, width, height):
        self.store.setValue("width", width)
        self.store.setValue("height", height)
        self.store.sync()

    def clear(self):
        self.store.clear()
        self.store.sync()

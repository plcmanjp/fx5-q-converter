import os
from pathlib import Path

app = Path(SPECPATH).resolve().parent
from p006_app.branding import EXECUTABLE_NAME
from p006_converter.distribution import PUBLIC_RELEASE
version = Path(os.environ["QFX_VERSION_RESOURCE"]).resolve(strict=True)
datas = []
datas.append((str(app/"src/p006_converter/resources/statement-format-widths.json"), "p006_converter/resources"))
for package, directory in (("p006_app", "approved-profiles"), ("p006_converter", "public-runtime" if PUBLIC_RELEASE else "upstream-contracts"),
                           ("p006_converter", "public-upstream"), ("p006_converter", "q-reader")):
    datas.append((str(app/"src"/package/"resources"/directory), f"{package}/resources/{directory}"))
a = Analysis([str(app/"packaging/qt_portable_entry.py")], pathex=[str(app/"src")],
             binaries=[], datas=datas, hiddenimports=["olefile", "p006_app.qt_workflow", "p006_converter.text_conversion"],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["webview", "pythonnet", "clr", "bottle", "tkinterdnd2"],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=EXECUTABLE_NAME, version=str(version),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=EXECUTABLE_NAME)

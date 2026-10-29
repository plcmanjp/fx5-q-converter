import os
from pathlib import Path

app = Path(SPECPATH).resolve().parent
from app.branding import EXECUTABLE_NAME
from converter.distribution import PUBLIC_RELEASE
version = Path(os.environ["QFX_VERSION_RESOURCE"]).resolve(strict=True)
datas = []
datas.append((str(app/"src/converter/resources/statement-format-widths.json"), "converter/resources"))
for package, directory in (("app", "approved-profiles"), ("converter", "public-runtime" if PUBLIC_RELEASE else "upstream-contracts"),
                           ("converter", "public-upstream"), ("converter", "q-reader"),
                           ("converter", "readonly-analysis")):
    datas.append((str(app/"src"/package/"resources"/directory), f"{package}/resources/{directory}"))
a = Analysis([str(app/"packaging/qt_portable_entry.py")], pathex=[str(app/"src")],
             binaries=[], datas=datas, hiddenimports=["olefile", "app.qt_workflow", "converter.text_conversion", "converter.gxw_diagnostics"],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["webview", "pythonnet", "clr", "bottle", "tkinterdnd2"],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=EXECUTABLE_NAME, version=str(version),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=EXECUTABLE_NAME)

"""Build the native Q-FX Converter portable app from a clean, pinned app source."""
import argparse
import hashlib
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP/"src"))
from app.branding import APP_NAME, EXECUTABLE_NAME


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_environment(resource_path):
    """Keep unrelated native tool DLLs out of PyInstaller's PATH search."""
    windows = Path(os.environ["SystemRoot"]).resolve(strict=True)
    roots = (Path(sys.executable).resolve(strict=True).parent,
             Path(sys.base_prefix).resolve(strict=True),
             (windows / "System32").resolve(strict=True), windows)
    return dict(os.environ, QFX_VERSION_RESOURCE=str(resource_path),
                PYTHONPATH=str(APP / "src"), PYTHONHASHSEED="0",
                PATH=os.pathsep.join(dict.fromkeys(map(str, roots))))


def verify_qt_icu(bundle):
    """Reject a same-named ICU DLL that cannot supply Qt's imported symbols."""
    import pefile
    qt = bundle / "_internal/PySide6/Qt6Core.dll"
    with pefile.PE(str(qt)) as pe:
        dependencies = [item for item in pe.DIRECTORY_ENTRY_IMPORT if item.dll.lower() == b"icuuc.dll"]
        if len(dependencies) != 1 or any(item.name is None for item in dependencies[0].imports):
            raise ValueError("Pinned Qt ICU import contract differs")
        required = {item.name for item in dependencies[0].imports}
    icu = bundle / "_internal/icuuc.dll"
    bundled = icu.exists()
    if not bundled:
        icu = Path(os.environ["SystemRoot"]) / "System32/icuuc.dll"
    with pefile.PE(str(icu)) as pe:
        exported = {item.name for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name}
    missing = sorted(item.decode("ascii") for item in required - exported)
    if missing:
        raise ValueError("Qt ICU ABI mismatch; missing exports: " + ", ".join(missing))
    return {"provider": "BUNDLED" if bundled else "WINDOWS_SYSTEM_BUILD_HOST_ONLY",
            "required_exports": len(required), "missing_exports": 0,
            "qt_core_sha256": sha(qt), "icu_sha256": sha(icu),
            "other_computers": "NOT_VERIFIED"}


def source_snapshot():
    changed = subprocess.check_output(["git", "-C", str(APP), "status", "--porcelain", "--untracked-files=normal", "--", "."])
    if changed.strip():
        raise ValueError("Commit app sources before building; tracked and untracked app changes are refused")
    names = subprocess.check_output(["git", "-C", str(APP), "ls-files", "-z", "--", "."]).decode("utf-8").split("\0")
    result = {}
    for name in sorted(names):
        if name:
            refuse_links(APP/name)
            result[name] = sha(APP/name)
    return result


def refuse_links(path):
    # Microsoft CLOUD/CLOUD_1..F tags preserve OneDrive files; name-surrogate tags redirect paths.
    # https://learn.microsoft.com/openspecs/windows_protocols/ms-fscc/c8e77b37-3909-4fe6-a4ea-2b9d423b1ee4
    cloud_tags = {0x9000001A | (n << 12) for n in range(16)}
    for part in (path, *path.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        tag = getattr(metadata, "st_reparse_tag", 0)
        if part.is_symlink() or (getattr(metadata, "st_file_attributes", 0) & 0x400 and tag not in cloud_tags):
            raise ValueError(f"Reparse path refused: {part}")


def reserve_build(label):
    if not re.fullmatch(r"[A-Za-z0-9]+", label):
        raise ValueError("label must be ASCII alphanumeric")
    work = APP/".temp/qt-build"/label
    dest = APP/"dist"
    for target in (work, dest):
        refuse_links(target)
        if target.exists():
            raise FileExistsError(f"Previous build preserved; existing path must be moved explicitly: {target}")
    work.mkdir(parents=True, exist_ok=False)
    dest.mkdir(parents=True, exist_ok=False)
    return work, dest


def version_resource(release):
    if not re.fullmatch(r"\d+\.\d+\.\d+", release):
        raise ValueError("app/VERSION must be a semantic version")
    parts = tuple(map(int, release.split(".")))+(0,)
    if any(n>65535 for n in parts):
        raise ValueError("Version component outside PE range")
    from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo, StringFileInfo,
                                                    StringTable, StringStruct, VarFileInfo, VarStruct)
    fields = {"CompanyName":"plcmanjp", "FileDescription":APP_NAME, "FileVersion":".".join(map(str,parts)),
              "InternalName":EXECUTABLE_NAME, "OriginalFilename":EXECUTABLE_NAME+".exe",
              "ProductName":APP_NAME, "ProductVersion":release}
    return str(VSVersionInfo(ffi=FixedFileInfo(filevers=parts, prodvers=parts, mask=0x3f, flags=0,
                      OS=0x40004, fileType=1, subtype=0, date=(0,0)),
                kids=[StringFileInfo([StringTable("040904B0",[StringStruct(k,v) for k,v in fields.items()])]),
                      VarFileInfo([VarStruct("Translation",[1033,1200])])]))


def verify_pe(exe, release):
    import pefile
    with pefile.PE(str(exe)) as pe:
        strings = {}
        for group in pe.FileInfo:
            for entry in group:
                for table in getattr(entry, "StringTable", []):
                    strings.update({k.decode():v.decode() for k,v in table.entries.items()})
        expected = {"ProductName":APP_NAME,"FileDescription":APP_NAME,"ProductVersion":release,
                    "OriginalFilename":EXECUTABLE_NAME+".exe"}
        if any(strings.get(k)!=v for k,v in expected.items()):
            raise ValueError("Executable version identity differs from product metadata")
        return expected


def bundle_licenses(bundle):
    """Preserve pinned upstream license texts and installed dependency notices offline."""
    root = APP / "packaging/third-party"
    prepared = {}
    for group in ("qtbase", "pyside"):
        folder = root / group
        manifest_path = folder / "UPSTREAM.json"
        refuse_links(manifest_path)
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("tag") != "v6.11.2":
            raise ValueError("Qt license provenance differs from the pinned runtime")
        for name, facts in manifest["files"].items():
            relative = Path(name)
            if relative.parts != (group, relative.name) or relative.suffix != ".txt":
                raise ValueError("Unsafe license manifest path")
            source = root / relative
            refuse_links(source)
            payload = source.read_bytes()
            if len(payload) != facts["bytes"] or hashlib.sha256(payload).hexdigest() != facts["sha256"]:
                raise ValueError("License text differs from pinned source")
            prepared[name] = payload
        prepared[f"{group}/UPSTREAM.json"] = manifest_bytes
    refuse_links(root / "README.md")
    prepared["README.md"] = (root / "README.md").read_bytes()
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    refuse_links(python_license)
    prepared["python-LICENSE.txt"] = python_license.read_bytes()
    dependencies = {}
    for name in ("PySide6-Essentials", "shiboken6", "olefile", "PyInstaller"):
        installed = distribution(name)
        dependencies[name] = {"version": installed.version, "license": installed.metadata.get("License"), "files": []}
        for item in installed.files or ():
            parts = Path(str(item)).parts
            if Path(str(item)).is_absolute() or ".." in parts or not ("licenses" in parts or Path(str(item)).name in {"LICENSE.txt", "COPYING.txt"}):
                continue
            source = Path(installed.locate_file(item))
            refuse_links(source)
            payload = source.read_bytes()
            relative = f"installed/{name}/{Path(str(item)).as_posix()}"
            prepared[relative] = payload
            dependencies[name]["files"].append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest()})
        if not dependencies[name]["files"]:
            raise ValueError(f"Installed license evidence missing: {name}")
    prepared["DEPENDENCIES.json"] = (json.dumps(dependencies, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destination = bundle / "THIRD-PARTY-LICENSES"
    refuse_links(destination)
    destination.mkdir(exist_ok=False)
    for name, payload in prepared.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(payload)
    return {"files": len(prepared), "dependencies": dependencies, "license_inventory": "VERIFIED"}


def main():
    parser = argparse.ArgumentParser(description="Q-FX Converter standalone Qt build")
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if platform.python_version()!="3.12.13" or platform.architecture()[0]!="64bit":
        raise ValueError("Requires Python 3.12.13 x64")
    pins = {"PyInstaller":"6.21.0", "PySide6-Essentials":"6.11.2", "shiboken6":"6.11.2", "olefile":"0.47"}
    if any(version(name)!=value for name,value in pins.items()):
        raise ValueError("Build environment differs from requirements-qt.txt")
    sources = source_snapshot()
    source_commit = subprocess.check_output(["git","-C",str(APP),"rev-parse","HEAD"]).decode().strip()
    release = (APP/"VERSION").read_text(encoding="utf-8").strip()
    resource = version_resource(release)
    work, dest = reserve_build(args.label)
    resource_path = work/"Q-FX-Converter.version.txt"
    resource_path.write_text(resource,encoding="utf-8")
    env = build_environment(resource_path)
    with (work/"pyinstaller.log").open("x",encoding="utf-8") as log:
        subprocess.run([sys.executable,"-B","-m","PyInstaller","--noconfirm","--workpath",str(work),
                        "--distpath",str(dest),str(APP/"packaging/Q-FX-Converter.spec")],
                       env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
    if source_snapshot()!=sources:
        raise ValueError("App sources changed during build; package not accepted")
    bundle_name = f"{EXECUTABLE_NAME}-v{release}"
    bundle = dest/bundle_name
    (dest/EXECUTABLE_NAME).rename(bundle)
    pe_identity = verify_pe(bundle/(EXECUTABLE_NAME+".exe"),release)
    qt_icu = verify_qt_icu(bundle)
    shutil.copyfile(APP/"VERSION",bundle/"VERSION")
    shutil.copyfile(APP/"README.md",bundle/"README.md")
    for name in ("LICENSE", "BUILD.md", "THIRD_PARTY_NOTICES.md"):
        if (APP / name).is_file():
            shutil.copyfile(APP / name, bundle / name)
    licenses = bundle_licenses(bundle)
    info = {"application":APP_NAME,"engine_version":release,"build_label":args.label,"frontend":"Qt Widgets / PySide6",
            "source_commit":source_commit,"source_tree":"clean","entry":"app/packaging/qt_portable_entry.py",
            "default_start":"source selection; no fixture, telemetry or auto publication",
            "web_server":False,"webview":False,"deployment_acceptance":"SEPARATE_RELEASE_VALIDATION","pe_identity":pe_identity,
            "build_path_policy":"PYTHON_AND_WINDOWS_ONLY","qt_icu_abi":qt_icu,
            "third_party_licenses":licenses}
    (bundle/"BUILD-INFO.json").write_text(json.dumps(info,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if source_snapshot() != sources:
        raise ValueError("App sources changed during bundle preparation; package not accepted")
    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    for path in files:
        refuse_links(path)
    manifest = {p.relative_to(bundle).as_posix():{"bytes":p.stat().st_size,"sha256":sha(p)} for p in files}
    archive = dest/f"{bundle_name}.zip"
    with zipfile.ZipFile(archive,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as zipped:
        for path in files:
            zipped.write(path,bundle_name+"/"+path.relative_to(bundle).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None or set(zipped.namelist())!={bundle_name+"/"+name for name in manifest}:
            raise ValueError("ZIP CRC or file inventory mismatch")
        for name,item in manifest.items():
            if hashlib.sha256(zipped.read(bundle_name+"/"+name)).hexdigest()!=item["sha256"]:
                raise ValueError(f"ZIP content mismatch: {name}")
    if source_snapshot() != sources:
        raise ValueError("App sources changed during archive verification; package not accepted")
    metadata = {**info,"pins":pins,"source_inputs":sources,"files":manifest,"zip":archive.name,
                "zip_bytes":archive.stat().st_size,"zip_sha256":sha(archive),
                "exe_sha256":manifest[EXECUTABLE_NAME+".exe"]["sha256"],"zip_crc_all_file_sha":"PASS"}
    (dest/"package.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in metadata.items() if k not in {"files","source_inputs"}},ensure_ascii=False))


if __name__ == "__main__":
    main()

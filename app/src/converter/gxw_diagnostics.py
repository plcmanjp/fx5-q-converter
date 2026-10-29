"""Separate, masked Read-V1 observations. Never grants conversion permission.

The existing conversion readers and their pins remain authoritative. The exact
read-only wheel runs in a fresh process so its top-level module names cannot
replace the conversion reader's modules. This is not an OS resource sandbox.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

SCHEMA = "qfx.gxw-auxiliary-diagnostics/1"
PROFILE = "q06udv-simple-ladder-read-v1"
WORKER_FLAG = "--gxw-diagnostics-worker"
WHEEL_NAME = "gx_project_toolkit_readonly-0.1.0-py3-none-any.whl"
WHEEL_SHA256 = "a2932105e29a30371e472ba33860dfef7d72f71ba86d6b315a4a682a8c2f6e06"
PROVENANCE_SHA256 = "17f00a6a9b0a7411b3cdab886cf76649900df9deec6b2ba3cb12adff305ab42e"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 4096
MAX_WHEEL_BYTES = 1024 * 1024
MAX_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
MODULES = (
    "gxw_ladder_reader", "gxw_pou_devmap", "gxw_reference_ir",
    "gxw_bounded_ole", "gxw_bounded_xml", "gxw_parameter_reader",
    "gxw_project_analysis", "gxw_read_v1", "gxw_analysis_service",
    "gxw_analysis_worker", "gxw_analysis_mcp",
)
# The wheel's complete inventory is pinned above. The local observation API
# does not activate the optional registry service, MCP server or service worker.
OBSERVATION_MODULES = (
    "gxw_ladder_reader", "gxw_pou_devmap", "gxw_reference_ir",
    "gxw_bounded_ole", "gxw_bounded_xml", "gxw_parameter_reader",
    "gxw_project_analysis", "gxw_read_v1",
)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class DiagnosticsError(ValueError):
    """Only a controlled reason code can cross the process boundary."""

    def __init__(self, reason):
        super().__init__(reason if type(reason) is str and _REASON.fullmatch(reason)
                         else "ANALYSIS_REJECTED")


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DiagnosticsError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(body, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(DiagnosticsError("INVALID_JSON_NUMBER")))
    except (ValueError, UnicodeError):
        raise DiagnosticsError("INVALID_JSON") from None


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _read(path, maximum):
    with path.open("rb") as source:
        body = source.read(maximum + 1)
    if len(body) > maximum:
        raise DiagnosticsError("FILE_SIZE_LIMIT")
    return body


def _regular(path):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DiagnosticsError("RESOURCE_NOT_SINGLE_REGULAR_FILE")
    _reject_redirected(path)


def _reject_redirected(path):
    # Cloud-backed regular package files are supported; name-surrogate reparse
    # points and all other redirection tags are refused.
    cloud = {0x9000001A | (n << 12) for n in range(16)}
    for item in (path, *path.parents):
        meta = item.lstat()
        if item.is_symlink() or (getattr(meta, "st_file_attributes", 0) & 0x400
                                 and getattr(meta, "st_reparse_tag", 0) not in cloud):
            raise DiagnosticsError("RESOURCE_LINK_OR_REPARSE_POINT")


def _package(root=None):
    root = root or Path(__file__).with_name("resources") / "readonly-analysis"
    manifest_path, wheel_path = root / "provenance.json", root / WHEEL_NAME
    _regular(manifest_path)
    _regular(wheel_path)
    manifest_body = _read(manifest_path, MAX_WHEEL_BYTES)
    body = _read(wheel_path, MAX_WHEEL_BYTES)
    if _sha(manifest_body) != PROVENANCE_SHA256 or _sha(body) != WHEEL_SHA256:
        raise DiagnosticsError("ANALYSIS_PACKAGE_SHA256_MISMATCH")
    manifest = _json(manifest_body)
    if (manifest["wheel"]["sha256"] != WHEEL_SHA256
            or manifest["wheel"]["bytes"] != len(body)
            or set(manifest["modules"]) != set(MODULES)):
        raise DiagnosticsError("ANALYSIS_PACKAGE_MANIFEST_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or set(names) != set(manifest["entries"]):
            raise DiagnosticsError("ANALYSIS_PACKAGE_INVENTORY_MISMATCH")
        payloads = {}
        for item in entries:
            facts = manifest["entries"][item.filename]
            if (item.file_size > MAX_WHEEL_BYTES or item.is_dir()
                    or item.file_size != facts["bytes"]
                    or (item.external_attr >> 16) & 0o170000 not in (0, 0o100000)):
                raise DiagnosticsError("ANALYSIS_PACKAGE_ENTRY_REJECTED")
            content = archive.read(item)
            if _sha(content) != facts["sha256"]:
                raise DiagnosticsError("ANALYSIS_PACKAGE_ENTRY_SHA256_MISMATCH")
            payloads[item.filename] = content
        for name in MODULES:
            if _sha(payloads[name + ".py"]) != manifest["modules"][name]:
                raise DiagnosticsError("ANALYSIS_MODULE_SHA256_MISMATCH")
    return manifest, payloads


def _base(source_sha256):
    return {"schema": SCHEMA, "source_sha256": source_sha256,
            "state": "NOT_ANALYZED", "reference_state": "NOT_ANALYZED",
            "complete_search": False, "conversion_authority": "NONE",
            "text_disclosure": "MASKED", "profile": PROFILE,
            "artifact_sha256": WHEEL_SHA256, "reason": None, "snapshot": None,
            "engine": None,
            "limits": {"max_input_bytes": MAX_INPUT_BYTES,
                       "max_response_bytes": MAX_RESPONSE_BYTES,
                       "windows_process_tree_job": os.name == "nt",
                       "process_tree_memory_limit_bytes": MAX_WORKER_MEMORY_BYTES if os.name == "nt" else None,
                       "os_time_memory_sandbox": False}}


def _rejected(digest, reason):
    result = _base(digest)
    result["reason"] = str(DiagnosticsError(reason))
    return result


def _masked(value):
    if type(value) is dict:
        for key, item in value.items():
            if key in {"text", "name", "path", "raw_path", "raw_token", "literal", "label", "owner"} and item is not None:
                raise DiagnosticsError("UNMASKED_ANALYSIS_RESULT")
            if key == "subtype" and not (
                    value.get("kind") == "note" and item is None
                    or value.get("kind") == "statement" and item in {None, "s", "i"}):
                raise DiagnosticsError("INVALID_MASKED_TEXT_SUBTYPE")
            _masked(item)
    elif type(value) is list:
        for item in value:
            _masked(item)


def _schema_definition(schema):
    """Accept only the language used by the exact pinned upstream schema."""
    keywords = {"$schema", "$id", "title", "type", "properties", "required",
                "additionalProperties", "items", "minimum", "pattern", "enum",
                "const", "anyOf", "allOf", "if", "then"}
    if type(schema) is not dict or set(schema) - keywords:
        raise DiagnosticsError("ANALYSIS_SCHEMA_KEYWORD_UNSUPPORTED")
    for key, value in schema.items():
        if key == "properties":
            if type(value) is not dict:
                raise DiagnosticsError("ANALYSIS_SCHEMA_DEFINITION_INVALID")
            for child in value.values():
                _schema_definition(child)
        elif key in {"items", "if", "then"}:
            _schema_definition(value)
        elif key in {"anyOf", "allOf"}:
            if type(value) is not list or not value:
                raise DiagnosticsError("ANALYSIS_SCHEMA_DEFINITION_INVALID")
            for child in value:
                _schema_definition(child)
        elif key == "additionalProperties" and type(value) is not bool:
            raise DiagnosticsError("ANALYSIS_SCHEMA_DEFINITION_INVALID")
        elif key == "type":
            names = value if type(value) is list else [value]
            if not names or any(name not in {"object", "array", "string", "integer", "number", "boolean", "null"} for name in names):
                raise DiagnosticsError("ANALYSIS_SCHEMA_DEFINITION_INVALID")


def _schema_matches(value, schema):
    """Draft 2020-12 subset, without references or external resource loading."""
    kinds = {"object": lambda v: type(v) is dict, "array": lambda v: type(v) is list,
             "string": lambda v: type(v) is str, "integer": lambda v: type(v) is int,
             "number": lambda v: type(v) in {int, float} and math.isfinite(v),
             "boolean": lambda v: type(v) is bool, "null": lambda v: v is None}
    if "type" in schema:
        names = schema["type"] if type(schema["type"]) is list else [schema["type"]]
        if not any(kinds[name](value) for name in names):
            return False
    exact = lambda a, b: type(a) is type(b) and a == b
    if "const" in schema and not exact(value, schema["const"]):
        return False
    if "enum" in schema and not any(exact(value, item) for item in schema["enum"]):
        return False
    if type(value) in {int, float} and "minimum" in schema and value < schema["minimum"]:
        return False
    if type(value) is str and "pattern" in schema and re.search(schema["pattern"], value) is None:
        return False
    if type(value) is dict:
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            return False
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return False
        if any(not _schema_matches(value[key], child) for key, child in properties.items() if key in value):
            return False
    if type(value) is list and "items" in schema:
        if any(not _schema_matches(item, schema["items"]) for item in value):
            return False
    if "allOf" in schema and not all(_schema_matches(value, child) for child in schema["allOf"]):
        return False
    if "anyOf" in schema and not any(_schema_matches(value, child) for child in schema["anyOf"]):
        return False
    if "if" in schema and _schema_matches(value, schema["if"]):
        if "then" in schema and not _schema_matches(value, schema["then"]):
            return False
    return True


def _snapshot_schema(payloads):
    name = next(name for name in payloads if name.endswith("/device-references-v1.schema.json"))
    schema = _json(payloads[name])
    _schema_definition(schema)
    return schema


def _verify_result(result, digest):
    if (type(result) is not dict or set(result) != set(_base(digest))
            or result["schema"] != SCHEMA or result["source_sha256"] != digest
            or result["profile"] != PROFILE or result["complete_search"] is not False
            or result["conversion_authority"] != "NONE" or result["text_disclosure"] != "MASKED"
            or result["artifact_sha256"] != WHEEL_SHA256
            or _encode(result["limits"]) != _encode(_base(digest)["limits"])):
        raise DiagnosticsError("WORKER_RESULT_BINDING_MISMATCH")
    if result["state"] == "NOT_ANALYZED":
        if (result["reference_state"] != "NOT_ANALYZED" or result["snapshot"] is not None
                or result["engine"] is not None or type(result["reason"]) is not str
                or _REASON.fullmatch(result["reason"]) is None):
            raise DiagnosticsError("WORKER_RESULT_STATE_MISMATCH")
        return result
    snapshot, engine = result["snapshot"], result["engine"]
    manifest, payloads = _package()
    expected_engine_modules = {name: manifest["modules"][name] for name in
                               ("gxw_project_analysis", "gxw_bounded_ole", "gxw_bounded_xml",
                                "gxw_ladder_reader", "gxw_reference_ir", "gxw_parameter_reader", "gxw_pou_devmap")}
    if (result["state"] != "PARTIAL" or result["reason"] is not None
            or type(snapshot) is not dict
            or snapshot.get("schema") != "gx-project-toolkit.device-references/1"
            or snapshot.get("source_sha256") != digest or snapshot.get("profile") != PROFILE
            or snapshot.get("state") != "PARTIAL" or snapshot.get("complete_search") is not False
            or snapshot.get("reference_state") not in {"OBSERVED", "NOT_ANALYZED"}
            or result["reference_state"] != snapshot["reference_state"]
            or type(engine) is not dict
            or set(engine) != {"schema", "policy_schema", "modules", "olefile_version"}
            or engine.get("modules") != expected_engine_modules
            or engine.get("olefile_version") != "0.47"
            or engine.get("schema") != "plcman.gxw.project-analysis/1"
            or type(engine.get("policy_schema")) is not int or engine["policy_schema"] != 1):
        raise DiagnosticsError("WORKER_RESULT_STATE_MISMATCH")
    if not _schema_matches(snapshot, _snapshot_schema(payloads)):
        raise DiagnosticsError("WORKER_SNAPSHOT_SCHEMA_MISMATCH")
    _masked(snapshot)
    return result


def _run_analysis(request, root):
    digest = request["source_sha256"]
    manifest, payloads = _package()
    if any(name.startswith("gxw_") for name in sys.modules):
        raise DiagnosticsError("ANALYSIS_MODULE_NAMESPACE_ALREADY_LOADED")
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=False)
    for name in MODULES:
        with (runtime / (name + ".py")).open("xb") as target:
            target.write(payloads[name + ".py"])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime))
    imported = {name: importlib.import_module(name) for name in OBSERVATION_MODULES}
    for name, module in imported.items():
        actual = Path(module.__file__)
        if actual.resolve() != (runtime / (name + ".py")).resolve() or _sha(actual.read_bytes()) != manifest["modules"][name]:
            raise DiagnosticsError("ANALYSIS_MODULE_ORIGIN_MISMATCH")
    engine, reader = imported["gxw_project_analysis"], imported["gxw_read_v1"]
    if engine.ENGINE["olefile_version"] != "0.47":
        raise DiagnosticsError("OLEFILE_VERSION_MISMATCH")
    policy = engine.Policy(include_text=False, max_response_bytes=MAX_RESPONSE_BYTES - 65536)
    try:
        body = engine.capture_file(root / "input.gxw", expected_sha256=digest, allow_root=root, policy=policy)
        analysis = engine.analyze_bytes(body, policy=policy)
        literals = {item["literal"] for item in analysis["cpu"]["candidates"]}
        if not literals or not literals <= {"Q06UDV", "Q06UDVCPU"}:
            raise DiagnosticsError("PROFILE_LITERAL_MISSING_OR_AMBIGUOUS")
        snapshot = reader.device_snapshot(analysis, include_text=False)
    except (engine.AnalysisError, reader.ReadV1Error) as error:
        raise DiagnosticsError(str(error)) from None
    result = _base(digest)
    result.update(state="PARTIAL", reference_state=snapshot["reference_state"],
                  snapshot=snapshot, engine=engine.ENGINE)
    _verify_result(result, digest)
    return result


def worker_main(argv=None):
    """Windowed portable entry: request path and exact request SHA, no stdio."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or _HEX.fullmatch(args[1]) is None:
        return 2
    request_path = Path(args[0])
    digest = None
    try:
        if not request_path.is_absolute() or request_path.name != "request.json":
            return 2
        _regular(request_path)
        raw = _read(request_path, MAX_REQUEST_BYTES)
        if _sha(raw) != args[1]:
            return 2
        request = _json(raw)
        if (type(request) is not dict or set(request) != {"schema", "source_sha256", "nonce"}
                or request["schema"] != SCHEMA or type(request["source_sha256"]) is not str
                or _HEX.fullmatch(request["source_sha256"]) is None
                or type(request["nonce"]) is not str or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None):
            return 2
        digest = request["source_sha256"]
        result = _run_analysis(request, request_path.parent)
    except DiagnosticsError as error:
        result = _rejected(digest, str(error))
    except Exception:
        result = _rejected(digest, "INTERNAL_ANALYSIS_FAILURE")
    try:
        response = _encode({"nonce": request["nonce"], "report": result})
        if len(response) > MAX_RESPONSE_BYTES:
            response = _encode({"nonce": request["nonce"], "report": _rejected(digest, "RESPONSE_LIMIT")})
        with request_path.with_name("response.json").open("xb") as target:
            target.write(response)
        return 0
    except Exception:
        return 2


def _worker_command(request_path, digest):
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG, str(request_path), digest]
    # -I removes caller CWD/PYTHONPATH and user site packages from module search.
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from converter.gxw_diagnostics import worker_main;"
            "raise SystemExit(worker_main(sys.argv[2:]))")
    return [sys.executable, "-I", "-B", "-c", code,
            str(Path(__file__).resolve().parents[1]), str(request_path), digest]


def _windows_worker(command, *, cwd, env, timeout):
    """Assign a suspended owned process to a bounded kill-on-close job."""
    import ctypes
    from ctypes import wintypes

    class Startup(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                    ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                    *[(name, wintypes.DWORD) for name in ("x", "y", "width", "height", "xchars", "ychars", "fill", "flags")],
                    ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                    ("reserved_data", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
                    ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]

    class Process(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                    ("process_id", wintypes.DWORD), ("thread_id", wintypes.DWORD)]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ((ctypes.c_void_p, wintypes.LPCWSTR), wintypes.HANDLE),
        "SetInformationJobObject": ((wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD), wintypes.BOOL),
        "CreateProcessW": ((wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                            ctypes.POINTER(Startup), ctypes.POINTER(Process)), wintypes.BOOL),
        "AssignProcessToJobObject": ((wintypes.HANDLE, wintypes.HANDLE), wintypes.BOOL),
        "ResumeThread": ((wintypes.HANDLE,), wintypes.DWORD),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        "GetExitCodeProcess": ((wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "TerminateJobObject": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = arguments, result
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise DiagnosticsError("WORKER_JOB_CREATE_FAILED")
    process, assigned = Process(), False
    try:
        limits = ExtendedLimits()
        # KILL_ON_JOB_CLOSE and a total process-tree committed-memory ceiling.
        # No BREAKAWAY flag: a venv launcher or bootloader child stays in this job.
        limits.basic.flags = 0x2000 | 0x200
        limits.job_memory = MAX_WORKER_MEMORY_BYTES
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise DiagnosticsError("WORKER_JOB_LIMIT_FAILED")
        startup = Startup()
        startup.cb = ctypes.sizeof(startup)
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        environment = ctypes.create_unicode_buffer("\0".join(key + "=" + env[key] for key in sorted(env, key=str.upper)) + "\0\0")
        # CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT.
        if not kernel.CreateProcessW(command[0], line, None, None, False, 0x4 | 0x08000000 | 0x400,
                                     environment, str(cwd), ctypes.byref(startup), ctypes.byref(process)):
            raise DiagnosticsError("WORKER_PROCESS_CREATE_FAILED")
        if not kernel.AssignProcessToJobObject(job, process.process):
            raise DiagnosticsError("WORKER_JOB_ASSIGN_FAILED")
        assigned = True
        if kernel.ResumeThread(process.thread) == 0xFFFFFFFF:
            raise DiagnosticsError("WORKER_PROCESS_RESUME_FAILED")
        waiting = kernel.WaitForSingleObject(process.process, max(1, math.ceil(timeout * 1000)))
        if waiting == 0x102:
            raise subprocess.TimeoutExpired(command, timeout)
        if waiting != 0:
            raise DiagnosticsError("WORKER_PROCESS_WAIT_FAILED")
        status = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(process.process, ctypes.byref(status)):
            raise DiagnosticsError("WORKER_PROCESS_STATUS_FAILED")
        return subprocess.CompletedProcess(command, status.value)
    finally:
        if process.process:
            # A job owns only this newly created suspended process and its
            # descendants. Failure before assignment never resumes the process.
            if assigned:
                kernel.TerminateJobObject(job, 2)
            else:
                kernel.TerminateProcess(process.process, 2)
            kernel.WaitForSingleObject(process.process, 5000)
        if process.thread:
            kernel.CloseHandle(process.thread)
        if process.process:
            kernel.CloseHandle(process.process)
        kernel.CloseHandle(job)


def _run_worker(command, *, cwd, env, timeout):
    if os.name == "nt":
        return _windows_worker(command, cwd=cwd, env=env, timeout=timeout)
    # The product's supported runtime is Windows. This fallback is for offline
    # developer checks only and does not claim the Windows process-tree limits.
    return subprocess.run(command, cwd=cwd, env=env, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def analyze_gxw_bytes(data: bytes, *, expected_sha256: str, timeout_seconds=15.0) -> dict:
    """Observe exact immutable bytes in isolation; failures stay NOT_ANALYZED.

    The caller owns source capture and expected digest. No path, source text or
    inferred comment ownership is returned. An absent reference is not unused.
    """
    if type(expected_sha256) is not str or _HEX.fullmatch(expected_sha256) is None:
        return _rejected(None, "EXPECTED_SHA256_REQUIRED")
    if type(data) is not bytes or not 0 < len(data) <= MAX_INPUT_BYTES:
        return _rejected(expected_sha256, "INPUT_SIZE_OR_TYPE_LIMIT")
    if _sha(data) != expected_sha256:
        return _rejected(expected_sha256, "SHA256_MISMATCH")
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30):
        return _rejected(expected_sha256, "INVALID_TIMEOUT")
    try:
        _package()
        # These are private runtime protocol files, not user output or a cache.
        # The process receives only this captured input and a fixed operation.
        with tempfile.TemporaryDirectory(prefix="qfx-readonly-analysis-") as location:
            root = Path(location).resolve()
            (root / "input.gxw").write_bytes(data)
            request = {"schema": SCHEMA, "source_sha256": expected_sha256, "nonce": uuid.uuid4().hex}
            body = _encode(request)
            path = root / "request.json"
            path.write_bytes(body)
            environment = {k: v for k, v in os.environ.items()
                           if not k.upper().startswith(("PYTHON", "GXW_", "QFX_"))}
            environment.update(PYTHONDONTWRITEBYTECODE="1", TEMP=str(root), TMP=str(root), TMPDIR=str(root))
            completed = _run_worker(_worker_command(path, _sha(body)), cwd=root,
                                    env=environment, timeout=timeout_seconds)
            if completed.returncode != 0:
                raise DiagnosticsError("WORKER_EXIT_FAILURE")
            response_path = root / "response.json"
            _regular(response_path)
            response = _json(_read(response_path, MAX_RESPONSE_BYTES))
            if type(response) is not dict or set(response) != {"nonce", "report"} or response["nonce"] != request["nonce"]:
                raise DiagnosticsError("WORKER_RESPONSE_BINDING_MISMATCH")
            return _verify_result(response["report"], expected_sha256)
    except subprocess.TimeoutExpired:
        return _rejected(expected_sha256, "WORKER_TIMEOUT")
    except DiagnosticsError as error:
        return _rejected(expected_sha256, str(error))
    except Exception:
        return _rejected(expected_sha256, "ANALYSIS_UNAVAILABLE")


def _local_path(value):
    raw = os.fspath(value)
    path = Path(raw)
    if (not isinstance(raw, str) or not raw or raw.startswith(("\\\\", "//"))
            or any(part in {".", ".."} for part in re.split(r"[\\/]", raw))
            or ":" in raw[len(path.drive):] or "\x00" in raw):
        raise DiagnosticsError("LOCAL_PATH_REQUIRED")
    return Path(os.path.abspath(path))


@contextmanager
def _native_handle(path, *, directory=False):
    """Hold local directory identity or immutable input until publication."""
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
    else:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        # Directory siblings may be written; rename/deletion is blocked. The
        # source handle also denies write sharing. OPEN_REPARSE_POINT avoids
        # following a substituted leaf before its handle path is checked.
        handle = create(str(path), 0 if directory else 0x80000000,
                        3 if directory else 1, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise DiagnosticsError("PATH_HANDLE_OPEN_DENIED")
        try:
            class AttributeTag(ctypes.Structure):
                _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]
            information = kernel.GetFileInformationByHandleEx
            information.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            information.restype = wintypes.BOOL
            metadata = AttributeTag()
            if not information(handle, 9, ctypes.byref(metadata), ctypes.sizeof(metadata)):
                raise DiagnosticsError("PATH_HANDLE_METADATA_UNAVAILABLE")
            cloud = {0x9000001A | (n << 12) for n in range(16)}
            if (bool(metadata.attributes & 0x10) != directory
                    or metadata.attributes & 0x400 and metadata.tag not in cloud):
                raise DiagnosticsError("PATH_HANDLE_TYPE_OR_REPARSE_REJECTED")
            final = kernel.GetFinalPathNameByHandleW
            final.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            final.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = final(handle, buffer, len(buffer), 0)
            actual = os.path.normcase(buffer.value.removeprefix("\\\\?\\"))
            if not 0 < length < len(buffer) or actual != os.path.normcase(str(path)):
                raise DiagnosticsError("PATH_HANDLE_IDENTITY_MISMATCH")
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
        finally:
            if handle is not None:
                close(handle)
    try:
        opened, named = os.fstat(descriptor), path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))
        if identity(opened) != identity(named):
            raise DiagnosticsError("PATH_HANDLE_IDENTITY_MISMATCH")
        _reject_redirected(path)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _guard_ancestors(*paths):
    with ExitStack() as stack:
        directories = {parent for path in paths for parent in (path, *path.parents)}
        for directory in sorted(directories, key=lambda path: len(path.parts)):
            _reject_redirected(directory)
            if not directory.is_dir() or directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise DiagnosticsError("DIRECTORY_LINK_OR_MISSING")
            stack.enter_context(_native_handle(directory, directory=True))
        yield


@contextmanager
def _source_snapshot(path, digest):
    _regular(path)
    with _native_handle(path) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= MAX_INPUT_BYTES:
            raise DiagnosticsError("INPUT_SIZE_OR_TYPE_LIMIT")
        blocks, total = [], 0
        while total <= MAX_INPUT_BYTES:
            block = os.read(descriptor, min(1024 * 1024, MAX_INPUT_BYTES + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
        if total > MAX_INPUT_BYTES:
            raise DiagnosticsError("INPUT_SIZE_OR_TYPE_LIMIT")
        body = b"".join(blocks)
        if _sha(body) != digest:
            raise DiagnosticsError("SHA256_MISMATCH")
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if identity(os.fstat(descriptor)) != identity(before) or identity(path.lstat()) != identity(before):
            raise DiagnosticsError("SOURCE_SNAPSHOT_CHANGED")
        yield body
        if identity(os.fstat(descriptor)) != identity(before) or identity(path.lstat()) != identity(before):
            raise DiagnosticsError("SOURCE_SNAPSHOT_CHANGED")


def _status_output(report):
    if sys.stdout is not None:
        print(_encode({key: report[key] for key in ("schema", "source_sha256", "state", "reason",
                                                   "complete_search", "conversion_authority")}).decode("ascii"))


def cli_main(argv=None):
    """Write one new masked JSON report. Existing/source files are preserved."""
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise DiagnosticsError("INVALID_ARGUMENTS")
    parser = Parser(description="GXW의 읽기 전용 보조 관측을 새 JSON 보고서로 저장합니다.")
    parser.add_argument("--source", required=True, help="읽을 GXW 파일")
    parser.add_argument("--sha256", required=True, help="입력 파일의 정확한 SHA-256")
    parser.add_argument("--output", required=True, help="아직 존재하지 않는 JSON 보고서 경로")
    digest = None
    try:
        args = parser.parse_args(argv)
        candidate_digest = args.sha256.lower()
        if _HEX.fullmatch(candidate_digest) is None:
            raise DiagnosticsError("EXPECTED_SHA256_REQUIRED")
        digest = candidate_digest
        source, output = _local_path(args.source), _local_path(args.output)
        if source.suffix.lower() != ".gxw" or output.suffix.lower() != ".json":
            raise DiagnosticsError("GXW_INPUT_AND_JSON_OUTPUT_REQUIRED")
        if os.path.normcase(str(source)) == os.path.normcase(str(output)):
            raise DiagnosticsError("OUTPUT_MUST_NOT_REPLACE_SOURCE")
        with _guard_ancestors(source.parent, output.parent):
            if output.exists() or output.is_symlink():
                raise DiagnosticsError("OUTPUT_ALREADY_EXISTS")
            with _source_snapshot(source, digest) as body:
                report = analyze_gxw_bytes(body, expected_sha256=digest)
                payload = _encode(report) + b"\n"
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise DiagnosticsError("RESPONSE_LIMIT")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(output, flags, 0o600)
                with os.fdopen(descriptor, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
        _status_output(report)
        return 0 if report["state"] == "PARTIAL" else 2
    except DiagnosticsError as error:
        _status_output(_rejected(digest, str(error)))
        return 2
    except (OSError, ValueError):
        _status_output(_rejected(digest, "PATH_OR_IO_REJECTED"))
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] == [WORKER_FLAG]:
        raise SystemExit(worker_main(sys.argv[2:]))
    raise SystemExit(cli_main())

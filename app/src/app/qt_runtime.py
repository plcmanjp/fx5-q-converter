"""Selected Qt app runtime with existing preparation contract and optional local telemetry."""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

from app.preparation_session import Session

def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="", help="Read-only fixture preselection")
    parser.add_argument("--probe-dir", type=Path)
    parser.add_argument("--auto-close", type=float, default=0)
    parser.add_argument("--preferences", type=Path, help="Optional local UI preferences path")
    parser.add_argument("--output-parent", default="", help="Existing parent for new result folders")
    parser.add_argument("--identifier", default="", help="Suggested result folder name")
    parser.add_argument("--statement-policy", choices=("in_plc", "preserve_source"), default="in_plc")
    args = parser.parse_args()
    if args.auto_close and not 5 <= args.auto_close <= 120:
        parser.error("auto-close must be 5..120 seconds")
    return args


class Runtime:
    def __init__(self, args, framework):
        self.args, self.framework = args, framework
        if framework == "QT_WIDGETS":
            from app.qt_workflow import QtSession
            self.session = QtSession()
        else:
            self.session = Session()
        self.closed = False
        self.probe = None
        self.probe_lock = threading.Lock()
        self.last_gui_tick = None
        self.progress_request = None
        self.progress_count = 0
        if args.probe_dir:
            args.probe_dir.mkdir(parents=True, exist_ok=False)
            self.probe = (args.probe_dir / "events.jsonl").open("x", encoding="utf-8")
        self.record("STARTED", framework=framework)

    def record(self, event, **values):
        with self.probe_lock:
            if self.probe:
                self.probe.write(json.dumps({"event": event, "time": time.time(), **values}, ensure_ascii=False) + "\n")
                self.probe.flush()

    def result_record(self, state):
        result = state.get("result")
        if result:
            self.record("RESULT", status=result["status"], direction=result["direction"],
                        source_sha256=result["source_sha256"], output_hashes=result["output_hashes"],
                        programs=[{"name": p["name"], "role": p["role"]} for p in result["programs"]],
                        diagnostics=len(result["diagnostics"]), field=result["field_validation"],
                        official_gx=result["official_gx_validation"], publication=result["file_generation"])
            self.record("NATIVE_RESULT", binding=result.get("binding"), output_dir=result.get("output_dir"),
                        actual_artifacts=result.get("artifacts", []))

    def gui_tick(self):
        if self.probe is None:
            return
        now = time.monotonic()
        if self.last_gui_tick is not None:
            self.record("UI_TICK", gap_seconds=now - self.last_gui_tick)
        self.last_gui_tick = now

    def progress_record(self, state):
        if self.probe is None:
            return
        request = (state["generation"], state["request_id"])
        if request != self.progress_request:
            self.progress_request, self.progress_count = request, 0
        events = state.get("events", [])
        for event in events[self.progress_count:]:
            self.record("PHASE", phase=event["phase"], generation=state["generation"],
                        request_id=state["request_id"], boundary="UI_OBSERVED_WORKER_EVENT")
        self.progress_count = len(events)

    def close(self):
        if self.closed:
            return
        try:
            self.session.close()
            self.closed = True
            self.record("CLEANUP", active=self.session.active is not None, retired=len(self.session.retired))
        finally:
            with self.probe_lock:
                if self.closed and self.probe:
                    self.probe.close()
                    self.probe = None

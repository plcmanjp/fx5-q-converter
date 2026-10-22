"""Opt-in portable QC through the production widgets and worker event path.

Only an explicitly supplied, hash-bound local fixture is processed. No GX or
PLC operations, engine patches, automatic window input, or authority overrides.
Tk scaling here is a layout stress test, not a Windows display-setting test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path


def load_spec(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("QC specification must be a small regular file")
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("QC specification must be an object")
    hold_codes = spec.get("expected_hold_codes")
    if hold_codes is not None and (not isinstance(hold_codes, list) or not hold_codes or
                                  any(not isinstance(code, str) or not code for code in hold_codes)):
        raise ValueError("expected_hold_codes must be a nonempty list of diagnostic codes")
    if any(key not in {"max_m", "max_d"} for key in spec.get("bounds", {})):
        raise ValueError("X/Y ranges are fixed; GUI QC accepts only optional M/D limits")
    if "cancel_first_plan" in spec and type(spec["cancel_first_plan"]) is not bool:
        raise ValueError("cancel_first_plan must be a boolean")
    if "q_source_review" in spec and type(spec["q_source_review"]) is not bool:
        raise ValueError("q_source_review must be a boolean")
    if "complete_text" in spec and type(spec["complete_text"]) is not bool:
        raise ValueError("complete_text must be a boolean")
    if "target_cpu" in spec:
        from p006_converter.fx5_target import validate_cpu
        validate_cpu(spec["target_cpu"])
        if (spec.get("direction") != "Q_TO_FX5" or spec.get("q_source_review")
                or spec.get("complete_text", True) is not True):
            raise ValueError("target_cpu QC requires complete-text Q to FX5")
    if type(spec.get("timeout_seconds", 90)) is not int or not 30 <= spec.get("timeout_seconds", 90) <= 600:
        raise ValueError("timeout_seconds must be an integer from 30 to 600")
    if spec.get("q_source_review") and (spec.get("direction") != "Q_TO_FX5" or spec.get("targets") or spec.get("bounds")):
        raise ValueError("Q source review requires Q input and no mapping")
    if spec.get("direction") not in {"FX5_TO_Q", "Q_TO_FX5"}:
        raise ValueError("invalid QC direction")
    source = Path(spec["source"])
    if not source.is_file() or source.is_symlink() or source.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("invalid QC source")
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != spec["source_sha256"]:
        raise ValueError("QC source hash mismatch")
    if not Path(spec["output_parent"]).is_dir():
        raise ValueError("QC output parent must exist")
    for key in ("xy_settings", "relocation_settings"):
        if key not in spec:
            continue
        if spec.get("q_source_review") or spec.get("targets"):
            raise ValueError("QC settings files cannot combine with source review or inline targets")
        if key == "relocation_settings" and (spec["direction"] != "Q_TO_FX5" or not spec.get("complete_text", True)):
            raise ValueError("QC relocation requires complete-text Q to FX5")
        settings_path = Path(spec[key])
        if (not settings_path.is_file() or settings_path.is_symlink()
                or settings_path.stat().st_size > 1024 * 1024
                or hashlib.sha256(settings_path.read_bytes()).hexdigest() != spec.get(key + "_sha256")):
            raise ValueError("QC settings file hash mismatch")
    return spec


def cancel_first_plan(app: object, output_parent: Path) -> tuple[str, ...]:
    """Exercise production plan/cancel buttons in one Tk callback."""
    before = tuple(sorted(path.name for path in output_parent.iterdir()))
    app.action_button.invoke()
    if app.workflow.stage.value != "PLANNING":
        raise RuntimeError("first plan did not enter PLANNING")
    app.cancel_button.invoke()
    if app.workflow.stage.value != "CANCEL_REQUESTED":
        raise RuntimeError("cancel button did not request plan cancellation")
    return before


def verify_cancelled_plan(app: object, output_parent: Path,
                          before: tuple[str, ...]) -> dict[str, object]:
    """Fail closed unless the cancelled plan left no generated result."""
    stage = app.workflow.stage.value
    if stage != "CANCELLED" or app._busy():
        raise RuntimeError(f"first plan did not finish CANCELLED: {stage}")
    no_result = (app.plan is None and app.publication is None and
                 not app.file_table.get_children() and app.result_dir is None)
    parent_unchanged = tuple(sorted(path.name for path in output_parent.iterdir())) == before
    if not no_result or not parent_unchanged:
        raise RuntimeError("cancelled first plan created or exposed a result")
    return {"requested": True, "terminal_stage": stage,
            "no_generated_result": no_result, "output_parent_unchanged": parent_unchanged}


def main(argv: list[str], config: dict, build_info: dict) -> int:
    from tkinterdnd2 import TkinterDnD
    from p006_app.unified_app import UnifiedApp

    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-qc", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.hold_seconds <= 120:
        raise ValueError("invalid hold duration")
    if args.result.exists() or not args.result.parent.is_dir():
        raise ValueError("QC result must be a new file in an existing parent")
    spec = load_spec(args.workflow_qc)
    root = TkinterDnD.Tk()
    if "tk_scaling" in spec:
        scaling = float(spec["tk_scaling"])
        if not 1 <= scaling <= 4:
            raise ValueError("invalid layout scaling")
        root.tk.call("tk", "scaling", scaling)
    app = UnifiedApp(root, config=config)
    started = time.perf_counter()
    result = {"status": "RUNNING", "mode": "PORTABLE_WIDGET_FLOW" if getattr(sys, "frozen", False) else "SOURCE_WIDGET_FLOW",
              "build_info": build_info, "source_sha256": spec["source_sha256"],
              "direction": spec["direction"], "gx_validation": "NOT_RUN",
              "field_acceptance": "NOT_GRANTED", "steps": [],
              "fixture_bounds": "SYNTHETIC_TEST_ONLY" if spec.get("bounds") else "NOT_PROVIDED",
              "windows_scale_qc": "NOT_CLAIMED"}
    phase = "setup"
    initial_outputs = tuple(sorted(path.name for path in Path(spec["output_parent"]).iterdir()))
    output_before_cancel: tuple[str, ...] | None = None
    heartbeats = 0
    last_tick = time.perf_counter()
    max_tick_gap = 0.0

    def finish(error: Exception | None = None) -> None:
        result["status"] = "FAIL" if error else "PASS"
        if error:
            result["error"] = f"{type(error).__name__}: {error}"
        result["seconds"] = time.perf_counter() - started
        result["event_loop_heartbeats"] = heartbeats
        result["max_event_loop_gap_seconds"] = max_tick_gap
        result["visible"] = bool(root.winfo_viewable())
        result["tk_scaling"] = float(root.tk.call("tk", "scaling"))
        result["source_unchanged"] = hashlib.sha256(Path(spec["source"]).read_bytes()).hexdigest() == spec["source_sha256"]
        with args.result.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        root.after(int(args.hold_seconds * 1000), app._on_close)

    def begin_plan() -> None:
        nonlocal phase, output_before_cancel
        if spec.get("cancel_first_plan", False):
            output_before_cancel = cancel_first_plan(app, Path(spec["output_parent"]))
            result["cancellation"] = {"requested": True, "plan_started_stage": "PLANNING",
                                      "stage_after_request": app.workflow.stage.value}
            phase = "cancel_plan"
        else:
            app.action_button.invoke()
            phase = "plan"

    def begin_relocation_or_plan() -> None:
        nonlocal phase
        if spec.get("relocation_settings"):
            # Only the OS file picker is replaced. Parsing, identity binding,
            # worker dispatch, event handling and plan publication are real.
            from unittest.mock import patch
            with patch("tkinter.filedialog.askopenfilename", return_value=spec["relocation_settings"]):
                app._load_relocation_settings()
            phase = "relocation_settings"
        else:
            begin_plan()

    def step() -> None:
        nonlocal phase, heartbeats, last_tick, max_tick_gap, output_before_cancel
        now = time.perf_counter()
        max_tick_gap = max(max_tick_gap, now - last_tick)
        last_tick = now
        heartbeats += 1
        try:
            if now - started > spec.get("timeout_seconds", 90):
                raise TimeoutError(f"workflow QC timeout at {phase}: {app.status_var.get()}")
            if phase == "setup":
                app.direction_var.set(spec["direction"])
                app.complete_text_var.set(spec.get("complete_text", True))
                app.target_cpu_var.set(spec.get("target_cpu", "FX5U"))
                if spec.get("q_source_review", False):
                    app.q_source_review_box.invoke()
                app.source_var.set(spec["source"])
                app.output_parent_var.set(spec["output_parent"])
                app.identifier_var.set(spec.get("project_identifier", "QC_PROJECT"))
                for key, value in spec.get("bounds", {}).items():
                    app.bound_vars[key].set(str(value))
                app.policy_var.set("range" if spec.get("targets") else spec.get("forward_policy", "numeric"))
                app.action_button.invoke()
                phase = "analysis"
            elif phase == "analysis" and app.analysis is not None and not app._busy():
                result["steps"].append({"phase": "analysis", "seconds": now-started})
                result["analysis_uninspected"] = list(app.analysis.uninspected_scope)
                for group in app.editor.inventory.groups:
                    target = spec.get("targets", {}).get(group.source_start)
                    if target is None:
                        if spec.get("targets"):
                            raise ValueError(f"missing explicit target for {group.source_start}")
                        continue
                    app.editor.table.selection_set(group.rule_id)
                    app.editor._selected()
                    app.editor.target_entry.set(target)
                    app.editor.commit_row()
                if spec.get("targets"):
                    app.editor.apply_current()
                    result["in_memory_settings_sha256"] = app.settings.digest
                if spec.get("xy_settings"):
                    from unittest.mock import patch
                    with patch("tkinter.filedialog.askopenfilename", return_value=spec["xy_settings"]):
                        app.editor.load_settings()
                    phase = "xy_settings"
                else:
                    begin_relocation_or_plan()
            elif phase == "xy_settings" and not app.editor._io_busy:
                if app.editor.loaded_settings_sha256 != spec["xy_settings_sha256"]:
                    raise ValueError("GUI X/Y settings file did not load")
                app.editor.apply_current()
                if app.settings is None:
                    raise ValueError("GUI X/Y settings were not applied")
                result["in_memory_settings_sha256"] = app.settings.digest
                begin_relocation_or_plan()
            elif phase == "relocation_settings" and not app._busy() and not app._job_kind:
                if (app.relocation_settings is None or
                        app.relocation_settings_provenance[1] != spec["relocation_settings_sha256"]):
                    raise ValueError(f"GUI relocation settings file did not load: {app.status_var.get()}")
                result["relocation_settings_sha256"] = app.relocation_settings.digest
                begin_plan()
            elif phase == "cancel_plan":
                stage = app.workflow.stage.value
                if stage == "CANCELLED" and not app._busy():
                    result["cancellation"].update(verify_cancelled_plan(
                        app, Path(spec["output_parent"]), output_before_cancel))
                    result["cancellation"]["seconds"] = now-started
                    result["steps"].append({"phase": "cancel_first_plan", "seconds": now-started})
                    app.action_button.invoke()
                    phase = "plan"
                elif not app._busy() and stage not in {"CANCEL_REQUESTED", "PLANNING"}:
                    raise RuntimeError(f"first plan cancellation failed: {stage}")
            elif phase == "plan" and app.plan is not None and not app._busy():
                if spec.get("expected_hold_codes"):
                    codes = {item.code for item in app.plan.diagnostics}
                    if app.plan.publishable or app.plan.status != "HOLD" or not set(spec["expected_hold_codes"]).issubset(codes):
                        raise ValueError(f"expected HOLD diagnostics absent: {sorted(codes)}")
                    current_outputs = tuple(sorted(path.name for path in Path(spec["output_parent"]).iterdir()))
                    if current_outputs != initial_outputs or app.publication is not None:
                        raise ValueError("HOLD case published an output")
                    result.update(plan_status="HOLD", product_status="HOLD", output_parent_unchanged=True,
                                  diagnostic_codes=sorted(codes), visible_status=app.status_var.get())
                    result["steps"].append({"phase": "expected_hold", "seconds": now-started})
                    finish()
                    return
                if not app.plan.publishable:
                    raise ValueError(f"plan HOLD: {app.plan.required_settings}; {app.plan.diagnostics}")
                result["steps"].append({"phase": "plan", "seconds": now-started})
                app.confirm_box.invoke()
                app.action_button.invoke()
                phase = "publish"
            elif phase == "publish" and app.publication is not None and not app._busy():
                result["steps"].append({"phase": "publish", "seconds": now-started})
                result["output_dir"] = str(app.publication.output_dir)
                result["product_status"] = app.publication.status
                result["plan_status"] = app.plan.status
                result["publication_diagnostics"] = len(app.publication.diagnostics)
                result["visible_diagnostic_rows"] = len(app.diagnostic_table.get_children())
                result["selected_tab"] = "summary" if app.notebook.select() == str(app.summary_tab) else "other"
                result["outputs"] = {p.relative_to(app.publication.output_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in app.publication.artifacts if isinstance(p, Path) and p.is_file()}
                finish()
                return
            elif not app._busy() and getattr(app.workflow.stage, "value", "") in {"FAILED", "CANCELLED", "PARTIAL_PUBLISH"}:
                raise RuntimeError(app.status_var.get())
        except Exception as error:
            finish(error)
            return
        root.after(50, step)

    root.after(100, step)
    root.mainloop()
    return 0 if result["status"] == "PASS" else 1

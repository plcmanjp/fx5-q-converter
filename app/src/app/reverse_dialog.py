"""Small Q-address to FX5U candidate panel for the pilot application."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from converter.q_to_fx5 import QDiagnostic, QPlan, analyze_q_source, plan_q_to_fx5, convert_q_to_fx5
from converter.xy_mapping import Bounds, Profile
from converter.xy_settings import MappingSettings
from app.xy_settings_dialog import MappingSettingsDialog, set_form_busy


def parse_arguments(value: dict[str, str]) -> dict[str, object]:
    """Parse an immutable form copy in a worker, including optional file I/O."""
    settings = (MappingSettings.from_json(Path(value["settings"]).read_bytes(),
                expected_source=Profile.Q, expected_target=Profile.FX5U)
                if value["settings"] else None)
    if bool(value["q_x"]) != bool(value["q_y"]) or bool(value["fx_x"]) != bool(value["fx_y"]):
        raise ValueError("X/Y 최대 주소는 각 CPU마다 X와 Y를 함께 입력해야 합니다")
    return {"source": Path(value["source"]), "output_dir": Path(value["output"]),
            "project_identifier": value["identifier"] or None,
            "max_m": int(value["max_m"]) if value["max_m"] else None,
            "max_d": int(value["max_d"]) if value["max_d"] else None, "settings": settings,
            "q_io_bounds": Bounds(int(value["q_x"]), int(value["q_y"])) if value["q_x"] else None,
            "fx5_io_bounds": Bounds(int(value["fx_x"]), int(value["fx_y"])) if value["fx_x"] else None}


def argument_identity(arguments: dict[str, object]) -> tuple:
    return tuple((key, value.digest if isinstance(value, MappingSettings) else value)
                 for key, value in sorted(arguments.items()))


def preview_form(values: dict[str, str]) -> tuple[QPlan, tuple | None]:
    """Retain source analysis even when unrelated optional settings are broken."""
    try:
        arguments = parse_arguments(values)
    except (ValueError, OSError) as error:
        analysis = analyze_q_source(Path(values["source"]))
        diagnostic = QDiagnostic("SETTINGS_INVALID", "error", "settings", None, None, None,
                                "설정을 읽거나 해석하지 못했습니다", "설정 경로와 숫자 입력을 확인하세요", str(error))
        return QPlan(analysis, (), (*analysis.diagnostics, diagnostic), False, None, None), None
    plan_args = {key: value for key, value in arguments.items() if key != "output_dir"}
    return plan_q_to_fx5(**plan_args), argument_identity(arguments)


def publish_form(values: dict[str, str], approved_plan: tuple[str, tuple]) -> dict:
    arguments = parse_arguments(values)
    if argument_identity(arguments) != approved_plan[1]:
        raise ValueError("설정이 변경됐습니다. 원본 분석 및 점검을 다시 실행하세요.")
    return convert_q_to_fx5(**arguments, expected_source_sha256=approved_plan[0])


def describe_plan(plan: QPlan) -> str:
    """Present inspection and missing settings without implying acceptance."""
    analysis = plan.analysis
    program_count = ("미확인" if not analysis.pous or any("program" in scope for scope in analysis.uninspected_scope)
                     else f"{len(analysis.program_references)}건")
    comment_count = ("미확인" if not analysis.pous or "comment binding" in analysis.uninspected_scope
                     else f"{len(analysis.comment_references)}건")
    lines = ["원본 CPU: 미확인 (파일 확장자로 기종을 확정하지 않습니다)",
             f"프로그램: {', '.join(analysis.pous) or '미확인'}",
             f"판독한 프로그램 참조: {program_count}",
             f"판독한 코멘트 주소: {comment_count}",
             "GX 검증: 미실행 / 현장 수락: 미승인"]
    if analysis.uninspected_scope:
        lines.append("미검사 범위: " + ", ".join(analysis.uninspected_scope))
    if plan.required_settings:
        labels = {"project_identifier": "프로젝트 식별자", "max_m": "대상 M 상한",
                  "max_d": "대상 D 상한", "settings": "X/Y 배정",
                  "q_io_bounds": "원본 Q X/Y 범위", "fx5_io_bounds": "대상 FX5 X/Y 범위"}
        lines.append("필요한 설정: " + ", ".join(labels.get(key, key) for key in plan.required_settings))
    for item in plan.diagnostics:
        location = " / ".join(value for value in (item.pou, item.source_locator, item.operand) if value)
        lines.append(f"{item.code} | {location or '프로젝트'} | {item.reason} | {item.action}")
    if plan.publishable:
        lines.append("생성 예정: " + ", ".join(plan.outputs or ()))
        lines.append("CSV 후보 생성 가능. GX 검증 완료를 뜻하지 않습니다.")
    else:
        lines.append("CSV 생성은 보류됩니다. 위 설정 또는 원본 문제를 확인하세요.")
    return "\n".join(lines)


class ReverseDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Q (X/Y 16진) → FX5U CSV 후보")
        self.minsize(720, 550)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(10, weight=1)
        self.fields: dict[str, tk.StringVar] = {}
        labels = (
            ("source", "Q .gxw 원본 (CPU 미확인)"), ("output", "새 결과 폴더"),
            ("identifier", "프로젝트 식별자"), ("max_m", "사용자 지정 M 최대 주소 (선택)"),
            ("max_d", "사용자 지정 D 최대 주소 (선택)"),
            ("settings", "X/Y 매핑 JSON (X/Y가 있을 때 필수)"),
            ("q_x", "검증된 Q X 최대 주소 (십진수)"),
            ("q_y", "검증된 Q Y 최대 주소 (십진수)"),
            ("fx_x", "검증된 FX5U X 최대 주소 (십진수)"),
            ("fx_y", "검증된 FX5U Y 최대 주소 (십진수)"),
        )
        for row, (key, label) in enumerate(labels):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=4)
            variable = tk.StringVar()
            self.fields[key] = variable
            ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
            if key in {"source", "settings"}:
                ttk.Button(self, text="찾기", command=lambda name=key: self._choose_file(name)).grid(row=row, column=2, padx=8)
            elif key == "output":
                ttk.Button(self, text="부모 폴더", command=self._choose_parent).grid(row=row, column=2, padx=8)
        self.fields["identifier"].set("Q_TO_FX5_CANDIDATE")
        controls = ttk.Frame(self)
        controls.grid(row=10, column=0, columnspan=3, sticky="ew", padx=12, pady=8)
        self.preview_button = ttk.Button(controls, text="원본 분석 및 점검", command=lambda: self._start(False))
        self.preview_button.pack(side="left", padx=(0, 8))
        self.convert_button = ttk.Button(controls, text="새 폴더에 후보 생성", command=lambda: self._start(True))
        self.convert_button.pack(side="left")
        ttk.Button(controls, text="X/Y 구간 편집", command=self._edit_settings).pack(side="left", padx=8)
        self.status = tk.StringVar(value="먼저 원본 파일만 선택해 분석할 수 있습니다. 결과는 GX Works3 검증 전 후보입니다.")
        ttk.Label(self, textvariable=self.status, wraplength=650).grid(row=11, column=0, columnspan=3, sticky="w", padx=12)
        self.details = tk.Text(self, height=12, wrap="word", state="disabled")
        self.details.grid(row=12, column=0, columnspan=3, sticky="nsew", padx=12, pady=8)
        self.rowconfigure(12, weight=1)
        self.events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._revision = 0
        self._approved_plan: tuple[str, tuple] | None = None
        for variable in self.fields.values():
            variable.trace_add('write', self._invalidate)
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.after(100, self._poll)

    def _invalidate(self, *_args) -> None:
        self._revision += 1
        self._approved_plan = None
        self.status.set('입력이 변경됐다. 이전 점검 결과는 무효이며 실행 시 전체를 다시 검사한다.')
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.configure(state='disabled')

    def _close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.status.set('검사 또는 파일 생성 중이다. 완료 후 닫을 수 있다.')
        else:
            self.destroy()

    def _edit_settings(self) -> None:
        try:
            q = Bounds(int(self.fields['q_x'].get()), int(self.fields['q_y'].get()))
            fx = Bounds(int(self.fields['fx_x'].get()), int(self.fields['fx_y'].get()))
            path = self.fields['settings'].get().strip()
            settings = (MappingSettings.from_json(Path(path).read_bytes(), expected_source=Profile.Q,
                        expected_target=Profile.FX5U) if path else None)
            MappingSettingsDialog(self, source=Profile.Q, target=Profile.FX5U,
                source_bounds=q, target_bounds=fx, settings=settings,
                on_saved=lambda chosen: self.fields['settings'].set(chosen))
        except (ValueError, OSError) as error:
            self.status.set(f'X/Y 상한과 설정 확인 필요: {error}')

    def _choose_file(self, key: str) -> None:
        kind = [("GX Works2", "*.gxw")] if key == "source" else [("JSON", "*.json")]
        chosen = filedialog.askopenfilename(parent=self, filetypes=kind)
        if chosen:
            self.fields[key].set(chosen)
            if key == "source" and not self.fields["output"].get():
                path = Path(chosen)
                self.fields["output"].set(str(path.with_name(path.stem + "-fx5-candidate")))

    def _choose_parent(self) -> None:
        chosen = filedialog.askdirectory(parent=self)
        if chosen:
            current = self.fields["output"].get()
            leaf = Path(current).name if current else "q-to-fx5-candidate"
            self.fields["output"].set(str(Path(chosen) / leaf))

    def _arguments(self, *, analysis: bool = False) -> dict[str, object]:
        value = {name: variable.get().strip() for name, variable in self.fields.items()}
        if analysis:
            value = {key: content if key in {"source", "output", "identifier"} else ""
                     for key, content in value.items()}
        return parse_arguments(value)

    def _start(self, publish: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        values = {name: variable.get().strip() for name, variable in self.fields.items()}
        approved_plan = self._approved_plan
        revision = self._revision
        if publish and approved_plan is None:
            self.status.set("원본 분석 및 점검을 먼저 실행하고 현재 계획을 확인하세요.")
            return
        self.status.set("Q 프로젝트 검사 중...")
        self.preview_button.configure(state="disabled")
        self.convert_button.configure(state="disabled")
        set_form_busy(self, True)

        def run() -> None:
            try:
                if publish:
                    report = publish_form(values, approved_plan)
                else:
                    plan, identity = preview_form(values)
                    self.events.put((revision, "plan_ready" if plan.publishable else "plan_hold", (plan, identity)))
                    return
                self.events.put((revision, "candidate", report))
            except Exception as error:
                self.events.put((revision, "hold", f"{type(error).__name__}: {error}"))

        self.worker = threading.Thread(target=run, name="converter-q-to-fx5", daemon=False)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                revision, kind, value = self.events.get_nowait()
                set_form_busy(self, False)
                self.preview_button.configure(state="normal")
                self.convert_button.configure(state="normal")
                if revision != self._revision:
                    continue
                if kind == "hold":
                    self._invalidate()
                    self.status.set(f"보류: {value}")
                elif kind in {"plan_ready", "plan_hold"}:
                    plan, identity = value
                    self._approved_plan = ((plan.analysis.source_sha256, identity) if plan.publishable else None)
                    self.status.set(
                        "원본 분석 및 점검 통과. GX 검증 전 후보입니다." if kind == "plan_ready"
                        else "분석 결과에 확인할 항목이 있습니다. CSV 생성은 보류됩니다.")
                    self.details.configure(state="normal")
                    self.details.delete("1.0", "end")
                    self.details.insert("1.0", describe_plan(plan))
                    self.details.configure(state="disabled")
                else:
                    self._approved_plan = None
                    self.status.set("후보 생성 완료. GX Works3 가져오기, 전체 재빌드, 재내보내기 검증 필요." if kind == "candidate"
                                    else "후보 사전 점검 통과. GX Works3 공식 검증 전 후보입니다.")
                    self.details.configure(state="normal")
                    self.details.delete("1.0", "end")
                    self.details.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
                    self.details.configure(state="disabled")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

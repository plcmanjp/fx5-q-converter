"""GUI preflight and candidate publication for FX5U-to-Q X/Y ranges."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from converter.map_forward_xy import map_forward_xy, plan_forward_xy
from converter.xy_mapping import Bounds, Profile
from converter.xy_settings import MappingSettings
from app.xy_settings_dialog import MappingSettingsDialog, set_form_busy


class ForwardMapDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("FX5U → Q (X/Y 16진) 매핑 후보")
        self.minsize(690, 430)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(9, weight=1)
        self.values: dict[str, tk.StringVar] = {}
        for row, (key, caption) in enumerate((
            ("input", "기존 정상 변환 결과 폴더"), ("output", "새 매핑 결과 폴더"),
            ("settings", "X/Y 매핑 JSON"), ("fx_x", "검증된 FX5U X 최대 주소 (십진수)"),
            ("fx_y", "검증된 FX5U Y 최대 주소 (십진수)"),
            ("q_x", "검증된 Q X 최대 주소 (십진수)"),
            ("q_y", "검증된 Q Y 최대 주소 (십진수)"),
        )):
            ttk.Label(self, text=caption).grid(row=row, column=0, sticky="w", padx=10, pady=4)
            variable = tk.StringVar()
            self.values[key] = variable
            ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=5)
            if key in {"input", "settings"}:
                ttk.Button(self, text="찾기", command=lambda name=key: self._choose(name)).grid(row=row, column=2, padx=8)
        controls = ttk.Frame(self)
        controls.grid(row=7, column=0, columnspan=3, sticky="w", padx=10, pady=10)
        self.preview_button = ttk.Button(controls, text="매핑 사전 점검", command=lambda: self._start(False))
        self.preview_button.pack(side="left", padx=(0, 8))
        self.publish_button = ttk.Button(controls, text="새 폴더에 후보 생성", command=lambda: self._start(True))
        self.publish_button.pack(side="left")
        ttk.Button(controls, text="X/Y 구간 편집", command=self._edit_settings).pack(side="left", padx=8)
        self.status = tk.StringVar(value="완전한 정상 Q 결과와 검증된 CPU 주소 상한을 입력하세요.")
        ttk.Label(self, textvariable=self.status, wraplength=650).grid(row=8, column=0, columnspan=3, sticky="w", padx=10)
        self.details = tk.Text(self, height=12, state="disabled")
        self.details.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=8)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        for variable in self.values.values():
            variable.trace_add('write', self._invalidate)
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.after(100, self._poll)

    def _invalidate(self, *_args) -> None:
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
            fx = Bounds(int(self.values['fx_x'].get()), int(self.values['fx_y'].get()))
            q = Bounds(int(self.values['q_x'].get()), int(self.values['q_y'].get()))
            path = self.values['settings'].get().strip()
            settings = (MappingSettings.from_json(Path(path).read_bytes(), expected_source=Profile.FX5U,
                        expected_target=Profile.Q) if path else None)
            MappingSettingsDialog(self, source=Profile.FX5U, target=Profile.Q,
                source_bounds=fx, target_bounds=q, settings=settings,
                on_saved=lambda chosen: self.values['settings'].set(chosen))
        except (ValueError, OSError) as error:
            self.status.set(f'X/Y 상한과 설정 확인 필요: {error}')

    def _choose(self, key: str) -> None:
        selected = (filedialog.askdirectory(parent=self) if key == "input" else
                    filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")]))
        if selected:
            self.values[key].set(selected)
            if key == "input" and not self.values["output"].get():
                self.values["output"].set(selected + "-xy-mapped")

    def _arguments(self) -> dict:
        values = {key: variable.get().strip() for key, variable in self.values.items()}
        settings = MappingSettings.from_json(Path(values["settings"]).read_bytes(),
                                             expected_source=Profile.FX5U, expected_target=Profile.Q)
        return {"result_dir": Path(values["input"]), "output_dir": Path(values["output"]),
                "settings": settings,
                "fx5_bounds": Bounds(int(values["fx_x"]), int(values["fx_y"])),
                "q_bounds": Bounds(int(values["q_x"]), int(values["q_y"]))}

    def _start(self, publish: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            arguments = self._arguments()
        except (OSError, ValueError) as error:
            self._invalidate()
            self.status.set(f"입력 보류: {error}")
            return
        self.status.set("모든 프로그램 및 코멘트 X/Y 검사 중...")
        self.preview_button.configure(state="disabled")
        self.publish_button.configure(state="disabled")
        set_form_busy(self, True)

        def run() -> None:
            try:
                if publish:
                    report = map_forward_xy(**arguments)
                else:
                    _outputs, report = plan_forward_xy(arguments["result_dir"], settings=arguments["settings"],
                                                       fx5_bounds=arguments["fx5_bounds"],
                                                       q_bounds=arguments["q_bounds"])
                self.events.put(("candidate" if publish else "preview", report))
            except Exception as error:
                self.events.put(("hold", f"{type(error).__name__}: {error}"))

        self.worker = threading.Thread(target=run, name="converter-forward-xy", daemon=False)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, report = self.events.get_nowait()
                set_form_busy(self, False)
                self.preview_button.configure(state="normal")
                self.publish_button.configure(state="normal")
                if kind == "hold":
                    self._invalidate()
                    self.status.set(f"보류: {report}")
                else:
                    self.status.set("후보 생성 완료. GX Works2 가져오기, 재빌드, 재내보내기 검증 필요." if kind == "candidate"
                                    else "매핑 사전 점검 통과. 공식 검증 전 후보입니다.")
                    self.details.configure(state="normal")
                    self.details.delete("1.0", "end")
                    self.details.insert("1.0", json.dumps(report, ensure_ascii=False, indent=2))
                    self.details.configure(state="disabled")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

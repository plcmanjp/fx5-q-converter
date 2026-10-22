"""Shared range editor; conversion authority remains in the mapping engine."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from converter.xy_mapping import Bounds, Profile, Rule, UnmappedPolicy
from converter.xy_settings import MappingSettings

MAX_EDITOR_POINTS = 65536


def range_rows(settings: MappingSettings, source_bounds: Bounds, target_bounds: Bounds, *,
               allow_target_aliases: bool = False) -> tuple:
    if sum(rule.count for rule in settings.rules) > MAX_EDITOR_POINTS:
        raise ValueError(f'편집기 구간 합계는 {MAX_EDITOR_POINTS}점을 넘을 수 없다')
    settings.mapper(source_bounds=source_bounds, target_bounds=target_bounds,
                    allow_target_aliases=allow_target_aliases)
    result = []
    for rule in settings.rules:
        # Validate disabled drafts too, without assigning them to the active map.
        replace(settings, rules=(replace(rule, enabled=True),)).mapper(
            source_bounds=source_bounds, target_bounds=target_bounds,
            allow_target_aliases=allow_target_aliases)
        def end(start: str, profile: Profile) -> str:
            number = int(start[1:], profile.radix) + rule.count - 1
            return start[0].upper() + format(number, 'X' if profile is Profile.Q else 'o')
        result.append((rule.rule_id, '사용' if rule.enabled else '제외', rule.source_start,
                       end(rule.source_start, settings.source), rule.count, rule.target_start,
                       end(rule.target_start, settings.target)))
    return tuple(result)


def save_new_settings(path: Path, settings: MappingSettings,
                      source_bounds: Bounds, target_bounds: Bounds, *,
                      allow_target_aliases: bool = False) -> None:
    range_rows(settings, source_bounds, target_bounds, allow_target_aliases=allow_target_aliases)
    payload = settings.to_json()
    # Exclusive creation protects the loaded settings and any existing output.
    created = False
    try:
        with path.open('xb') as stream:
            created = True
            stream.write(payload)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


class MappingSettingsDialog(tk.Toplevel):
    def __init__(self, master, *, source: Profile, target: Profile,
                 source_bounds: Bounds, target_bounds: Bounds,
                 settings: MappingSettings | None = None, on_saved=None) -> None:
        settings = settings or MappingSettings(source, target, UnmappedPolicy.HOLD, ())
        if (settings.source, settings.target) != (source, target):
            raise ValueError('설정 방향이 편집기와 다릅니다')
        range_rows(settings, source_bounds, target_bounds)
        super().__init__(master)
        self.title(f'X/Y 구간 편집: {source.display_name} → {target.display_name}')
        self.minsize(820, 470)
        self.source_bounds, self.target_bounds = source_bounds, target_bounds
        self.settings = settings
        self.on_saved = on_saved
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text=(f'{source.display_name} ({source.radix}진수) → {target.display_name} ({target.radix}진수). '
                             '점수와 CPU 상한은 십진수. 저장 후 프로그램과 코멘트 전체를 다시 점검한다.')).grid(
                                 row=0, column=0, sticky='w', padx=10, pady=8)
        self.policy = tk.StringVar(value=self.settings.unmapped.value)
        policy_frame = ttk.Frame(self)
        policy_frame.grid(row=1, column=0, sticky='w', padx=10)
        ttk.Label(policy_frame, text='미매핑 정책').pack(side='left')
        ttk.Combobox(policy_frame, textvariable=self.policy, state='readonly', width=28,
                     values=[item.value for item in UnmappedPolicy]).pack(side='left', padx=8)
        columns = ('id', 'enabled', 'source', 'source_end', 'count', 'target', 'target_end')
        self.table = ttk.Treeview(self, columns=columns, show='headings', selectmode='browse', height=9)
        for key, title in zip(columns, ('규칙 ID', '상태', '원본 시작', '원본 끝', '점수', '대상 시작', '대상 끝')):
            self.table.heading(key, text=title)
            self.table.column(key, width=105, stretch=True)
        self.table.grid(row=2, column=0, sticky='nsew', padx=10, pady=8)
        self.table.bind('<<TreeviewSelect>>', self._selected)
        form = ttk.Frame(self)
        form.grid(row=3, column=0, sticky='ew', padx=10)
        self.fields = {key: tk.StringVar() for key in ('id', 'source', 'count', 'target')}
        for column, (key, title) in enumerate((('id', '규칙 ID'), ('source', '원본 시작'),
                                              ('count', '점수'), ('target', '대상 시작'))):
            ttk.Label(form, text=title).grid(row=0, column=column)
            ttk.Entry(form, textvariable=self.fields[key], width=16).grid(row=1, column=column, padx=4)
        self.enabled = tk.BooleanVar(value=True)
        self.editing_id = None
        self.form_snapshot = self._form_state()
        ttk.Checkbutton(form, text='사용', variable=self.enabled).grid(row=1, column=4)
        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky='w', padx=10, pady=8)
        for title, action in (('새 구간', self._new), ('구간 적용', self._apply), ('입력 취소', self._discard),
                              ('선택 구간 제거', self._remove), ('불러오기', self._load),
                              ('새 파일로 저장', self._save), ('반대 방향 후보', self._reverse)):
            ttk.Button(actions, text=title, command=lambda fn=action: self._run(fn)).pack(side='left', padx=3)
        self.status = tk.StringVar(value='구간 변경은 새 파일로 저장해야 변환 화면에 적용된다.')
        ttk.Label(self, textvariable=self.status, wraplength=800).grid(row=5, column=0, sticky='w', padx=10, pady=8)
        self.policy.trace_add('write', lambda *_: self.status.set('미매핑 정책 변경. 저장 시 다시 검증한다.'))
        self._refresh()
        self.transient(master)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self._close)

    def _close(self):
        parent = self.master
        self.destroy()
        if isinstance(parent, MappingSettingsDialog) and parent.winfo_exists():
            parent.grab_set()

    def _require_applied(self):
        if self._form_state() != self.form_snapshot:
            raise ValueError('편집 중인 구간을 먼저 적용하거나 입력 취소를 선택해라')

    def _form_state(self):
        return tuple(value.get().strip() for value in self.fields.values()), self.enabled.get()

    def _show_rule(self, rule=None):
        values = (rule.rule_id, rule.source_start, str(rule.count), rule.target_start) if rule else ('', '', '', '')
        for key, value in zip(self.fields, values):
            self.fields[key].set(value)
        self.enabled.set(rule.enabled if rule else True)
        self.editing_id = rule.rule_id if rule else None
        self.form_snapshot = self._form_state()

    def _discard(self):
        rule = next((r for r in self.settings.rules if r.rule_id == self.editing_id), None)
        self._show_rule(rule)

    def _run(self, action):
        try:
            action()
        except (ValueError, OSError) as error:
            self.status.set(f'보류: {error}')

    def _current(self):
        return replace(self.settings, unmapped=UnmappedPolicy(self.policy.get()))

    def _refresh(self):
        rows = range_rows(self._current(), self.source_bounds, self.target_bounds)
        self.table.delete(*self.table.get_children())
        for values in rows:
            self.table.insert('', 'end', iid=values[0], values=values)

    def _selected(self, _event=None):
        selected = self.table.selection()
        if not selected or selected[0] == self.editing_id:
            return
        if self._form_state() != self.form_snapshot:
            if self.editing_id:
                self.table.selection_set(self.editing_id)
            else:
                self.table.selection_remove(*selected)
            self.status.set('미적용 입력이 있다. 구간 적용 또는 입력 취소 후 다른 행을 선택해라.')
            return
        rule = next(r for r in self.settings.rules if r.rule_id == selected[0])
        self._show_rule(rule)

    def _new(self):
        self._require_applied()
        self.table.selection_remove(*self.table.selection())
        self._show_rule()

    def _apply(self):
        values = {key: value.get().strip() for key, value in self.fields.items()}
        rule = Rule(values['id'], values['source'], int(values['count']), values['target'], self.enabled.get())
        rules = tuple(rule if old.rule_id == self.editing_id else old for old in self.settings.rules)
        if self.editing_id is None:
            rules += (rule,)
        candidate = replace(self._current(), rules=rules)
        range_rows(candidate, self.source_bounds, self.target_bounds)
        self.settings = candidate
        self._show_rule(rule)
        self._refresh()
        self.table.selection_set(rule.rule_id)
        self.status.set('구간 검증 통과. 새 파일로 저장한 뒤 변환 화면에서 전체 참조와 코멘트를 점검한다.')

    def _remove(self):
        self._require_applied()
        selected = self.table.selection()
        if selected:
            self.settings = replace(self._current(), rules=tuple(r for r in self.settings.rules if r.rule_id != selected[0]))
            self._refresh()
            self._show_rule()
            self.status.set('선택 구간을 초안에서 제거했다. 원본 설정 파일은 유지된다.')

    def _load(self):
        self._require_applied()
        chosen = filedialog.askopenfilename(parent=self, filetypes=[('JSON', '*.json')])
        if chosen:
            candidate = MappingSettings.from_json(Path(chosen).read_bytes(),
                expected_source=self.settings.source, expected_target=self.settings.target)
            range_rows(candidate, self.source_bounds, self.target_bounds)
            self.settings = candidate
            self.policy.set(candidate.unmapped.value)
            self._refresh()
            self._show_rule()
            self.status.set('설정 불러오기 완료. 새 파일로 저장하면 변환 화면에 적용된다.')

    def _save(self):
        self._require_applied()
        candidate = self._current()
        range_rows(candidate, self.source_bounds, self.target_bounds)
        chosen = filedialog.asksaveasfilename(parent=self, defaultextension='.json', filetypes=[('JSON', '*.json')])
        if chosen:
            save_new_settings(Path(chosen), candidate, self.source_bounds, self.target_bounds)
            if self.on_saved:
                self.on_saved(chosen)
            self.status.set('새 설정 저장 완료. 변환 화면에서 전체 사전 점검을 다시 실행한다.')

    def _reverse(self):
        self._require_applied()
        current = self._current()
        candidate = current.reversed_candidate(source_bounds=self.target_bounds, target_bounds=self.source_bounds)
        MappingSettingsDialog(self, source=candidate.source, target=candidate.target,
            source_bounds=self.target_bounds, target_bounds=self.source_bounds, settings=candidate)


def set_form_busy(container, busy: bool) -> None:
    """Freeze all editable inputs while the immutable request is running."""
    for child in container.winfo_children():
        if isinstance(child, (ttk.Entry, ttk.Button, ttk.Combobox)):
            child.state(['disabled'] if busy else ['!disabled'])
        set_form_busy(child, busy)


def candidate_worker_running(container) -> bool:
    for child in container.winfo_children():
        if isinstance(child, tk.Toplevel):
            worker = getattr(child, 'worker', None)
            if worker is not None and worker.is_alive():
                return True
        if candidate_worker_running(child):
            return True
    return False

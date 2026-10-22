"""Explicit editor transitions shared by the X/Y and module rule forms."""
from dataclasses import replace

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QMessageBox, QStyledItemDelegate, QStyle

from p006_converter.xy_mapping import Bounds, Rule, Reference
from p006_converter.module_mapping import unit_number
from p006_app.xy_settings_dialog import range_rows
from p006_app.source_selection import interval_key, select_source_range


class SourceRangeDelegate(QStyledItemDelegate):
    """Expose the explicit range to styled painting without selecting the model."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.data(Qt.ItemDataRole.UserRole) is True:
            option.state |= QStyle.StateFlag.State_Selected
        else:
            option.state &= ~QStyle.StateFlag.State_Selected


class RuleEditorMixin:
    def controls(self, module=False):
        return (self.module_table, self.module_fields, self.module_mode, self.module_error,
                self.module_buttons, self.module_state, self.module_pending) if module else (
                    self.table, self.fields, self.xy_mode, self.xy_error,
                    self.buttons, self.form_state, self.pending)

    def ask_transition(self, module=False):
        if not self.isVisible():
            return "continue"
        box = QMessageBox(self)
        box.setWindowTitle("입력 중인 규칙")
        box.setText("입력 중인 변경을 어떻게 처리할까요?")
        save = box.addButton("저장 후 이동", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("변경 버리고 이동", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("계속 편집", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.exec()
        return "save" if box.clickedButton() is save else "discard" if box.clickedButton() is discard else "continue"

    def allow_transition(self, module=False):
        *_, pending = self.controls(module)
        snapshot = self.module_snapshot if module else self.row_snapshot
        if snapshot is None or not pending():
            return True
        decision = self.ask_transition(module)
        if decision == "save":
            saved = self.store_rule(module)
            if not saved:
                self.restore_selection(module)
            return saved
        if decision == "discard":
            self.cancel_rule(module)
            return True
        self.restore_selection(module)
        return False

    def restore_selection(self, module=False):
        table = self.module_table if module else self.table
        key = self.module_edit_key if module else self.xy_edit_key
        rules = self.draft.module_rules if module else self.draft.rules
        row = next((i for i, rule in enumerate(rules)
                    if (rule[0] if module else rule.rule_id) == key), None) if key is not None else None
        if module:
            self.module_selected = row
        else:
            self.selected = row
        table.blockSignals(True)
        table.clearSelection()
        table.setCurrentIndex(QModelIndex())
        if row is not None:
            table.selectRow(row)
            table.scrollToItem(table.item(row, 0))
        table.blockSignals(False)

    def start_rule(self, module=False, *, guard=True):
        if guard and not self.allow_transition(module):
            return False
        table, fields, mode, error, buttons, state, _ = self.controls(module)
        if module:
            self.module_edit_key = None
        else:
            self.xy_edit_key = None
        for field in fields.values():
            field.clear()
        if not module:
            ids = {rule.rule_id for rule in self.draft.rules}
            number = 1
            while str(number) in ids:
                number += 1
            fields["id"].setText(str(number))
            self.enabled.setChecked(True)
        mode.setText("새 U 규칙 추가 중" if module else "새 규칙 추가 중")
        buttons["U 규칙 반영" if module else "행 반영"].setText("U 규칙 추가" if module else "규칙 추가")
        error.clear()
        self.restore_selection(module)
        self.capture_form(module)
        if not module:
            self.update_range_hint()
        fields["source"].setFocus()
        return True

    def capture_form(self, module=False):
        if module:
            self.module_snapshot = self.module_state()
        else:
            self.row_snapshot = self.form_state()

    def edit_rule(self, row, module=False, *, guard=True):
        rules = self.draft.module_rules if module else self.draft.rules
        if not 0 <= row < len(rules):
            return
        # Capture identity before a possible save/refresh, never resolve by stale row.
        key = rules[row][0] if module else rules[row].rule_id
        if guard and not self.allow_transition(module):
            return
        rules = self.draft.module_rules if module else self.draft.rules
        rule = next((r for r in rules if (r[0] if module else r.rule_id) == key), None)
        if rule is None:
            return
        table, fields, mode, error, buttons, _, _ = self.controls(module)
        if module:
            self.module_edit_key = key
            values = dict(source=rule[0], target=rule[1])
        else:
            self.xy_edit_key = key
            values = dict(id=rule.rule_id, source=rule.source_start, count=rule.count, target=rule.target_start)
            self.enabled.setChecked(rule.enabled)
        for name, value in values.items():
            fields[name].setText(str(value))
        mode.setText(f"{'U ' if module else ''}규칙 {key} 수정 중 (전체 범위)")
        buttons["U 규칙 반영" if module else "행 반영"].setText("U 변경 저장" if module else "변경 저장")
        error.clear()
        self.capture_form(module)
        self.restore_selection(module)

    def cancel_rule(self, module=False):
        _, fields, _, error, _, _, _ = self.controls(module)
        snapshot = self.module_snapshot if module else self.row_snapshot
        for field, value in zip(fields.values(), snapshot):
            field.setText(value)
        if not module:
            self.enabled.setChecked(snapshot[4])
        error.clear()
        self.restore_selection(module)
        self.status.setText("입력 변경을 취소했습니다. 저장된 규칙은 보존됩니다.")

    def store_rule(self, module=False):
        _, fields, _, error, _, _, _ = self.controls(module)
        key = self.module_edit_key if module else self.xy_edit_key
        try:
            for name in (('source', 'target') if module else ('source', 'count', 'target')):
                if not fields[name].text().strip():
                    fields[name].setFocus()
                    raise ValueError(f"{fields[name].accessibleName()} 값을 입력하세요")
            if module:
                rule = tuple(f"U{unit_number(fields[k].text().strip()):X}" for k in ("source", "target"))
                rules = list(self.draft.module_rules)
            else:
                try:
                    count = int(fields['count'].text())
                except ValueError:
                    fields['count'].setFocus()
                    raise ValueError("점수: 1 이상의 십진수 정수를 입력하세요. 예: 16") from None
                if count < 1:
                    fields['count'].setFocus()
                    raise ValueError("점수: 1 이상이어야 합니다. 0이나 음수는 사용할 수 없습니다.")
                rule = Rule(fields['id'].text().strip(), fields['source'].text().strip().upper(),
                            count, fields['target'].text().strip().upper(), self.enabled.isChecked())
                rules = list(self.draft.rules)
            if key is None:
                rules.append(rule)
            else:
                index = next((i for i, r in enumerate(rules) if (r[0] if module else r.rule_id) == key), None)
                if index is None:
                    raise ValueError("수정 대상이 바뀌었습니다. 규칙을 다시 선택하세요")
                rules[index] = rule
            candidate = replace(self.draft, **{'module_rules' if module else 'rules': tuple(rules)})
            if module:
                self.module_mapper(candidate)
            else:
                range_rows(candidate, Bounds(), Bounds(), allow_target_aliases=True)
        except (ValueError, OSError) as exc:
            error.setText(str(exc))
            self.status.setText("검사 실패: 입력 오류를 확인하세요. 기존 규칙과 입력값은 보존됩니다.")
            return False
        self.draft = candidate
        saved_key = rule[0] if module else rule.rule_id
        if module:
            self.module_edit_key = saved_key
        else:
            self.xy_edit_key = saved_key
        self.capture_form(module)
        self.refresh()
        self.edit_rule(next(i for i, r in enumerate(rules) if (r[0] if module else r.rule_id) == saved_key), module, guard=False)
        self.status.setText(f"규칙 {saved_key}을 {'추가' if key is None else '수정'}했습니다. 저장한 규칙을 검사합니다.")
        self.committed.emit()
        return True

    def delete_rule(self, module=False):
        # Removal is explicit and never silently saves pending input.
        if (self.module_pending() if module else self.pending()):
            self.status.setText("입력 변경을 저장하거나 취소한 뒤 규칙을 삭제하세요.")
            return
        key = self.module_edit_key if module else self.xy_edit_key
        if key is None:
            self.status.setText("삭제할 규칙을 선택하세요.")
            return
        rules = self.draft.module_rules if module else self.draft.rules
        remaining = tuple(r for r in rules if (r[0] if module else r.rule_id) != key)
        self.draft = replace(self.draft, **{'module_rules' if module else 'rules': remaining})
        self.start_rule(module, guard=False)
        self.refresh()
        self.status.setText(f"규칙 {key}을 삭제했습니다. 저장한 규칙을 검사합니다.")
        self.committed.emit()

    def clear_source_selection(self):
        self.source_anchor = None
        self.source_selection = None
        self.source_selection_form = None

    def highlight_source_selection(self):
        selection = self.source_selection
        for row in range(self.preview_table.rowCount()):
            index = self.preview_page * 200 + row
            if index >= len(self.preview_rows):
                continue
            selected = False
            if selection:
                try:
                    family, first, last = interval_key(self.preview_rows[index], self.draft.source.radix)
                    selected = bool(family == selection.family
                                    and first <= selection.last and last >= selection.first)
                except ValueError:
                    pass
            for column in range(self.preview_table.columnCount()):
                item = self.preview_table.item(row, column)
                if item is not None:
                    item.setBackground(QColor("#dbeafe") if selected else QColor("#ffffff"))
                    item.setData(Qt.ItemDataRole.UserRole, selected)

    def choose_source(self, row, column=0, *, shift=None):
        if (self.preview_pending or not 0 <= row < self.preview_table.rowCount()
                or self.preview_page * 200 + row >= len(self.preview_rows)):
            return
        segment = self.preview_rows[self.preview_page * 200 + row]
        shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier) if shift is None else shift
        try:
            endpoint = interval_key(segment, self.draft.source.radix)
        except ValueError as error:
            self.status.setText(f"원본 구간을 선택할 수 없습니다: {error}. 기존 입력을 보존합니다.")
            return
        if shift:
            if self.source_anchor is None:
                self.status.setText("먼저 원본 구간을 일반 클릭한 뒤 Shift+클릭하세요. 기존 입력을 보존합니다.")
                return
            try:
                selection = select_source_range(self.preview_rows, self.source_anchor, endpoint, self.draft.source.radix)
            except ValueError as error:
                self.status.setText(str(error))
                self.highlight_source_selection()
                return
            # Auto-filled selection changes are not unsaved operator edits. A
            # manually edited form still uses the existing save/discard gate.
            if self.form_state() != self.source_selection_form and not self.allow_transition():
                self.highlight_source_selection()
                return
            if self.preview_pending or self.source_anchor is None:
                self.status.setText("규칙 저장으로 주소 목록을 다시 계산 중입니다. 완료 후 원본을 일반 클릭하여 다시 선택하세요.")
                return
            target = self.fields['target'].text()
            self.start_rule(guard=False)
            self.fields['source'].setText(selection.spelling(selection.first, self.draft.source.radix))
            self.fields['count'].setText(str(selection.points))
            self.fields['target'].setText(target)
            self.source_selection = selection
            self.source_selection_form = self.form_state()
            self.highlight_source_selection()
            self.open_rule_editor(0)
            self.fields['target'].setFocus()
            self.status.setText(f"원본 {selection.spelling(selection.first, self.draft.source.radix)} ~ "
                                f"{selection.spelling(selection.last, self.draft.source.radix)}, {selection.points}점 선택. "
                                "대상 시작을 확인한 뒤 수정안을 저장하세요. 기존 규칙은 그대로 보존됩니다.")
            self.edited.emit()
            return
        if not self.allow_transition():
            return
        start = segment['source_start']
        number = int(start[1:], self.draft.source.radix)
        matches = [i for i, rule in enumerate(self.draft.rules)
                   if rule.source_start[0].upper() == start[0].upper()
                   and int(rule.source_start[1:], self.draft.source.radix) <= number
                   < int(rule.source_start[1:], self.draft.source.radix) + rule.count]
        if matches:
            active = next((i for i in matches if self.draft.rules[i].enabled), matches[0])
            self.edit_rule(active, guard=False)
        else:
            self.start_rule(guard=False)
            self.fields['source'].setText(start)
            self.fields['count'].setText(str(segment['points']))
            try:
                target = self.draft.mapper(source_bounds=Bounds(), target_bounds=Bounds(),
                                           allow_target_aliases=True).project(Reference(start, 'editor')).target
            except ValueError:
                target = '-'
            self.fields['target'].setText(target if target != '-' else '')
            if target == '-':
                self.xy_error.setText("현재 변경안이 보류 상태입니다. 대상 시작을 입력하세요.")
        self.source_anchor = endpoint
        self.source_selection = select_source_range(self.preview_rows, endpoint, endpoint, self.draft.source.radix)
        self.source_selection_form = self.form_state()
        self.highlight_source_selection()
        self.open_rule_editor(0)
        self.fields['target'].setFocus()
        self.edited.emit()

    def choose_module_source(self, row, column=0):
        if not self.module_fields['source'].isEnabled() or not 0 <= row < self.module_preview.rowCount():
            return
        source = self.module_preview.item(row, 0).text()
        if not self.allow_transition(True):
            return
        match = next((i for i, r in enumerate(self.draft.module_rules) if unit_number(r[0]) == unit_number(source)), None)
        if match is not None:
            self.edit_rule(match, True, guard=False)
        else:
            self.start_rule(True, guard=False)
            try:
                target = self.module_mapper(self.draft).target_unit(source)
            except ValueError:
                target = '-'
            self.module_fields['source'].setText(source)
            self.module_fields['target'].setText(target if target != '-' else '')
            if target == '-':
                self.module_error.setText("현재 변경안이 보류 상태입니다. 대상 U를 입력하세요.")
        self.open_rule_editor(1)
        self.module_fields['target'].setFocus()
        self.edited.emit()

    def update_range_hint(self):
        try:
            count = int(self.fields['count'].text())
            if count < 1:
                raise ValueError()
            ends = []
            for key, profile in (('source', self.draft.source), ('target', self.draft.target)):
                start = self.fields[key].text().strip().upper()
                if not start:
                    ends.append("미입력")
                    continue
                value = int(start[1:], profile.radix) + count - 1
                ends.append(start[0] + format(value, 'X' if profile.radix == 16 else 'o'))
            self.range_hint.setText(f"원본 끝 {ends[0]} / 대상 끝 {ends[1]}")
        except (ValueError, IndexError):
            self.range_hint.setText("끝 주소: 원본 시작, 점수, 대상 시작을 입력하세요")

    def new_row(self):
        return self.start_rule()

    def select_row(self):
        self.edit_rule(self.table.currentRow())

    def discard_row(self):
        self.cancel_rule()

    def commit_row(self):
        return self.store_rule()

    def remove_row(self):
        self.delete_rule()

    def new_module_row(self):
        return self.start_rule(True)

    def select_module_row(self):
        self.edit_rule(self.module_table.currentRow(), True)

    def discard_module_row(self):
        self.cancel_rule(True)

    def commit_module_row(self):
        return self.store_rule(True)

    def remove_module_row(self):
        self.delete_rule(True)

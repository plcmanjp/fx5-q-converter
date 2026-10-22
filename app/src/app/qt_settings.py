"""Qt draft editor; range arithmetic and validation use existing engine helpers."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import threading
from PySide6.QtCore import Signal, Qt, QTimer, QModelIndex, QSize
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout,
                              QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QTableWidget,
                              QTableWidgetItem, QTextEdit, QVBoxLayout, QTabWidget, QWidget, QLayout, QSplitter, QListView)
from app.usage_editor import direction_profiles, read_settings_snapshot, UsageGroup, group_matches_diagnostic
from app.workflow_service import WorkflowDiagnostic
from app.xy_settings_dialog import MAX_EDITOR_POINTS, range_rows, save_new_settings
from app.qt_rule_editor import RuleEditorMixin, SourceRangeDelegate
from converter import core
from converter.module_mapping import ModuleMapper, unit_number
from converter.xy_mapping import Bounds, Rule, Reference, UnmappedPolicy
from converter.xy_settings import MappingSettings


def usage_mapping_rows(inventory, initial, current, cancelled=None):
    """Project every used point; split rows at changes and mapping failures."""
    if sum(g["interval_points"] for g in inventory) > MAX_EDITOR_POINTS:
        raise ValueError(f"사용 구간 합계가 미리보기 한도 {MAX_EDITOR_POINTS}점을 넘습니다")
    mappers = [s.mapper(source_bounds=Bounds(), target_bounds=Bounds(), allow_target_aliases=True)
               for s in (initial, current)]
    rows = []
    for group in inventory:
        segment = None
        for number in range(group["first"], group["last"] + 1):
            if cancelled is not None and cancelled.is_set():
                return []
            spelling = group["family"] + format(number, "X" if current.source.radix == 16 else "o")
            projections = []
            for mapper in mappers:
                try:
                    target = mapper.project(Reference(spelling, "usage-preview")).target
                    projections.append((target, int(target[1:], current.target.radix), ""))
                except ValueError as error:
                    projections.append(("-", None, str(error)))
            targets = tuple(p[0] for p in projections)
            ordinals = tuple(p[1] for p in projections)
            states = tuple(p[1] is not None for p in projections)
            contiguous = segment is not None and segment["states"] == states and all(
                ordinal is None or ordinal == previous + 1
                for ordinal, previous in zip(ordinals, segment["ordinals"]))
            if not contiguous:
                segment = dict(source_start=spelling, source_end=spelling, points=1,
                               initial_start=targets[0], initial_end=targets[0],
                               target_start=targets[1], target_end=targets[1],
                               states=states, ordinals=ordinals,
                               errors=tuple(p[2] for p in projections), pous=group["pous"])
                rows.append(segment)
            else:
                segment.update(source_end=spelling, points=segment["points"] + 1,
                               initial_end=targets[0], target_end=targets[1], ordinals=ordinals)
    return rows


class PolicyComboBox(QComboBox):
    """Keep the complete short policy list visible, including styled row padding."""

    def __init__(self):
        super().__init__()
        self.setView(QListView())
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def showPopup(self):
        self.ensurePolished()
        height = self.fontMetrics().height() + 12
        for row in range(self.count()):
            self.setItemData(row, QSize(0, height), Qt.ItemDataRole.SizeHintRole)
        self.setMaxVisibleItems(self.count())
        self.view().setMinimumHeight(height * self.count() + 2 * self.view().frameWidth())
        super().showPopup()


class AddressDialog(RuleEditorMixin, QDialog):
    applied = Signal(str)
    edited = Signal()
    committed = Signal()

    def __init__(self, parent, result, *, embedded=False):
        super().__init__(parent)
        self.embedded = embedded
        self.compact = False
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.setObjectName("mappingEditor")
        self.setWindowTitle("주소 초안 편집")
        self.resize(920, 660)
        self.setMinimumSize(0, 0)
        source, target = direction_profiles(result["direction"])
        policy = UnmappedPolicy.SOURCE_SPELLING_IDENTITY if result["direction"] == "FX5_TO_Q" else UnmappedPolicy.NUMERIC_IDENTITY
        self.baseline = MappingSettings.from_json(result["xy_json"].encode("utf-8"), expected_source=source, expected_target=target) if result.get("xy_json") else MappingSettings(source, target, policy, ())
        self.draft = self.baseline
        self.inventory = result.get("inventory", [])
        self.direction = result["direction"]
        self.target_cpu = result.get("actual_target_cpu", "FX5U")
        self.module_inventory = tuple(result.get("module_inventory", ()))
        self.initial = MappingSettings(source, target, policy, ())
        self.module_selected = None
        self.module_snapshot = None
        self.preview_generation = 0
        self.preview_queue = queue.Queue()
        self.preview_cancelled = threading.Event()
        self.preview_pending = False
        self.preview_rows = []
        self.preview_page = 0
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(30)
        self.preview_timer.timeout.connect(self.receive_preview)
        self.selected = None
        self.clear_source_selection()
        self.xy_edit_key = None
        self.module_edit_key = None
        self.row_snapshot = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        text = QLabel(f"{source.display_name} {source.radix}진수 → {target.display_name} {target.radix}진수. 점수는 십진수입니다. 실제 CPU 할당과 동적 주소는 별도 확인하세요.")
        text.setWordWrap(True)
        if not embedded:
            layout.addWidget(text)
        else:
            text.hide()
        inventory = QTextEdit()
        inventory.setAccessibleName("사용 주소와 미검사 구간")
        inventory.setReadOnly(True)
        inventory.setMaximumHeight(55)
        inventory.setPlainText("사용 구간\n" + "\n".join(f"{g['source_start']} ~ {g['source_end']} / {g['used_points']}점 / {', '.join(g['pous'])}" for g in result.get("inventory", [])) +
                               "\n미검사: " + ", ".join(result.get("inventory_unsupported", [])))
        if not embedded:
            layout.addWidget(inventory)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("X/Y 주소와 U 모듈 매핑")
        self.tabs.setToolTip(text.text())
        xy_page = QWidget()
        xy_layout = QVBoxLayout(xy_page)
        self.tabs.addTab(xy_page, "X/Y 주소 매핑")
        layout.addWidget(self.tabs)
        comparison_page, editor_page = QWidget(), QWidget()
        comparison_layout, editor_layout = QVBoxLayout(comparison_page), QVBoxLayout(editor_page)
        xy_columns = QSplitter(Qt.Orientation.Vertical)
        xy_columns.setChildrenCollapsible(False)
        xy_columns.addWidget(comparison_page)
        xy_columns.addWidget(editor_page)
        xy_columns.setStretchFactor(0, 1)
        xy_columns.setStretchFactor(1, 0)
        xy_layout.addWidget(xy_columns)
        preview_heading = QHBoxLayout()
        self.preview_summary = QLabel("실제 사용 구간의 기본 대상 주소와 현재 대상 주소")
        self.preview_summary.setWordWrap(True)
        preview_heading.addWidget(self.preview_summary, 1)
        self.preview_previous = QPushButton("이전 구간")
        self.preview_next = QPushButton("다음 구간")
        for button, offset in ((self.preview_previous, -1), (self.preview_next, 1)):
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, step=offset: self.show_preview_page(self.preview_page + step))
            preview_heading.addWidget(button)
        comparison_layout.addLayout(preview_heading)
        self.xy_edit_button = QPushButton("X/Y 편집 규칙 열기")
        self.xy_edit_button.setObjectName("saveDraft")
        self.xy_edit_button.clicked.connect(lambda: self.open_rule_editor(0))
        preview_heading.addWidget(self.xy_edit_button)
        self.xy_edit_button.hide()
        self.preview_table = QTableWidget(0, 7)
        self.preview_table.setAccessibleName("사용 X/Y 구간의 기본 대상 주소와 현재 대상 주소")
        self.preview_table.setHorizontalHeaderLabels(["원본 시작", "원본 끝", "점수", "기본 대상 주소", "현재 대상 주소", "상태", "POU"])
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.source_range_delegate = SourceRangeDelegate(self.preview_table)
        self.preview_table.setItemDelegate(self.source_range_delegate)
        self.preview_table.setStyleSheet(
            "QTableView::item:selected { background: #dbeafe; color: #163d7a; }")
        self.preview_table.setToolTip("일반 클릭으로 기준 구간 선택, Shift+클릭으로 같은 계열의 연속 주소 확장 또는 축소")
        comparison_layout.addWidget(self.preview_table)
        self.preview_table.setMinimumHeight(200)
        self.preview_table.cellClicked.connect(self.choose_source)
        self.xy_empty = QLabel("분석된 실제 사용 X/Y 구간이 없습니다. X/Y 변경 규칙을 만들 필요가 없습니다.\n"
                               "U 모듈이 있으면 U 모듈 매핑을 확인하세요. 규칙을 저장하면 검사하고, 변환 버튼으로 파일을 만듭니다. "
                               "미검사 항목과 생성 보류 여부는 전체 검사에서 확인합니다.")
        self.xy_empty.setAccessibleName("사용 X/Y 구간 없는 상태와 다음 단계")
        self.xy_empty.setWordWrap(True)
        comparison_layout.addWidget(self.xy_empty)
        self.xy_empty.hide()
        editor_heading = QHBoxLayout()
        self.xy_mode = QLabel("새 규칙 추가 중")
        editor_heading.addWidget(self.xy_mode)
        editor_layout.addLayout(editor_heading)
        self.table = QTableWidget(0, 7)
        self.table.setAccessibleName("주소 초안의 원본 구간과 대상 구간")
        self.table.setHorizontalHeaderLabels(["규칙", "상태", "원본 시작", "원본 끝", "점수", "대상 시작", "대상 끝"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.select_row)
        editor_layout.addWidget(self.table)
        self.table.setMinimumHeight(60)
        self.policy = PolicyComboBox()
        self.policy.setAccessibleName("규칙 없는 주소 처리")
        for value, title in ((UnmappedPolicy.HOLD, "직접 지정 필요"), (UnmappedPolicy.NUMERIC_IDENTITY, "주소 번호 유지"),
                             (UnmappedPolicy.SOURCE_SPELLING_IDENTITY, "주소 표기 유지")):
            self.policy.addItem(title, value)
        for i in range(self.policy.count()):
            if self.policy.itemData(i) == self.draft.unmapped:
                self.policy.setCurrentIndex(i)
        self.policy.currentIndexChanged.connect(self.policy_changed)
        policy_row = QHBoxLayout()
        policy_row.addWidget(QLabel("규칙 없는 주소 처리"))
        policy_row.addWidget(self.policy)
        preview_heading.removeWidget(self.preview_summary)
        self.preview_summary.hide()
        preview_heading.insertLayout(0, policy_row, 1)
        self.policy_hint = QLabel()
        self.policy_hint.setAccessibleName("주소 처리 설명")
        self.policy_hint.setWordWrap(True)
        policy_row.addWidget(self.policy_hint, 1)
        self.update_policy_hint()
        form = QGridLayout()
        self.fields = {}
        for i, (key, title) in enumerate((("id", "규칙 ID"), ("source", "원본 시작"), ("count", "점수"), ("target", "대상 시작"))):
            form.addWidget(QLabel(title), 0, i)
            field = QLineEdit()
            field.setAccessibleName(title)
            self.fields[key] = field
            field.setMinimumWidth(0)
            field.setMinimumHeight(30)
            form.addWidget(field, 1, i)
        self.fields["id"].setReadOnly(True)
        self.range_hint = QLabel()
        self.range_hint.setAccessibleName("계산된 끝 주소")
        form.addWidget(self.range_hint, 2, 0, 1, 4)
        self.xy_error = QLabel()
        self.xy_error.setWordWrap(True)
        self.xy_error.setStyleSheet("color: #b42335")
        form.addWidget(self.xy_error, 3, 0, 1, 4)
        self.enabled = QCheckBox("사용")
        self.enabled.setChecked(True)
        editor_heading.addStretch(1)
        editor_heading.addWidget(self.enabled)
        editor_layout.addLayout(form)
        xy_actions = QGridLayout()
        editor_layout.addLayout(xy_actions)
        module_page = QWidget()
        module_layout = QVBoxLayout(module_page)
        self.tabs.addTab(module_page, "U 모듈 매핑")
        module_comparison, module_editor = QWidget(), QWidget()
        module_comparison_layout = QVBoxLayout(module_comparison)
        module_editor_layout = QVBoxLayout(module_editor)
        module_columns = QSplitter(Qt.Orientation.Vertical)
        module_columns.setChildrenCollapsible(False)
        module_columns.addWidget(module_comparison)
        module_columns.addWidget(module_editor)
        module_columns.setStretchFactor(0, 1)
        module_columns.setStretchFactor(1, 0)
        module_layout.addWidget(module_columns)
        module_note = QLabel("G 주소, 비트, 인덱스와 명령을 보존합니다. 프로그램과 코멘트의 대상 U 중복은 전체 검사에서 보류합니다.")
        self.module_note = module_note
        module_note.setWordWrap(True)
        module_heading = QHBoxLayout()
        module_heading.addWidget(module_note, 1)
        module_comparison_layout.addLayout(module_heading)
        self.module_edit_button = QPushButton("U 편집 규칙 열기")
        self.module_edit_button.setObjectName("saveDraft")
        self.module_edit_button.clicked.connect(lambda: self.open_rule_editor(1))
        module_heading.addWidget(self.module_edit_button)
        self.module_edit_button.hide()
        self.module_preview = QTableWidget(0, 4)
        self.module_preview.setMinimumHeight(200)
        self.module_preview.cellClicked.connect(self.choose_module_source)
        self.module_preview.setAccessibleName("U 모듈의 기본 대상 주소와 현재 대상 주소")
        self.module_preview.setHorizontalHeaderLabels(["원본", "기본 대상 주소", "현재 대상 주소", "사유"])
        self.module_preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        module_comparison_layout.addWidget(self.module_preview)
        self.module_mode = QLabel("새 U 규칙 추가 중")
        module_editor_layout.addWidget(self.module_mode)
        self.module_table = QTableWidget(0, 2)
        self.module_table.setAccessibleName("명시한 U 모듈 매핑 규칙")
        self.module_table.setHorizontalHeaderLabels(["원본 U", "대상 U"])
        self.module_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.module_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.module_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.module_table.itemSelectionChanged.connect(self.select_module_row)
        module_editor_layout.addWidget(self.module_table)
        self.module_table.setMinimumHeight(60)
        module_form = QGridLayout()
        self.module_fields = {}
        for i, (key, title) in enumerate((("source", "원본 U 모듈"), ("target", "대상 U 모듈"))):
            module_form.addWidget(QLabel(title), 0, i)
            field = QLineEdit()
            field.setAccessibleName(title)
            field.setMinimumHeight(30)
            self.module_fields[key] = field
            module_form.addWidget(field, 1, i)
        self.module_error = QLabel()
        self.module_error.setWordWrap(True)
        self.module_error.setStyleSheet("color: #b42335")
        module_form.addWidget(self.module_error, 2, 0, 1, 2)
        module_editor_layout.addLayout(module_form)
        module_actions = QGridLayout()
        self.module_buttons = {}
        for title, callback in (("새 U 규칙", self.new_module_row), ("U 규칙 반영", self.commit_module_row),
                                ("U 입력 취소", self.discard_module_row), ("선택 U 규칙 제거", self.remove_module_row)):
            button = QPushButton(title)
            button.setAutoDefault(False)
            button.clicked.connect(callback)
            position = len(self.module_buttons)
            module_actions.addWidget(button, 0, position)
            self.module_buttons[title] = button
        module_editor_layout.addLayout(module_actions)
        module_editable = self.direction == "Q_TO_FX5" and self.target_cpu != "FX5S"
        for widget in (*self.module_fields.values(), *self.module_buttons.values()):
            widget.setEnabled(module_editable)
        self.status = QLabel("규칙을 추가하거나 변경 저장한 뒤 전체 검사를 수행하세요.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.diff = QTextEdit()
        self.diff.setAccessibleName("현재 세션과 주소 초안의 차이")
        self.diff.setReadOnly(True)
        self.diff.setMaximumHeight(45)
        self.diff.setMinimumHeight(32)
        if embedded:
            self.diff_dialog = QDialog(self)
            self.diff_dialog.setWindowTitle("현재 세션과 주소 초안의 차이")
            self.diff_dialog.resize(700, 300)
            self.diff.setMaximumHeight(16777215)
            QVBoxLayout(self.diff_dialog).addWidget(self.diff)
        else:
            layout.addWidget(self.diff)
        actions = QGridLayout()
        self.actions = actions
        self.buttons = {}
        for i, (title, callback) in enumerate((("새 행", self.new_row), ("행 반영", self.commit_row), ("행 취소", self.discard_row),
                                ("선택 행 제거", self.remove_row), ("매핑 설정 불러오기", self.load), ("매핑 설정 저장", self.save),
                                ("세션에 적용", self.apply), ("닫기", self.hide))):
            button = QPushButton(title)
            button.setAutoDefault(False)
            if i < 4:
                button.clicked.connect(callback)
                xy_actions.addWidget(button, 0, i)
            else:
                button.clicked.connect(callback)
                actions.addWidget(button, 0, i - 4)
            self.buttons[title] = button
        self.buttons["세션에 적용"].setText("저장한 규칙 검사")
        self.buttons["행 반영"].setText("수정안에 저장")
        self.module_buttons["U 규칙 반영"].setText("U 수정안에 저장")
        for button in (self.buttons["행 반영"], self.module_buttons["U 규칙 반영"]):
            button.setObjectName("saveDraft")
        for button in (self.buttons["선택 행 제거"], self.module_buttons["선택 U 규칙 제거"]):
            button.setObjectName("removeRule")
        self.buttons["매핑 설정 불러오기"].setToolTip("저장한 X/Y 주소와 U 모듈 규칙을 초안으로 불러옵니다. 적용하려면 전체 검사를 수행하세요.")
        self.buttons["매핑 설정 저장"].setToolTip("현재 매핑 초안을 새 설정 파일로 보관합니다. 다음 작업이나 다른 PC에서 재사용할 수 있습니다.")
        for name in ("행 취소", "매핑 설정 불러오기", "매핑 설정 저장", "닫기"):
            self.buttons[name].setObjectName("quiet")
        self.module_buttons["U 입력 취소"].setObjectName("quiet")
        if embedded:
            self.buttons["닫기"].hide()
            self.buttons["세션에 적용"].hide()
            difference = QPushButton("초안 차이 보기")
            difference.setObjectName("quiet")
            difference.clicked.connect(self.diff_dialog.show)
            actions.addWidget(difference, 0, 3)
            # Reuse actions sit in the tab bar so a short window can keep five
            # comparison rows. The original buttons stay for their tooltips.
            self.corner_host = QWidget()
            self.corner_row = QHBoxLayout(self.corner_host)
            self.corner_row.setContentsMargins(0, 0, 0, 0)
            self.corner_row.setSpacing(4)
            reuse = QPushButton("규칙 파일")
            reuse.setObjectName("quiet")
            reuse.setAutoDefault(False)
            reuse.setAccessibleName("주소 규칙 파일")
            menu = QMenu(reuse)
            for name in ("매핑 설정 불러오기", "매핑 설정 저장"):
                menu.addAction(name, self.buttons[name].click)
            menu.addAction("초안 차이 보기", difference.click)
            reuse.setMenu(menu)
            self.corner_row.addWidget(reuse)
            self.tabs.setCornerWidget(self.corner_host, Qt.Corner.TopRightCorner)
            for widget in (self.buttons["매핑 설정 불러오기"], self.buttons["매핑 설정 저장"], difference):
                widget.hide()
        layout.addLayout(actions)
        for key in ("source", "count", "target"):
            self.fields[key].textChanged.connect(self.update_range_hint)
        for buttons, names in ((self.buttons, {"새 행": "새 규칙 추가", "행 취소": "입력 변경 취소", "선택 행 제거": "선택 규칙 삭제"}),
                               (self.module_buttons, {"새 U 규칙": "새 U 규칙 추가", "U 입력 취소": "U 입력 변경 취소", "선택 U 규칙 제거": "선택 U 규칙 삭제"})):
            for key, label in names.items():
                buttons[key].setText(label)
        self.new_row()
        self.new_module_row()
        self.refresh()
        self.set_target_cpu(self.target_cpu)
        for section in (xy_layout, comparison_layout, editor_layout, module_layout,
                        module_comparison_layout, module_editor_layout):
            section.setContentsMargins(4, 4, 4, 4)
            section.setSpacing(4)
        for section in (editor_layout, module_editor_layout):
            section.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        # Minimum viewport capacity includes five data rows, header and scrollbar.
        for table in (self.table, self.module_table, self.preview_table, self.module_preview):
            table.ensurePolished()
            height = (table.horizontalHeader().sizeHint().height()
                      + 5 * table.verticalHeader().defaultSectionSize()
                      + table.horizontalScrollBar().sizeHint().height() + 2 * table.frameWidth())
            table.setMinimumHeight(height)
        for page in (editor_page, module_editor):
            page.ensurePolished()
            page.layout().activate()
        xy_columns.setStretchFactor(1, 1)
        module_columns.setStretchFactor(1, 1)
        xy_columns.setSizes([300, editor_page.minimumSizeHint().height()])
        module_columns.setSizes([300, module_editor.minimumSizeHint().height()])
        self.editor_sections = ((xy_columns, editor_page), (module_columns, module_editor))
        self.rule_dialogs = []
        self.status_layout = layout
        for title in ("X/Y 편집 규칙", "U 편집 규칙"):
            dialog = QDialog(self)
            dialog.setWindowTitle(title)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog_layout = QVBoxLayout(dialog)
            close = QPushButton("비교표로 돌아가기")
            close.setObjectName("quiet")
            close.clicked.connect(dialog.close)
            dialog_layout.addWidget(close)
            dialog.finished.connect(self.restore_editor_status)
            self.rule_dialogs.append(dialog)
        for field in (*self.fields.values(), *self.module_fields.values()):
            field.textEdited.connect(self.edited)
        self.enabled.clicked.connect(self.edited)
        self.policy.activated.connect(self.edited)
        if not embedded:
            self.set_compact(True)
        self.update_xy_empty_state()

    def set_compact(self, compact):
        """Rehome the same draft controls, preserving inputs and validation state."""
        if self.compact == compact:
            return
        self.compact = compact
        for (splitter, page), dialog in zip(self.editor_sections, self.rule_dialogs):
            dialog.close()
            if compact:
                dialog.layout().insertWidget(0, page)
                page.show()
            else:
                splitter.addWidget(page)
                page.show()
                splitter.setSizes([300, page.minimumSizeHint().height()])
        self.xy_edit_button.setVisible(compact)
        self.module_edit_button.setVisible(compact)
        self.apply_compact_chrome()
        self.update_xy_empty_state()

    def apply_compact_chrome(self):
        """Give the comparison tables the short-window height. Draft status stays in the editor dialog."""
        in_dialog = self.status.parentWidget() is not self
        self.status.setVisible((not self.compact) or in_dialog)
        if hasattr(self, "module_note"):
            self.module_note.setVisible(not self.compact)
        if hasattr(self, "policy_hint"):
            self.policy_hint.setMaximumHeight(32 if self.compact else 16777215)
        margin = 0 if self.compact else 4
        root = self.layout()
        if root is not None:
            root.setContentsMargins(margin, margin, margin, margin)
            root.setSpacing(2 if self.compact else 4)
        if hasattr(self, "editor_sections"):
            for splitter, page in self.editor_sections:
                for widget in (splitter.widget(0), page, splitter.parentWidget()):
                    if widget is not None and widget.layout() is not None:
                        widget.layout().setContentsMargins(margin, margin, margin, margin)
                        widget.layout().setSpacing(2 if self.compact else 4)

    def restore_editor_status(self):
        self.status_layout.insertWidget(self.status_layout.indexOf(self.tabs) + 1, self.status)
        self.apply_compact_chrome()

    def open_rule_editor(self, index):
        if not self.compact:
            self.editor_sections[index][1].show()
            return
        dialog = self.rule_dialogs[index]
        dialog.layout().insertWidget(1, self.status)
        self.status.show()
        screen = self.screen().availableGeometry()
        dialog.resize(min(900, screen.width() - 32), min(dialog.sizeHint().height(), screen.height() - 64))
        dialog.show()
        dialog.raise_()
        (self.fields if index == 0 else self.module_fields)["source"].setFocus()

    def form_state(self):
        return tuple(f.text() for f in self.fields.values()) + (self.enabled.isChecked(),)

    def focus_diagnostic(self, diagnostic):
        item = WorkflowDiagnostic(**diagnostic)
        groups = [UsageGroup(**g) for g in self.inventory]
        matches = [g for g in groups if group_matches_diagnostic(g, item.locator, item.operand,
                                                                self.draft.source, diagnostic=item)]
        if self.pending():
            self.status.setText("입력 중인 행을 보존했습니다. 관련 위치: " + str(item.locator or item.operand))
            self.open_rule_editor(0)
            return
        mapper = self.draft.mapper(source_bounds=Bounds(), target_bounds=Bounds(), allow_target_aliases=True)
        operand = item.operand
        if item.code == "XY_TARGET_ALIAS" and item.detail:
            try:
                aliases = json.loads(item.detail)
                operand = aliases.get("sources", [None])[0]
            except (ValueError, TypeError, IndexError, AttributeError):
                operand = None
        for group in matches:
            try:
                ids = mapper.project(Reference(operand, item.locator or "diagnostic")).rule_ids if operand else ()
            except ValueError:
                ids = ()
            for index, rule in enumerate(self.draft.rules):
                if rule.rule_id in ids:
                    self.tabs.setCurrentIndex(0)
                    self.table.selectRow(index)
                    self.open_rule_editor(0)
                    self.fields["target"].setFocus()
                    return
        self.status.setText("관련 사용 주소: " + ", ".join(g.source_start + " ~ " + g.source_end for g in matches) +
                            " / 위치: " + str(item.locator or item.operand or "프로젝트"))

    def pending(self):
        return self.form_state() != self.row_snapshot

    def run(self, action):
        try:
            action()
        except (ValueError, OSError) as error:
            self.status.setText(f"검사 실패: {error}. 초안과 행 입력을 보존했습니다.")

    def policy_changed(self):
        self.draft = replace(self.draft, unmapped=UnmappedPolicy(self.policy.currentData()))
        self.refresh()

    def update_policy_hint(self):
        policy = self.draft.unmapped
        if policy is UnmappedPolicy.HOLD:
            text = "대상 주소를 지정할 때까지 보류합니다."
        elif policy is UnmappedPolicy.NUMERIC_IDENTITY:
            example = "X100 → X400 (16진수 → 8진수)" if self.direction == "Q_TO_FX5" else "X100 → X40 (8진수 → 16진수)"
            text = f"예: {example}"
        else:
            text = "예: X100 → X100 (표기 유지)"
        self.policy_hint.setText(text)
        self.policy_hint.setToolTip("규칙이 없는 X/Y 주소 전체에 적용합니다. 주소 번호 유지는 숫자값을, 주소 표기 유지는 문자열을 유지합니다. 대상에서 사용할 수 없는 표기는 확인이 필요합니다.")

    def module_state(self):
        return tuple(field.text() for field in self.module_fields.values())

    def module_pending(self):
        return self.module_state() != self.module_snapshot

    def module_mapper(self, settings):
        if self.direction != "Q_TO_FX5":
            if settings.module_rules:
                raise ValueError("U 사용자 매핑은 Q에서 FX5로 변환할 때만 지원합니다")
            return None
        return ModuleMapper(self.target_cpu, rules=settings.module_rules)

    def set_target_cpu(self, cpu):
        unchanged = cpu == self.target_cpu
        self.target_cpu = cpu
        editable = self.direction == "Q_TO_FX5" and cpu != "FX5S"
        for widget in (*self.module_fields.values(), *self.module_buttons.values()):
            widget.setEnabled(editable)
        self.module_buttons["선택 U 규칙 제거"].setEnabled(editable or bool(self.draft.module_rules))
        self.module_buttons["U 입력 취소"].setEnabled(editable or self.module_pending())
        if unchanged:
            return
        # CPU changes invalidate the workspace plan, not the saved rule draft.
        # Keep any existing pending/dirty state without inventing a rule edit.
        self.refresh(notify_edit=False)
        try:
            self.module_mapper(self.draft)
        except ValueError as error:
            self.status.setText(f"대상 CPU 변경: {error}. 초안은 보존했습니다. 유효한 CPU나 U 규칙을 선택하세요.")
        else:
            self.status.setText("대상 CPU가 바뀌었습니다. 초안은 보존하며 저장한 규칙을 새 CPU로 자동 검사합니다.")

    def refresh_module_preview(self):
        rules = self.draft.module_rules
        self.module_table.blockSignals(True)
        self.module_table.setRowCount(len(rules))
        for index, rule in enumerate(rules):
            for column, value in enumerate(rule):
                self.module_table.setItem(index, column, QTableWidgetItem(value))
        self.module_table.resizeColumnsToContents()
        self.module_table.blockSignals(False)
        self.restore_selection(True)
        units = tuple(dict.fromkeys(f"U{unit_number(unit):X}" for unit in (
            *self.module_inventory, *(source for source, _ in rules))))
        try:
            mapper = self.module_mapper(self.draft)
            rows = mapper.rows(units) if mapper else [
                dict(source=unit, initial=unit, target=unit, reason="FX5에서 Q로 변환: 원본 U 보존")
                for unit in units]
        except ValueError as error:
            # Retain the user's rules after a CPU change and expose the error.
            explicit = {f"U{unit_number(source):X}": target for source, target in rules}
            try:
                defaults = {row["source"]: row["initial"] for row in ModuleMapper(self.target_cpu).rows(units)}
            except ValueError:
                defaults = {}
            rows = [dict(source=unit, initial=defaults.get(unit, "-"), target=explicit.get(unit, defaults.get(unit, "-")),
                         reason=f"보류: {error}") for unit in units]
        used = {f"U{unit_number(unit):X}" for unit in self.module_inventory}
        targets = {}
        for row in rows:
            if row["source"] in used:
                targets.setdefault(row["target"], set()).add(row["source"])
        for row in rows:
            if row["source"] in used and len(targets[row["target"]]) > 1:
                row["reason"] += " / 실제 사용 대상 중복"
        self.module_preview.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, key in enumerate(("source", "initial", "target", "reason")):
                self.module_preview.setItem(index, column, QTableWidgetItem(str(row[key])))
        self.module_preview.resizeColumnsToContents()

    def refresh_usage_preview(self):
        self.clear_source_selection()
        self.preview_cancelled.set()
        self.preview_cancelled = threading.Event()
        self.preview_generation += 1
        generation, cancelled = self.preview_generation, self.preview_cancelled
        inventory = tuple(dict(group) for group in self.inventory)
        initial, current, sink = self.initial, self.draft, self.preview_queue
        self.preview_pending = True
        self.preview_previous.setEnabled(False)
        self.preview_next.setEnabled(False)
        self.update_xy_empty_state()
        if not inventory:
            self.preview_pending = False
            self.preview_rows = []
            self.preview_table.setRowCount(0)
            self.preview_timer.stop()
            if not self.pending() and not self.module_pending() and not self.draft.rules:
                self.status.setText("X/Y 변경 규칙 없이 전체 검사로 진행할 수 있습니다. U 모듈과 미검사 항목도 검사합니다.")
            return
        self.preview_summary.setText("실제 사용 구간의 기본 대상 주소와 현재 대상 주소 계산 중")
        self.preview_table.setRowCount(1)
        for column in range(self.preview_table.columnCount()):
            self.preview_table.setItem(0, column, QTableWidgetItem("계산 중" if column == 5 else "-"))
        def work():
            try:
                rows, error = usage_mapping_rows(inventory, initial, current, cancelled), None
            except ValueError as caught:
                rows, error = [], str(caught)
            if not cancelled.is_set():
                sink.put((generation, rows, error))
        # The worker owns plain immutable settings and a queue, never widgets.
        threading.Thread(target=work, daemon=True).start()
        self.preview_timer.start()

    def update_xy_empty_state(self):
        empty = not self.inventory
        self.xy_empty.setVisible(empty)
        self.preview_table.setVisible(not empty)
        self.preview_previous.setVisible(not empty)
        self.preview_next.setVisible(not empty)
        self.xy_edit_button.setVisible(self.compact or empty)
        if hasattr(self, "editor_sections") and not self.compact:
            self.editor_sections[0][1].setVisible(not empty or bool(self.draft.rules) or self.pending())

    def hideEvent(self, event):
        # Source resets and stage switches close owned modal windows while
        # preserving the same draft controls for the next visit.
        for dialog in getattr(self, "rule_dialogs", ()):
            dialog.close()
        self.preview_cancelled.set()
        self.preview_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.preview_cancelled.set()
        self.preview_timer.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.preview_pending and self.preview_cancelled.is_set():
            self.refresh_usage_preview()

    def receive_preview(self):
        while True:
            try:
                generation, rows, error = self.preview_queue.get_nowait()
            except queue.Empty:
                return
            if generation != self.preview_generation:
                continue
            self.preview_pending = False
            self.preview_timer.stop()
            self.preview_rows = rows
            if not rows:
                self.preview_table.setRowCount(1)
                self.preview_summary.setText("실제 사용 구간의 기본 대상 주소와 현재 대상 주소")
                for column in range(self.preview_table.columnCount()):
                    self.preview_table.setItem(0, column, QTableWidgetItem(
                        (f"보류: {error}" if error else "실제 사용 X/Y 구간 없음") if column == 5 else "-"))
                continue
            self.show_preview_page(0)

    def show_preview_page(self, page):
        if self.preview_pending or not self.preview_rows:
            return
        page_size = 200
        self.preview_page = max(0, min(page, (len(self.preview_rows) - 1) // page_size))
        first = self.preview_page * page_size
        rows = self.preview_rows[first:first + page_size]
        self.preview_summary.setText(f"사용 주소: {len(self.preview_rows)}구간 ({first + 1} ~ {first + len(rows)})")
        self.preview_previous.setEnabled(first > 0)
        self.preview_next.setEnabled(first + len(rows) < len(self.preview_rows))
        self.preview_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            def interval(start, end):
                return start if start == end else start + " ~ " + end
            state = "기본: 확인 필요" if not row["states"][0] else "기본: 주소 계산됨"
            state += " / " + ("현재: 확인 필요" if not row["states"][1] else "현재: 주소 계산됨")
            values = (row["source_start"], row["source_end"], row["points"],
                      interval(row["initial_start"], row["initial_end"]),
                      interval(row["target_start"], row["target_end"]), state, ", ".join(row["pous"]))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip("주소 계산 결과입니다. 적용하려면 매핑 전체 검사를 수행하세요.\n" + "\n".join(error for error in row["errors"] if error))
                self.preview_table.setItem(index, column, item)
        self.preview_table.resizeColumnsToContents()
        self.highlight_source_selection()

    def refresh(self, *, notify_edit=True):
        if notify_edit:
            self.edited.emit()
        self.update_policy_hint()
        rows = range_rows(self.draft, Bounds(), Bounds(), allow_target_aliases=True)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                self.table.setItem(i, j, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
        self.table.blockSignals(False)
        self.restore_selection()
        old = {r.rule_id: r for r in self.baseline.rules}
        new = {r.rule_id: r for r in self.draft.rules}
        changes = [f"{key}: {old.get(key)} → {new.get(key)}" for key in old.keys() | new.keys() if old.get(key) != new.get(key)]
        if self.baseline.unmapped != self.draft.unmapped:
            changes.append(f"미매핑 정책: {self.baseline.unmapped.value} → {self.draft.unmapped.value}")
        if self.baseline.module_rules != self.draft.module_rules:
            changes.append(f"U 모듈 규칙: {self.baseline.module_rules} → {self.draft.module_rules}")
        self.diff.setPlainText("전체 초안 차이\n" + ("\n".join(changes) or "변경 없음"))
        self.refresh_module_preview()
        self.refresh_usage_preview()

    def require_committed(self):
        if self.pending() or self.module_pending():
            raise ValueError("입력 중인 X/Y 행과 U 규칙을 반영하거나 취소하세요")
        range_rows(self.draft, Bounds(), Bounds(), allow_target_aliases=True)
        self.module_mapper(self.draft)

    def apply(self):
        def action():
            self.require_committed()
            self.status.setText("전체 검사 중입니다. 매핑 설정 파일은 별도로 저장할 수 있습니다.")
            self.applied.emit(self.draft.to_json().decode("utf-8"))
        self.run(action)

    def load(self):
        def action():
            self.require_committed()
            path, _ = QFileDialog.getOpenFileName(self, "매핑 설정 불러오기", "", "매핑 설정 (*.json)")
            if not path:
                return
            payload, _ = read_settings_snapshot(Path(path))
            candidate = MappingSettings.from_json(payload, expected_source=self.draft.source, expected_target=self.draft.target)
            range_rows(candidate, Bounds(), Bounds(), allow_target_aliases=True)
            self.module_mapper(candidate)
            self.draft = candidate
            self.policy.blockSignals(True)
            for i in range(self.policy.count()):
                if self.policy.itemData(i) == candidate.unmapped:
                    self.policy.setCurrentIndex(i)
            self.policy.blockSignals(False)
            self.new_row()
            self.new_module_row()
            self.refresh()
            self.status.setText(f"매핑 설정을 초안으로 불러왔습니다. 출처: {path}. 적용하려면 전체 검사를 수행하세요.")
        self.run(action)

    def save(self):
        def action():
            self.require_committed()
            path, _ = QFileDialog.getSaveFileName(self, "매핑 설정을 새 파일로 저장", "", "매핑 설정 (*.json)")
            if not path:
                return
            path = core._reject_link_like_ancestors(Path(path), "new settings")
            save_new_settings(path, self.draft, Bounds(), Bounds(), allow_target_aliases=True)
            self.status.setText("매핑 설정을 저장했습니다. 적용하려면 전체 검사를 수행하세요.")
        self.run(action)

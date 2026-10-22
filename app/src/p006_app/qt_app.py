"""Selected Qt Widgets frontend for the P006 guided conversion workspace."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import sys
import uuid

from PySide6.QtCore import QSignalBlocker, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from shiboken6 import isValid
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                              QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                              QPushButton, QProgressBar, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
                              QTabWidget, QDialog, QSizePolicy, QMessageBox)
from p006_app.qt_runtime import Runtime, arguments
from p006_app.qt_preferences import Preferences
from p006_app.qt_settings import AddressDialog
from p006_app.qt_inspector import Inspector
from p006_app.generation_guidance import (EXPLICIT_EXECUTION_REQUEST, Q_CPUS,
                                         diagnostic_attention, generation_guidance,
                                         result_role_summary, diagnostic_recovery, diagnostic_category)
from p006_converter.fx5_target import CPUS
from p006_converter import statement_policy as statements

STYLE = """
QWidget { font-family: 'Malgun Gothic', 'Segoe UI'; font-size: 14px; color: #243247; }
QMainWindow, QWidget#workspace { background: #f3f5f8; }
QDialog { background: #f8fafc; }
QFrame#card { background: white; border: 1px solid #e4e9f0; border-radius: 10px; }
QLabel#brand { color: #182b49; font-size: 22px; font-weight: 700; }
QLabel#heading { color: #182b49; font-size: 17px; font-weight: 700; }
QLabel#subtle { color: #64748b; font-size: 12px; }
QLabel#warning { color: #805c24; background: #fffbf1; border: 1px solid #f0e2c6; padding: 6px; border-radius: 6px; }
QLabel#icon { color: #2563eb; background: #eff6ff; padding: 12px; border-radius: 8px; font-weight: 700; }
QPushButton { background: #ffffff; border: 1px solid #dbe2eb; border-radius: 7px; color: #334155; padding: 6px 12px; }
QPushButton:hover { background: #f1f5f9; border-color: #b7c5d8; }
QPushButton:pressed { background: #e2e8f0; border-color: #94a3b8; }
QPushButton:focus { border-color: #2563eb; }
QPushButton#primary { background: #2563eb; border-color: #2563eb; color: white; font-weight: 700; padding: 7px 20px; }
QPushButton#primary:hover { background: #1d4ed8; border-color: #1d4ed8; }
QPushButton#primary:pressed { background: #1e40af; border-color: #1e40af; }
QPushButton#primary:focus { border-color: #102f74; }
QPushButton#saveDraft { color: #1d4ed8; background: #eff6ff; border-color: #dbeafe; font-weight: 700; }
QPushButton#saveDraft:hover { background: #dbeafe; border-color: #93c5fd; }
QPushButton#quiet, QPushButton#link, QPushButton#removeRule { background: transparent; border-color: transparent; }
QPushButton#link { color: #2563eb; text-align: left; }
QPushButton#quiet:hover, QPushButton#link:hover { background: #eaf0f7; border-color: #eaf0f7; }
QPushButton#removeRule { color: #64748b; }
QPushButton#removeRule:hover { color: #b42335; background: #fff1f2; border-color: #ffe4e6; }
QPushButton#quiet:focus, QPushButton#link:focus, QPushButton#removeRule:focus { border-color: #2563eb; }
QPushButton:disabled, QPushButton#primary:disabled, QPushButton#saveDraft:disabled,
QPushButton#quiet:disabled, QPushButton#removeRule:disabled { color: #94a3b8; background: #f1f4f8; border-color: #e8edf3; }
QLineEdit, QComboBox { border: 1px solid #d4dde8; border-radius: 6px; background: white; padding: 6px 8px; selection-background-color: #dbeafe; selection-color: #172b4d; }
QLineEdit:hover, QComboBox:hover { border-color: #94a3b8; }
QLineEdit:focus, QComboBox:focus { border-color: #2563eb; background: #fbfdff; }
QLineEdit:disabled, QComboBox:disabled { background: #f1f4f8; color: #94a3b8; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #d4dde8; selection-background-color: #eff6ff; selection-color: #1d4ed8; }
QTabWidget::pane { background: white; border: 1px solid #e4e9f0; border-radius: 8px; top: -1px; }
QTabBar::tab { color: #64748b; background: transparent; border: none; border-bottom: 3px solid transparent; padding: 9px 14px; margin-right: 4px; }
QTabBar::tab:hover { color: #1d4ed8; background: #edf3fc; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { color: #1d4ed8; background: #eff6ff; border-bottom-color: #2563eb; font-weight: 700; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:focus { border-bottom-color: #102f74; }
QWidget#mappingEditor QPushButton, QWidget#mappingEditor QLineEdit, QWidget#mappingEditor QComboBox { padding: 4px 8px; }
QWidget#mappingEditor QTabBar::tab { padding: 5px 14px; }
QTableView { background: white; alternate-background-color: #f8fafc; border: 1px solid #e4e9f0; border-radius: 6px; gridline-color: #edf1f6; selection-background-color: #eaf2ff; selection-color: #163d7a; }
QTableView::item { padding: 3px; border-bottom: 1px solid #f0f3f7; }
QHeaderView::section { color: #52647b; background: #f5f7fa; border: none; border-bottom: 1px solid #e4e9f0; padding: 5px 7px; font-weight: 700; }
QTableCornerButton::section { background: #f5f7fa; border: none; }
QTextEdit { border: 1px solid #e4e9f0; border-radius: 6px; background: #f8fafc; font-size: 12px; padding: 4px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #f5f7fa; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #c4cedb; min-height: 24px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #94a3b8; }
QScrollBar:horizontal { background: #f5f7fa; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #c4cedb; min-width: 24px; border-radius: 4px; margin: 2px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: #edf1f6; }
QProgressBar { border: none; background: #e8effa; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #3b82f6; border-radius: 3px; }
QToolTip { color: #334155; background: white; border: 1px solid #dbe2eb; padding: 5px; }
"""


def label(text, name="", bold=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setMinimumWidth(0)
    widget.setObjectName(name)
    if bold:
        font = widget.font()
        font.setBold(True)
        widget.setFont(font)
    return widget


def diagnostic_detail_text(detail):
    """Structured engine evidence belongs in the report, not an operator heading."""
    if not isinstance(detail, str):
        return ""
    try:
        value = json.loads(detail)
    except (ValueError, TypeError):
        return detail
    return "" if isinstance(value, (dict, list)) else detail


def card():
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return widget, layout


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


class SourceSettingsDialog(QDialog):
    """Keep file drops routed through the workspace source gate in advanced UI."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        self.parent().dragEnterEvent(event)

    def dropEvent(self, event):
        self.parent().dropEvent(event)


class Workspace(QMainWindow):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.session = runtime.session
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="p006-qt-mutations")
        self.snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="p006-qt-identity")
        self.snapshot_future = None
        self.folder_future = None
        self.busy = False
        self.operation_error = None
        self.auto_prepare_pending = False
        self.futures = []
        self.generation = 0
        self.request_id = None
        self.version = -1
        self.result = None
        self.reported = False
        self.closing = False
        self.close_future = None
        self.publishing = False
        self.command = "prepare"
        self.applied_settings = None
        self.pending_settings = None
        self.pending_settings_request = None
        self.queued_saved_rule_check = None
        self.address_dialog = None
        self.editor_settings_json = None
        self.external_mapping_result = None
        self.current_source = runtime.args.source
        self.prefs = Preferences(getattr(runtime.args, "preferences", None)) if runtime.framework == "QT_WIDGETS" else None
        from p006_app.branding import APP_NAME, application_version
        self.setWindowTitle(f"{APP_NAME} v{application_version()} | 변환 작업공간")
        self.setMinimumSize(640, 420)
        preferred = self.prefs.size() if self.prefs else (1050, 840)
        available = self.screen().availableGeometry()
        # Leave room for the Windows title bar and resize frame. Stored sizes
        # from another display must not place the primary action off screen.
        self.resize(min(preferred[0], available.width() - 32),
                    min(preferred[1], available.height() - 48))
        self.setAcceptDrops(True)
        central = QWidget()
        central.setObjectName("workspace")
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central)
        root = QWidget()
        root.setObjectName("workspace")
        central_layout.addWidget(root, 1)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(label(APP_NAME, "brand"))
        header.addWidget(label("GX 변환 작업공간"))
        header.addStretch()
        header.addWidget(label("Q/FX5 양방향 변환", "subtle"))
        self.header_widgets = [header.itemAt(i).widget() for i in range(header.count())
                               if header.itemAt(i).widget()]
        self.update_button = QPushButton("업데이트 확인")
        header.addWidget(self.update_button)
        from p006_app.qt_update import UpdateNotifier
        self.updates = UpdateNotifier(self, self.update_button, application_version())
        layout.addLayout(header)
        self.stages = label("원본을 선택하면 주소 사용 현황을 자동 분석합니다.", "subtle")
        self.stages.hide()
        self.navigation_revision = 0
        self.navigation_request = None
        self.next_prepare_stage = None
        self.mapping_checked = False
        self.mapping_dirty = False
        self.mapping_unapplied = False
        self.mapping_revision = 0
        self.validation_revision = None
        self.workflow_tabs = QTabWidget()
        self.workflow_tabs.setAccessibleName("변환 작업 3단계")
        self.pages = []
        for title in ("1 파일 등록", "2 주소·규칙", "3 결과"):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(6, 6, 6, 6)
            self.workflow_tabs.addTab(page, title)
            self.pages.append(page_layout)
        layout.addWidget(self.workflow_tabs, 1)
        self.safety = label("PLC 다운로드와 운전 금지. 검토용 대체 명령은 원래 기능을 구현하지 않습니다. 파일 생성은 GX 가져오기 성공이나 정상 운전의 증명이 아닙니다.", "warning")
        source, source_layout = card()
        row = QHBoxLayout()
        row.addWidget(label("현재 원본", "heading"))
        row.addStretch()
        self.file_detail = QPushButton("상세 설정")
        self.file_detail.setObjectName("link")
        row.addWidget(self.file_detail)
        self.pick = QPushButton("원본 바꾸기")
        self.pick.setObjectName("link")
        row.addWidget(self.pick)
        source_layout.addLayout(row)
        file_row = QHBoxLayout()
        self.source_icon = label("GX", "icon")
        file_row.addWidget(self.source_icon)
        file_text = QVBoxLayout()
        self.source_name = label("원본을 선택하세요", bold=True)
        self.direction = label("GX Works3 .gx3 / GX Works2 .gxw", "subtle")
        file_text.addWidget(self.source_name)
        file_text.addWidget(self.direction)
        file_row.addLayout(file_text, 1)
        source_layout.addLayout(file_row)
        self.source_path = label("", "subtle")
        source_layout.addWidget(self.source_path)
        self.recent = QComboBox()
        self.recent.addItem("최근 원본 선택 또는 파일을 이 창에 놓으세요", None)
        for path in self.prefs.recent() if self.prefs else []:
            self.recent.addItem(path, path)
        self.recent.setAccessibleName("최근 원본 선택")
        source_layout.addWidget(self.recent)
        self.cpu_heading = label("실제 대상 CPU", bold=True)
        source_layout.addWidget(self.cpu_heading)
        self.cpu = QComboBox()
        self.cpu.addItems(CPUS)
        self.cpu.setAccessibleName("실제 대상 CPU")
        source_layout.addWidget(self.cpu)
        self.actual_q_cpu = QComboBox()
        self.actual_q_cpu.addItems(Q_CPUS)
        self.actual_q_cpu.setAccessibleName("실제 대상 Q CPU 모델")
        self.actual_q_cpu.hide()
        source_layout.addWidget(self.actual_q_cpu)
        self.cpu_note = label("FX5U는 기본값입니다. 원본에서 감지한 CPU가 아닙니다.", "subtle")
        source_layout.addWidget(self.cpu_note)
        self.detail_toggle = QPushButton("상세 설정")
        self.detail_toggle.setObjectName("link")
        self.source_detail = SourceSettingsDialog(self)
        self.source_detail.setWindowTitle("상세 설정")
        self.source_detail.resize(650, 450)
        detail = QVBoxLayout(self.source_detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.addWidget(label("원본의 전체 로컬 경로"))
        self.path = QLineEdit(runtime.args.source)
        self.path.setAccessibleName("원본의 전체 로컬 경로")
        detail.addWidget(self.path)
        detail.addWidget(label("작업 목적"))
        self.mode = QComboBox()
        self.mode.addItem("변환 분석과 계획", "convert")
        self.mode.addItem("Q 원본 대조 (GXW 전용)", "source_review")
        self.mode.setAccessibleName("작업 목적")
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.mode)
        mode_row.addWidget(label("Statement"))
        self.statement_policy = QComboBox()
        self.statement_policy.addItem("In PLC (기본)", statements.IN_PLC)
        self.statement_policy.addItem("원본 유형 유지", statements.PRESERVE_SOURCE)
        self.statement_policy.setCurrentIndex(self.statement_policy.findData(
            getattr(runtime.args, "statement_policy", statements.IN_PLC)))
        self.statement_policy.setAccessibleName("Statement 저장 유형")
        mode_row.addWidget(self.statement_policy)
        detail.addLayout(mode_row)
        self.statement_notice = label(statements.MIGRATION_NOTICE, "subtle")
        detail.addWidget(self.statement_notice)
        policy_buttons = QHBoxLayout()
        self.load_statement = QPushButton("Statement 설정 불러오기")
        self.save_statement = QPushButton("Statement 설정 저장")
        policy_buttons.addWidget(self.load_statement)
        policy_buttons.addWidget(self.save_statement)
        detail.addLayout(policy_buttons)
        self.edit_addresses = QPushButton("사용 주소와 모듈 매핑 편집")
        mapping_layout = self.pages[1]
        self.mapping_state = label("매핑: 원본 분석 필요", "subtle")
        self.mapping_state.setAccessibleName("매핑 초안, 검사 및 적용 상태와 출처")
        self.edit_addresses.hide()
        self.detail_toggle.hide()
        self.mapping_placeholder = label("원본 분석 후 X/Y와 U 모듈의 최초안, 변경안과 편집 규칙이 표시됩니다.")
        mapping_layout.addWidget(self.mapping_placeholder)
        self.mapping_host = QVBoxLayout()
        mapping_layout.addLayout(self.mapping_host, 1)
        detail.addWidget(label("D → R 자동 치환과 자동 할당은 제공하지 않습니다. 기존 JSON을 명시적으로 선택하며 전체 검사를 다시 실행합니다.", "subtle"))
        self.advanced = {}
        self.advanced_buttons = []
        for key, title in (("xy", "X/Y 및 U 설정 JSON"), ("word", "ZR → R 설정 JSON"), ("relocation", "원본 SHA에 결속된 D/T/ZR 재배치 JSON")):
            detail.addWidget(label(title))
            row = QHBoxLayout()
            field = QLineEdit()
            field.setAccessibleName(title)
            self.advanced[key] = field
            row.addWidget(field, 1)
            button = QPushButton("선택")
            button.setAccessibleName(title + " 선택")
            button.clicked.connect(lambda checked=False, f=field: self.choose_settings(f))
            self.advanced_buttons.append(button)
            row.addWidget(button)
            detail.addLayout(row)
        self.reset_preferences = QPushButton("이 앱의 최근 경로와 창 크기 초기화")
        detail.addWidget(self.reset_preferences)
        self.source_detail.hide()
        self.pages[0].addWidget(source)
        self.pages[0].addStretch()
        self.context = label("원본 미선택", "subtle")
        self.context.setWordWrap(False)
        self.context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.insertWidget(1, self.context)
        self.target_summary = label("파일을 선택하면 대상 CPU와 저장 위치를 여기에 표시합니다.")
        self.target_summary.setWordWrap(True)
        self.identifier = QLineEdit()
        self.identifier.setText(getattr(runtime.args, "identifier", ""))
        self.identifier.setAccessibleName("결과 이름")
        self.output_parent = QLineEdit()
        self.output_parent.setText(getattr(runtime.args, "output_parent", ""))
        self.output_parent.setAccessibleName("결과 저장 위치")
        self.choose_output = QPushButton("위치 선택")
        self.destination_preview = label("원본을 선택하면 새 결과 폴더를 안내합니다.", "subtle")
        self.destination_preview.setWordWrap(True)
        self.problem_summary = label("해결할 문제는 검사 후 여기에 표시됩니다.", "subtle")
        self.problem_summary.setAccessibleName("해결할 문제 요약")
        self.problem_summary.setWordWrap(False)
        self.problem_summary.setMinimumWidth(0)
        self.problem_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.generation_notice = label("파일을 선택한 뒤 주소 규칙을 확인하고 변환하세요.", "subtle")
        self.generation_notice.setAccessibleName("결과 생성 가능 여부와 다음 행동")
        self.generation_notice.setWordWrap(True)
        axes_box, axes = card()
        self.readiness = label("미확인", bold=True)
        self.generation_state = label("미생성", bold=True)
        for title, value in [("파일 생성", self.generation_state),
                             ("GX 가져오기 준비", self.readiness),
                             ("실제 장비 동작 검증", label("NOT_RUN", bold=True))]:
            row = QHBoxLayout()
            row.addWidget(label(title, "subtle"))
            row.addWidget(value, 1)
            axes.addLayout(row)
        action, actions = card()
        actions.addWidget(self.target_summary)
        actions.addWidget(self.mapping_state)
        name_row = QHBoxLayout()
        name_row.addWidget(label("결과 이름", "subtle"))
        name_row.addWidget(self.identifier, 1)
        self.cpu_summary = label("대상 CPU 미입력", "subtle")
        self.cpu_summary.setAccessibleName("변환에 사용하는 대상 CPU")
        name_row.addWidget(self.cpu_summary)
        actions.addLayout(name_row)
        place_row = QHBoxLayout()
        place_row.addWidget(label("저장 위치", "subtle"))
        place_row.addWidget(self.output_parent, 1)
        place_row.addWidget(self.choose_output)
        actions.addLayout(place_row)
        actions.addWidget(self.destination_preview)
        actions.addWidget(self.generation_notice)
        self.status = label("원본을 선택하면 자동 분석합니다.")
        self.status.setAccessibleDescription("분석 진행 상태와 다음 행동")
        self.status.setWordWrap(False)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        actions.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.retry, self.cancel, close = [QPushButton(t) for t in ("분석 다시 시도", "취소 (Esc)", "앱 종료")]
        self.generate = QPushButton("원본을 선택하세요")
        self.generate.setObjectName("primary")
        self.generate.setEnabled(False)
        self.generate.setMinimumWidth(220)
        close.setObjectName("quiet")
        self.cancel.setObjectName("quiet")
        buttons.addWidget(close)
        buttons.addWidget(self.problem_summary, 1)
        buttons.addWidget(self.status, 1)
        self.cancel.setEnabled(False)
        for widget in (self.generate, self.retry, self.cancel, close):
            widget.setAutoDefault(False)
        for widget in (self.cancel, self.retry, self.generate):
            buttons.addWidget(widget)
        actions.addLayout(buttons)
        actions.addWidget(self.safety)
        central_layout.addWidget(action)
        self.results = QWidget()
        self.results.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        result_layout = QVBoxLayout(self.results)
        result_layout.setContentsMargins(0, 0, 0, 0)
        self.summary = label("")
        result_layout.addWidget(self.summary)
        self.recovery = label("", "warning")
        self.recovery_box = QWidget()
        recovery_layout = QHBoxLayout(self.recovery_box)
        recovery_layout.setContentsMargins(0, 0, 0, 0)
        recovery_layout.addWidget(self.recovery, 1)
        recovery_button = QPushButton("복구 상세 보기")
        recovery_layout.addWidget(recovery_button)
        self.recovery_dialog = QDialog(self)
        self.recovery_dialog.setWindowTitle("저장 오류와 남은 파일")
        self.recovery_dialog.resize(800, 500)
        recovery_detail_layout = QVBoxLayout(self.recovery_dialog)
        self.recovery_detail = QTextEdit()
        self.recovery_detail.setReadOnly(True)
        self.recovery_detail.setAccessibleName("저장 오류와 남은 파일 상세")
        recovery_detail_layout.addWidget(self.recovery_detail)
        recovery_button.clicked.connect(self.recovery_dialog.show)
        self.recovery_box.hide()
        result_layout.addWidget(self.recovery_box)
        result_actions = QHBoxLayout()
        self.inspect_all = QPushButton("문제와 원문 검토")
        self.open_result = QPushButton("결과 폴더 열기")
        self.guide_button = QPushButton("GX 안내 보기")
        for button in (self.inspect_all, self.open_result, self.guide_button):
            result_actions.addWidget(button)
        self.mapping_back = QPushButton("주소·규칙으로 돌아가기")
        self.mapping_back.clicked.connect(lambda: self.workflow_tabs.setCurrentIndex(1))
        result_actions.addWidget(self.mapping_back)
        result_layout.addLayout(result_actions)
        result_layout.addWidget(label("원문 보존과 투영은 대상 반영, 정상 운전의 증명이 아닙니다.", "subtle"))
        self.grid = QGridLayout()
        self.issues_card, self.issues = card()
        self.files_card, self.files = card()
        self.issues_scroll = self.local_scroll(self.issues_card)
        self.files_scroll = self.local_scroll(self.files_card)
        self.grid.addWidget(self.issues_scroll, 0, 0)
        self.grid.addWidget(self.files_scroll, 1, 0)
        self.grid.setColumnStretch(0, 1)
        self.grid.setRowStretch(0, 1)
        self.grid.setRowStretch(1, 1)
        self.result_layout = result_layout
        self.result_details_slot = result_layout.count()
        self.result_details = QWidget()
        self.result_details.setLayout(self.grid)
        result_layout.addWidget(self.result_details, 1)
        self.result_details_button = QPushButton("진단과 POU 상세 보기")
        self.result_details_button.setAutoDefault(False)
        detail_actions = QHBoxLayout()
        detail_actions.addWidget(self.result_details_button)
        result_layout.addLayout(detail_actions)
        self.result_details_dialog = QDialog(self)
        self.result_details_dialog.setWindowTitle("진단과 POU 상세")
        details_layout = QVBoxLayout(self.result_details_dialog)
        details_close = QPushButton("결과로 돌아가기")
        details_close.clicked.connect(self.result_details_dialog.hide)
        details_layout.addWidget(details_close)
        self.result_details_button.clicked.connect(self.show_result_details)
        report_toggle = QPushButton("원본, 승인 결속과 보고서 상세")
        detail_actions.addWidget(report_toggle)
        self.report_dialog = QDialog(self)
        self.report_dialog.setWindowTitle("원본과 보고서 상세")
        self.report_dialog.resize(800, 550)
        report_layout = QVBoxLayout(self.report_dialog)
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setAccessibleName("원본과 보고서 상세")
        report_layout.addWidget(self.report)
        report_toggle.clicked.connect(self.report_dialog.show)
        guide, guide_layout = card()
        self.guide = guide
        guide_layout.addWidget(label("GX에서 확인할 순서", "heading"))
        guide_layout.addWidget(label("이번 출력의 공식 GX 검증은 NOT_RUN_FOR_THIS_OUTPUT입니다. 수행 메모는 PASS 증거가 되지 않습니다.", "subtle"))
        self.guide_direction = label("")
        guide_layout.addWidget(self.guide_direction)
        for step in ("원본 프로젝트 복제와 백업", "실제 CPU와 Device 메모리 할당 확인", "Q 한글은 Project Language Korean 설정 후 원본 CSV 다시 가져오기",
                     "가져오기 시험 후보의 role 확인", "Check Program과 Build/Rebuild 적용성 확인",
                     "저장, 새 프로세스로 재열기와 공식 export 대조", "별도 현장 검증 승인"):
            guide_layout.addWidget(label("도움말: " + step, "subtle"))
        guide_layout.addWidget(label("원문 대조와 typed 미확정 자료는 가져오기 대상이 아닙니다. label 없는 Q의 Rebuild 적용성은 미결입니다. 이 도움말을 수행해도 검증 PASS가 되지 않습니다.", "subtle"))
        self.guide_scroll = self.local_scroll(guide)
        self.guide_scroll.hide()
        self.pages[2].addWidget(axes_box)
        self.pages[2].addWidget(self.results, 1)
        self.pages[2].addWidget(self.guide_scroll)
        self.results.hide()
        self.open_result.setEnabled(False)
        # File URLs may also contain text. Route drops through the source gate,
        # rather than letting an editor insert the URL into CPU/settings/output.
        for field in (self.actual_q_cpu, self.path, *self.advanced.values(),
                      self.identifier, self.output_parent):
            field.setAcceptDrops(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(250)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.prepare)
        self.rule_check = QTimer(self)
        self.rule_check.setSingleShot(True)
        self.rule_check.timeout.connect(self.run_saved_rule_check)
        self.path.textChanged.connect(self.changed)
        self.cpu.currentTextChanged.connect(self.changed)
        self.actual_q_cpu.currentTextChanged.connect(self.changed)
        self.mode.currentIndexChanged.connect(self.changed)
        self.statement_policy.currentIndexChanged.connect(self.changed)
        self.load_statement.clicked.connect(self.load_statement_settings)
        self.save_statement.clicked.connect(self.save_statement_settings)
        for field in self.advanced.values():
            field.textChanged.connect(self.changed)
        self.identifier.textChanged.connect(self.changed)
        self.output_parent.textChanged.connect(self.changed)
        self.path.returnPressed.connect(self.prepare)
        self.retry.clicked.connect(self.prepare)
        self.generate.clicked.connect(self.publish)
        self.cancel.clicked.connect(self.cancelled)
        close.clicked.connect(self.close)
        self.pick.clicked.connect(self.choose)
        self.recent.activated.connect(self.choose_recent)
        self.choose_output.clicked.connect(self.choose_destination)
        self.edit_addresses.clicked.connect(self.edit_settings)
        self.reset_preferences.clicked.connect(self.clear_preferences)
        self.inspect_all.clicked.connect(lambda: self.inspect())
        self.open_result.clicked.connect(self.open_folder)
        self.guide_button.clicked.connect(self.toggle_guide)
        self.file_detail.clicked.connect(self.source_detail.show)
        self.detail_toggle.clicked.connect(self.source_detail.show)
        self.source_detail.finished.connect(lambda: self.detail_toggle.setChecked(False))
        self.workflow_tabs.currentChanged.connect(self.navigation_changed)
        QShortcut(QKeySequence("Esc"), self, activated=self.cancel_if_busy)
        self.update_source()
        self.adapt_display()
        QTimer.singleShot(0, self.ready)
        if runtime.args.auto_close:
            QTimer.singleShot(int(runtime.args.auto_close * 1000), self.close)

    @staticmethod
    def local_scroll(widget):
        """Only lists/guide content scroll; the window and stage actions stay fixed."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def navigation_changed(self, index):
        self.navigation_revision += 1
        self.refresh_primary()

    def arm_navigation(self, body, stage):
        self.navigation_request = (body["generation"], body["request_id"], self.navigation_revision, stage)

    def finish_navigation(self, state, result):
        pending = self.navigation_request
        if not pending or pending[:2] != (state["generation"], state["request_id"]):
            return
        self.navigation_request = None
        if self.command == "publish" and result.get("file_generation") != "GENERATED":
            return
        if pending[2] == self.navigation_revision and 0 <= pending[3] < self.workflow_tabs.count():
            self.workflow_tabs.setCurrentIndex(pending[3])

    def draft_changed(self):
        self.mapping_revision += 1
        self.mapping_dirty = True
        self.mapping_checked = False
        if self.command == "validate":
            self.navigation_request = None
        self.refresh_primary()

    def toggle_guide(self):
        self.workflow_tabs.setCurrentIndex(2)
        self.guide_scroll.setVisible(not self.guide_scroll.isVisible())

    def editor_pending(self):
        editor = self.address_dialog
        return bool(editor and editor.isVisible() and (editor.pending() or editor.module_pending()))

    def review_output(self, result):
        programs = result.get("programs") or []
        readiness = (result.get("import_readiness") or {}).get("state")
        return any(p.get("role") != "TEXT_CANDIDATE" for p in programs) or readiness in {"BLOCKED", "REVIEW"}

    def schedule_saved_rule_check(self):
        if self.closing or self.publishing or self.mode.currentData() != "convert":
            return
        if self.external_mapping_result is not None or self.editor_pending():
            return
        self.rule_check.start(400)

    def run_saved_rule_check(self):
        if self.closing or self.publishing or not self.address_dialog or self.editor_pending():
            return
        if self.mode.currentData() != "convert" or self.external_mapping_result is not None:
            return
        self.apply_settings(self.address_dialog.draft.to_json().decode("utf-8"))

    def guide_pending_input(self):
        editor = self.address_dialog
        if not editor:
            return
        try:
            editor.require_committed()
        except ValueError as error:
            self.generation_notice.setText(f"변환 전에 입력을 정리하세요: {error}")
            self.status.setText("주소 규칙의 입력 변경을 저장하거나 취소한 뒤 변환하세요.")
            self.workflow_tabs.setCurrentIndex(1)
            index = 0 if editor.pending() else 1 if editor.module_pending() else editor.tabs.currentIndex()
            editor.tabs.setCurrentIndex(index)
            editor.open_rule_editor(index)

    def ensure_mapping_editor(self, result):
        if self.mode.currentData() == "source_review":
            if self.address_dialog:
                self.address_dialog.hide()
            self.mapping_placeholder.setText("원문 대조 모드입니다. 주소를 변경하지 않고 전체 검사 후 검토합니다.")
            self.mapping_placeholder.show()
            return
        incoming = result.get("xy_json")
        if self.address_dialog:
            self.address_dialog.show()
            self.mapping_placeholder.hide()
            if incoming != self.editor_settings_json and not result.get("validated_xy_json"):
                if self.mapping_dirty or self.mapping_unapplied:
                    self.external_mapping_result = result
                    self.address_dialog.status.setText("새 설정과 작성 중인 초안이 다릅니다. 초안을 보존했습니다. 전체 검사에서 사용할 설정을 선택하세요.")
                    return
                self.replace_mapping_editor(result)
            elif result.get("validated_xy_json"):
                self.editor_settings_json = incoming
                self.external_mapping_result = None
            else:
                self.external_mapping_result = None
                if self.mapping_dirty:
                    self.address_dialog.status.setText("작성 중인 초안을 보존했습니다. 수정안 저장 후 전체 검사를 수행하세요.")
            return
        if self.address_dialog is None and self.mode.currentData() == "convert" and result.get("direction") in {"Q_TO_FX5", "FX5_TO_Q"}:
            self.address_dialog = AddressDialog(self, result, embedded=True)
            self.address_dialog.set_compact(self.height() < 940)
            self.address_dialog.applied.connect(self.apply_settings)
            self.address_dialog.edited.connect(self.draft_changed)
            self.address_dialog.committed.connect(self.schedule_saved_rule_check)
            self.mapping_host.addWidget(self.address_dialog)
            self.address_dialog.corner_row.addWidget(self.detail_toggle)
            self.detail_toggle.show()
            self.mapping_placeholder.hide()
            self.address_dialog.show()
            self.editor_settings_json = incoming
            self.external_mapping_result = None

    def replace_mapping_editor(self, result):
        previous_tab = self.address_dialog.tabs.currentIndex() if self.address_dialog else 0
        if self.address_dialog:
            self.detail_toggle.setParent(self)
            self.detail_toggle.hide()
            self.mapping_host.removeWidget(self.address_dialog)
            self.address_dialog.close()
            self.address_dialog.deleteLater()
            self.address_dialog = None
        self.mapping_dirty = False
        self.mapping_checked = False
        self.mapping_unapplied = False
        self.external_mapping_result = None
        self.ensure_mapping_editor(result)
        self.address_dialog.tabs.setCurrentIndex(previous_tab)

    def choose_mapping_conflict(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("검사할 매핑 설정 선택")
        dialog.setText("불러온 설정과 작성 중인 초안이 다릅니다. 검사할 설정을 선택하세요. 취소하면 초안을 그대로 보존합니다.")
        imported = dialog.addButton("불러온 설정 사용", QMessageBox.ButtonRole.AcceptRole)
        draft = dialog.addButton("현재 초안 사용", QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        return "imported" if dialog.clickedButton() is imported else "draft" if dialog.clickedButton() is draft else "cancel"

    def ready(self):
        screen = self.screen()
        self.runtime.record("READY", boundary="QT_FIRST_EVENT_LOOP_CALLBACK", host=os.environ.get("COMPUTERNAME"),
                            os=platform.platform(), window_size=[self.width(), self.height()],
                            device_pixel_ratio=self.devicePixelRatioF(), logical_dpi=screen.logicalDotsPerInch(),
                            qt_scale_factor=os.environ.get("QT_SCALE_FACTOR", "default"))
        if self.path.text().strip():
            self.update_destination()
            self.prepare()

    def update_source(self):
        path = self.path.text().strip()
        self.source_name.setText(Path(path).name if path else "원본을 선택하세요")
        self.source_path.setText(path)
        self.direction.setText("FX5 → Q / CSV 형식 템플릿" if path.lower().endswith(".gx3") else "Q → FX5 / 실제 대상 CPU 확인 필요" if path else "GX Works3 .gx3 / GX Works2 .gxw")
        forward = path.lower().endswith(".gx3")
        self.cpu.setVisible(not forward)
        self.actual_q_cpu.setVisible(forward)
        note = ("기본 선택 Q06UDVCPU. 실제 장비 검증은 아닙니다." if forward
                else "FX5U는 기본값이며 원본에서 감지한 CPU가 아닙니다.")
        detail = ("선택 범위는 기존 제품 근거가 있는 Q06UDVCPU입니다. Q06UDV는 CSV 형식 템플릿이며 실제 CPU의 명령, 메모리와 프로젝트 할당 및 GX 검증은 별도입니다."
                  if forward else "FX5U는 기본값입니다. 원본에서 감지한 실제 대상이 아닙니다.")
        self.cpu_note.setText(note)
        self.cpu_note.setToolTip(detail)
        self.actual_q_cpu.setToolTip(detail)
        self.cpu.setToolTip(detail)
        self.show_cpu_summary()

    def show_cpu_summary(self):
        forward = (self.result or {}).get("direction") == "FX5_TO_Q" or self.path.text().strip().lower().endswith(".gx3")
        cpu = self.actual_q_cpu.currentText().strip() if forward else self.cpu.currentText().strip()
        self.cpu_summary.setText(f"대상 CPU {cpu or '미입력'}")
        self.cpu_summary.setToolTip(self.cpu_note.toolTip() or "기본 선택이며 실제 장비 검증이 아닙니다.")

    def update_destination(self):
        from p006_app.workflow_service import suggest_project_identifier
        path = self.path.text().strip()
        with QSignalBlocker(self.identifier), QSignalBlocker(self.output_parent):
            if path and not self.identifier.text():
                self.identifier.setText(suggest_project_identifier(Path(path)))
            if path and not self.output_parent.text():
                self.output_parent.setText(str(Path(path).absolute().parent))
        if self.output_parent.text():
            self.destination_preview.setText(str(Path(self.output_parent.text()) / self.identifier.text()) +
                                             "\n같은 이름의 폴더가 있으면 -2, -3 순서의 새 폴더를 생성합니다. 실제 생성 경로는 결과에 표시합니다.")

    def request_options(self):
        if self.mode.currentData() == "source_review":
            return {"identifier": self.identifier.text().strip(), "output_parent": self.output_parent.text().strip(),
                    "xy_json": None, "xy_path": "", "word_path": "", "relocation_path": ""}
        return {"identifier": self.identifier.text().strip(), "output_parent": self.output_parent.text().strip(),
                "actual_q_cpu": self.actual_q_cpu.currentText().strip(),
                "statement_policy": self.statement_policy.currentData(),
                "xy_json": self.pending_settings if self.pending_settings is not None else self.applied_settings,
                **{key + "_path": "" if key == "xy" and self.pending_settings is not None else field.text().strip()
                   for key, field in self.advanced.items()}}

    def refresh_primary(self):
        review = self.mode.currentData() == "source_review"
        self.show_cpu_summary()
        self.statement_policy.setEnabled(not review and not self.busy and not self.publishing)
        self.load_statement.setEnabled(not review and not self.busy and not self.publishing)
        self.save_statement.setEnabled(not review and not self.busy and not self.publishing)
        if review:
            self.statement_notice.setText("원문 대조에서는 Statement 변환 정책을 적용하지 않습니다.")
        result = self.result
        stage = self.workflow_tabs.currentIndex()
        idle = not self.busy and not self.futures and not self.publishing and not self.closing
        origins = (result or {}).get("settings_origins", [])
        origin = ", ".join(str(o.get("path", "")) for o in origins) or (
            self.advanced["xy"].text().strip() or "앱의 주소 편집 초안")
        mapping_state = ("적용 완료" if self.mapping_checked and not self.mapping_dirty and not self.mapping_unapplied
                         and self.applied_settings is not None else
                         "검사 완료 (보류 초안, 적용 안 됨)" if self.mapping_checked and self.mapping_unapplied else
                         "불러온 초안, 검사 필요" if origins or self.advanced["xy"].text().strip() else "초안, 검사 필요")
        self.mapping_state.setText(f"매핑: {mapping_state} / 출처: {origin}")
        if self.address_dialog:
            self.address_dialog.setAccessibleDescription(self.mapping_state.text())
            self.address_dialog.preview_summary.setToolTip(self.mapping_state.text())
        # Source review keeps a hidden convert draft. Its warning does not apply there.
        guidance = generation_guidance(result, idle=idle, dirty=self.mapping_dirty and not review,
            q_cpu=self.actual_q_cpu.currentText(), pending_input=self.editor_pending() and not review,
            source_selected=bool(self.path.text().strip()))
        self.generation_notice.setText(self.operation_error or guidance)
        self.generation_notice.setToolTip(self.generation_notice.text())
        self.status.setToolTip(self.status.text())
        self.problem_summary.setToolTip(self.problem_summary.text())
        completed = bool(result and result.get("file_generation") == "GENERATED" and result.get("output_dir"))
        if completed:
            self.generate.setText("결과 폴더 열기")
            self.generate.setEnabled(idle and self.folder_future is None)
            return
        if not result and self.operation_error:
            self.generate.setText("분석 다시 시도")
            self.generate.setEnabled(bool(self.path.text().strip()) and not self.busy and not self.futures and not self.closing)
            return
        if not result or stage != 1:
            self.generate.setText("주소·규칙으로 이동" if result else "원본을 선택하세요" if not self.path.text().strip() else "변환 준비 중")
            self.generate.setEnabled(bool(result) and idle)
            return
        if self.mode.currentData() == "convert" and (
                self.editor_pending() or self.mapping_dirty or self.external_mapping_result is not None):
            self.generate.setText("규칙 저장 필요" if self.editor_pending() else "저장한 규칙 검사")
            self.generate.setEnabled(idle)
            return
        blocked = (not result.get("publishable_by_engine") or not result.get("binding")
                   or result.get("output_destination_validation") == "INVALID"
                   or (result.get("direction") == "FX5_TO_Q" and self.actual_q_cpu.currentText() not in Q_CPUS))
        if blocked:
            self.generate.setText("해결할 문제 보기")
            self.generate.setEnabled(idle)
            return
        if review:
            title = "원문 대조 파일 생성"
        elif self.review_output(result):
            title = "검토용 파일 생성"
        else:
            title = "변환"
        self.generate.setText(title)
        self.generate.setEnabled(idle)

    def publish(self):
        if self.publishing or self.closing or not self.generate.isEnabled():
            return
        if not self.result:
            if self.operation_error:
                self.prepare()
            return
        if self.result.get("file_generation") == "GENERATED":
            self.open_folder()
            return
        stage = self.workflow_tabs.currentIndex()
        if stage != 1:
            self.workflow_tabs.setCurrentIndex(1)
            return
        if self.mode.currentData() == "convert" and self.editor_pending():
            self.guide_pending_input()
            return
        if self.mode.currentData() == "convert" and (self.mapping_dirty or self.external_mapping_result is not None):
            if self.address_dialog and self.mode.currentData() == "convert" and self.external_mapping_result is None:
                self.apply_settings(self.address_dialog.draft.to_json().decode("utf-8"))
            elif self.address_dialog:
                self.address_dialog.apply()
            return
        if not self.result.get("publishable_by_engine") or not self.result.get("binding"):
            self.inspect(diagnostics=self.result.get("diagnostics"))
            return
        if self.result.get("output_destination_validation") == "INVALID":
            self.choose_output.setFocus()
            return
        expected = self.result["binding"]
        body = self.identity()
        body.update(path=self.path.text().strip(), cpu=self.cpu.currentText(), mode=self.mode.currentData(),
                    command="publish", expected_binding=expected,
                    execution_request=EXPLICIT_EXECUTION_REQUEST, **self.request_options())
        self.publishing = self.busy = True
        self.command = "publish"
        self.arm_navigation(body, 2)
        self.set_input_enabled(False)
        self.generate.setEnabled(False)
        self.progress.show()
        self.status.setText("표시한 원본과 설정을 다시 검사한 뒤 새 폴더에 생성합니다.")
        self.stages.setText("표시한 원본과 설정으로 새 결과 폴더를 생성하고 있습니다.")
        self.submit(body, self.session.prepare, body)

    def set_input_enabled(self, enabled):
        for widget in (self.path, self.cpu, self.mode, self.pick, self.recent, self.identifier, self.output_parent,
                       self.actual_q_cpu, self.statement_policy, self.load_statement, self.save_statement,
                       self.choose_output, self.edit_addresses, self.file_detail, *self.advanced.values()):
            widget.setEnabled(enabled)
        for button in self.advanced_buttons:
            button.setEnabled(enabled)
        if self.address_dialog:
            self.address_dialog.setEnabled(enabled)
            self.address_dialog.buttons["세션에 적용"].setEnabled(enabled and self.mode.currentData() == "convert")

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "결과를 저장할 기존 폴더 선택", self.output_parent.text())
        if path:
            self.output_parent.setText(path)
        self.choose_output.setFocus()

    def choose_recent(self, index):
        path = self.recent.itemData(index)
        if path:
            self.path.setText(path)

    def choose_settings(self, field):
        if self.publishing or self.closing:
            return
        path, _ = QFileDialog.getOpenFileName(self, "기존 주소 설정 JSON 선택", "", "JSON (*.json)")
        if path:
            field.setText(path)
        field.setFocus()

    def load_statement_settings(self):
        if self.busy or self.publishing or self.closing or self.mode.currentData() == "source_review":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Statement 설정 불러오기", "", "JSON (*.json)")
        if not path:
            return
        try:
            from p006_app.qt_workflow import settings_bytes
            payload, _ = settings_bytes(path, 4096)
            settings = statements.StatementSettings.from_json(payload)
            self.statement_policy.setCurrentIndex(self.statement_policy.findData(settings.policy))
            self.statement_notice.setText(settings.migration_notice or "Statement 설정을 불러왔습니다. 생성 전 다시 검사하세요.")
        except (ValueError, OSError) as error:
            self.statement_notice.setText("설정을 불러오지 못했습니다: " + str(error))

    def save_statement_settings(self):
        if self.busy or self.publishing or self.closing or self.mode.currentData() == "source_review":
            return
        path, _ = QFileDialog.getSaveFileName(self, "Statement 설정을 새 파일로 저장", "", "JSON (*.json)")
        if not path:
            return
        try:
            from p006_converter import core
            target = core._reject_link_like_ancestors(Path(path), "new Statement settings")
            # Only the explicit Save action writes, and never overwrites a file.
            statements.StatementSettings(self.statement_policy.currentData()).save_new(target)
            self.statement_notice.setText("Statement 설정을 새 파일로 저장했습니다.")
        except (ValueError, OSError) as error:
            self.statement_notice.setText("설정을 저장하지 못했습니다: " + str(error))

    def edit_settings(self, checked=False, *, diagnostic=None):
        if not self.result or self.mode.currentData() == "source_review":
            self.status.setText("변환 준비가 끝난 원본에서 주소를 편집하세요. 원문 대조에는 주소 변경을 적용하지 않습니다.")
            return
        self.ensure_mapping_editor(self.result)
        cpu = (self.actual_q_cpu.currentText() if self.address_dialog.direction == "FX5_TO_Q"
               else self.cpu.currentText())
        if self.address_dialog.direction != "FX5_TO_Q" or cpu in Q_CPUS:
            self.address_dialog.set_target_cpu(cpu)
        self.workflow_tabs.setCurrentIndex(1)
        self.address_dialog.show()
        if diagnostic:
            self.address_dialog.focus_diagnostic(diagnostic)

    def rule_check_busy(self):
        return (self.busy or self.publishing or self.closing or self.pending_settings_request is not None
                or any(not future.done() for _, future in self.futures))

    def flush_queued_rule_check(self):
        """Run the newest saved-rule check after the in-flight check releases the worker."""
        if self.queued_saved_rule_check is None:
            return
        if self.closing or self.publishing or self.mode.currentData() != "convert":
            self.queued_saved_rule_check = None
            return
        if not self.address_dialog or self.editor_pending() or self.external_mapping_result is not None:
            return
        if self.rule_check_busy():
            return
        payload = self.address_dialog.draft.to_json().decode("utf-8")
        self.queued_saved_rule_check = None
        if payload == self.applied_settings and self.mapping_checked and not self.mapping_dirty:
            return
        self.apply_settings(payload)

    def apply_settings(self, payload, *, target_stage=None):
        self.rule_check.stop()
        if self.mode.currentData() != "convert":
            if self.address_dialog:
                self.address_dialog.status.setText("원문 대조에서는 주소 초안을 적용하지 않습니다. 변환 모드로 돌아가서 검사하세요.")
            return
        if self.rule_check_busy():
            self.queued_saved_rule_check = payload
            if payload != self.pending_settings:
                self.mapping_dirty = True
                self.mapping_checked = False
            if self.address_dialog:
                self.address_dialog.status.setText("저장한 규칙을 대기했습니다. 현재 검사가 끝나면 이어서 검사합니다.")
            return
        self.queued_saved_rule_check = None
        if self.external_mapping_result is not None:
            choice = self.choose_mapping_conflict()
            if choice == "cancel":
                self.address_dialog.status.setText("검사를 취소했습니다. 불러온 설정과 작성 중인 초안을 보존했습니다.")
                return
            if choice == "imported":
                self.replace_mapping_editor(self.external_mapping_result)
                payload = self.address_dialog.draft.to_json().decode("utf-8")
            self.external_mapping_result = None
        self.pending_settings = payload
        self.next_prepare_stage = target_stage
        self.prepare()

    def inspect(self, *, pou=None, diagnostics=None):
        if self.result:
            origin = QApplication.focusWidget()
            dialog = Inspector(self, self.result, pou=pou, diagnostics=diagnostics)
            dialog.exec()
            if origin is not None and isValid(origin) and origin.isVisible() and origin.isEnabled():
                origin.setFocus()
            else:
                self.workflow_tabs.setCurrentIndex(2)
                self.inspect_all.setFocus()

    def open_folder(self):
        if self.folder_future is not None or self.closing or not self.result:
            return
        completed = self.result.get("file_generation") == "GENERATED"
        path = self.result.get("output_dir") if completed else (
            self.result.get("preserved_folder") if self.result.get("remaining_files") else None)
        if not path:
            self.status.setText("생성한 결과 폴더가 없습니다. 검사 결과와 실패 또는 취소 안내를 확인하세요.")
            return
        # Path inspection can block on a network/OneDrive volume. Only the URL
        # dispatch belongs to the Qt thread, after the worker completes.
        def inspect_folder():
            from p006_converter import core
            folder = core._reject_link_like_ancestors(Path(path), "result")
            if not folder.is_dir():
                raise ValueError("폴더가 이동 또는 삭제되어 찾을 수 없습니다")
            return folder
        self.folder_future = (self.result, path, self.executor.submit(inspect_folder))
        self.open_result.setEnabled(False)
        self.status.setText(("결과 폴더" if completed else "미완료 파일 보존 폴더") + f" 확인 중: {path}")

    def poll_folder(self):
        if self.folder_future is None:
            return
        result, path, future = self.folder_future
        if not future.done():
            return
        self.folder_future = None
        if result is not self.result or self.closing:
            if not self.closing and self.result:
                completed = self.result.get("file_generation") == "GENERATED" and self.result.get("output_dir")
                partial = self.result.get("file_generation") in {"FAILED", "CANCELLED"} and self.result.get("preserved_folder") and self.result.get("remaining_files")
                self.open_result.setEnabled(bool(completed or partial))
                self.refresh_primary()
            return
        try:
            folder = future.result()
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError("Windows에서 폴더를 열지 못했습니다")
            self.status.setText(f"폴더 열기 요청 완료: {folder}")
        except (OSError, ValueError) as error:
            self.status.setText(f"폴더를 열 수 없습니다: {error}. 실제 경로: {path}")
        self.open_result.setEnabled(True)
        self.refresh_primary()

    def clear_preferences(self):
        if self.prefs:
            self.prefs.clear()
        self.recent.clear()
        self.recent.addItem("최근 경로가 초기화되었습니다. 원본 파일은 보존됩니다.", None)
        self.status.setText("이 앱의 최근 경로와 저장된 창 크기를 초기화했습니다.")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if self.publishing or self.closing:
            self.status.setText("현재 작업의 정리를 기다린 뒤 원본 바꾸기로 파일을 선택하세요.")
        elif len(urls) != 1 or not urls[0].isLocalFile():
            self.status.setText("이 PC의 원본 파일 하나만 놓을 수 있습니다. 원본 바꾸기에서 .gx3 또는 .gxw 파일을 선택하세요.")
        elif Path(urls[0].toLocalFile()).suffix.lower() not in {".gx3", ".gxw"}:
            self.status.setText("지원하지 않는 형식입니다. 원본 바꾸기에서 GX Works3 .gx3 또는 GX Works2 .gxw 파일을 선택하세요.")
        else:
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not self.publishing and not self.closing and len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() in {".gx3", ".gxw"}:
            self.path.setText(urls[0].toLocalFile())
            event.acceptProposedAction()

    def identity(self):
        self.operation_error = None
        self.generation += 1
        self.request_id = str(uuid.uuid4())
        self.version = -1
        self.result = None
        self.mapping_checked = False
        self.navigation_request = None
        self.queued_saved_rule_check = None
        self.rule_check.stop()
        self.results.hide()
        self.readiness.setText("미확인")
        self.problem_summary.setText("현재 계획 미확인")
        self.generation_state.setText("미생성")
        self.generate.setEnabled(False)
        self.open_result.setEnabled(False)
        self.result_details_dialog.hide()
        self.recovery_dialog.hide()
        self.recovery_detail.clear()
        self.reported = False
        return {"protocol": 1, "generation": self.generation, "request_id": self.request_id}

    def submit(self, identity, callback, *args):
        self.futures.append((identity["generation"], self.executor.submit(callback, *args)))

    def changed(self, *_args):
        if self.closing:
            return
        if self.publishing:
            return
        self.pending_settings_request = None
        self.pending_settings = None
        if self.address_dialog:
            cpu = (self.actual_q_cpu.currentText() if self.address_dialog.direction == "FX5_TO_Q"
                   else self.cpu.currentText())
            if self.address_dialog.direction != "FX5_TO_Q" or cpu in Q_CPUS:
                self.address_dialog.set_target_cpu(cpu)
        body = self.identity()
        if self.current_source != self.path.text().strip():
            self.current_source = self.path.text().strip()
            with QSignalBlocker(self.identifier), QSignalBlocker(self.output_parent):
                self.identifier.clear()
                self.output_parent.clear()
            self.applied_settings = self.pending_settings = None
            self.editor_settings_json = None
            self.external_mapping_result = None
            self.mapping_dirty = False
            self.mapping_unapplied = False
            self.next_prepare_stage = 1
            self.workflow_tabs.setCurrentIndex(0)
            self.mapping_placeholder.show()
            if self.address_dialog:
                self.detail_toggle.setParent(self)
                self.detail_toggle.hide()
                self.address_dialog.close()
                self.address_dialog.deleteLater()
                self.address_dialog = None
            for field in self.advanced.values():
                field.blockSignals(True)
                field.clear()
                field.blockSignals(False)
        self.stages.setText("입력이 바뀌었습니다. 저장한 주소 규칙은 유지하고 최신 입력을 다시 검사합니다.")
        self.status.setText("입력 변경으로 이전 계획을 무효화했습니다.")
        self.update_source()
        self.update_context()
        self.update_destination()
        self.refresh_primary()
        self.submit(body, self.session.invalidate, body["generation"], body["request_id"])
        self.auto_prepare_pending = True
        self.debounce.start(650)

    def prepare(self):
        self.debounce.stop()
        if self.closing or not self.path.text().strip():
            return
        if any(not future.done() for _, future in self.futures):
            self.debounce.start(250)
            return
        if self.busy:
            if self.auto_prepare_pending:
                self.debounce.start(250)
            return
        self.auto_prepare_pending = False
        body = self.identity()
        body.update(path=self.path.text().strip(), cpu=self.cpu.currentText(), mode=self.mode.currentData())
        body.update(self.request_options())
        body["command"] = "validate" if self.pending_settings is not None and self.mode.currentData() == "convert" else "prepare"
        target_stage = self.next_prepare_stage
        self.next_prepare_stage = None
        if target_stage is None and self.address_dialog is None:
            target_stage = 1
        if target_stage is not None:
            self.arm_navigation(body, target_stage)
        if self.pending_settings is not None:
            self.pending_settings_request = (body["generation"], body["request_id"], self.pending_settings)
            self.validation_revision = self.mapping_revision
        self.command = body["command"]
        self.busy = True
        self.refresh_primary()
        self.status.setText("로컬 엔진 시작 중")
        self.stages.setText("원본과 설정을 분석하고 있습니다. 완료 후 주소 매핑과 검토를 진행하세요.")
        self.submit(body, self.session.prepare, body)

    def cancelled(self):
        self.debounce.stop()
        self.auto_prepare_pending = False
        # A saved rule waiting on the in-flight check must not start after cancel.
        self.rule_check.stop()
        self.queued_saved_rule_check = None
        if hasattr(self.session, "cancel") and self.busy:
            self.submit({"generation": self.generation}, self.session.cancel)
            self.cancel.setEnabled(False)
            return
        body = self.identity()
        self.submit(body, self.session.invalidate, body["generation"], body["request_id"], "취소됨. 원본과 기존 결과는 보존됩니다.")
        self.stages.setText("취소됨. 원본과 기존 결과는 보존됩니다.")
        self.retry.setFocus()

    def cancel_if_busy(self):
        if self.cancel.isEnabled():
            self.cancelled()

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "읽기 전용 원본 선택", "", "GX 원본 (*.gx3 *.gxw)")
        if path:
            self.path.setText(path)
        self.pick.setFocus()

    def poll(self):
        self.runtime.gui_tick()
        try:
            self._poll_ready()
        finally:
            if self.operation_error:
                self.status.setText(self.operation_error)
                self.status.setToolTip(self.operation_error)
            self.flush_queued_rule_check()

    def _poll_ready(self):
        self.poll_folder()
        if self.close_future is not None:
            if self.close_future.done():
                try:
                    self.close_future.result()
                except Exception as error:
                    self.close_future = None
                    self.closing = False
                    self.operation_error = f"종료 정리 실패: {error}. 앱과 소유 작업을 보존했습니다. 종료를 다시 시도하세요."
                    self.centralWidget().setEnabled(True)
                else:
                    self.close()
            return
        remaining = []
        for generation, future in self.futures:
            if future.done():
                try:
                    future.result()
                except Exception as error:
                    if generation == self.generation:
                        self.operation_error = f"실패: {error}. 원본과 설정을 확인하고 다시 시도하세요."
            else:
                remaining.append((generation, future))
        self.futures = remaining
        state = None
        if self.snapshot_future is not None and self.snapshot_future.done():
            try:
                state = self.snapshot_future.result()
            except Exception as error:
                self.operation_error = f"원본 재검사 실패: {error}. 원본을 확인하고 다시 시도하세요."
            self.snapshot_future = None
        if self.snapshot_future is None:
            self.snapshot_future = self.snapshot_executor.submit(self.session.snapshot)
        self.cancel.setEnabled(bool(self.futures) or self.busy)
        self.retry.setEnabled(not self.futures and not self.busy)
        self.refresh_primary()
        if state is None:
            return
        if state["generation"] != self.generation or state["request_id"] != self.request_id or state["version"] < self.version:
            return
        busy = state["busy"] or bool(self.futures)
        self.busy = busy
        self.progress.setVisible(busy)
        self.set_input_enabled(not self.publishing or not busy)
        self.cancel.setEnabled(busy)
        self.retry.setEnabled(not busy)
        result = state["result"]
        if state["version"] == self.version:
            return
        self.version = state["version"]
        self.runtime.progress_record(state)
        cancelled_now = bool(state["events"]) and state["events"][-1]["phase"] == "CANCELLED"
        if state["events"]:
            event = state["events"][-1]
            self.status.setText(event["label"] + (" / " + str(event["error"]["message"]) if event.get("error") else ""))
            if event["phase"] in {"FAILED", "CANCELLED"}:
                self.pending_settings = None
                self.pending_settings_request = None
                self.publishing = False
                self.operation_error = self.status.text()
                if cancelled_now:
                    # Drop a rule saved during the cancelled check. Do not start it afterwards.
                    self.rule_check.stop()
                    self.queued_saved_rule_check = None
        if not result:
            self.result = None
            self.mapping_checked = False
            self.results.hide()
            self.result_details_dialog.hide()
            self.recovery_dialog.hide()
            self.recovery_detail.clear()
            self.readiness.setText("미확인")
            self.problem_summary.setText("현재 계획 미확인")
            self.generation_state.setText("현재 계획 미확인")
            self.refresh_primary()
            return
        if result is not self.result:
            self.result = result
            pending = self.pending_settings_request
            if pending and pending[:2] == (state["generation"], state["request_id"]):
                if (result.get("status") != "HOLD" and self.mode.currentData() == "convert"
                        and result.get("validated_xy_json") == pending[2]):
                    self.applied_settings = pending[2]
                    self.mapping_unapplied = False
                    self.advanced["xy"].blockSignals(True)
                    self.advanced["xy"].clear()
                    self.advanced["xy"].blockSignals(False)
                elif self.address_dialog:
                    # A completed HOLD check does not adopt its draft. Preserve
                    # that independent state across later prepare operations.
                    self.mapping_unapplied = pending[2] != self.applied_settings
                    self.address_dialog.status.setText("전체 검사에서 보류되었습니다. 초안을 보존하고 기존 세션 설정을 유지합니다.")
                self.pending_settings = None
                self.pending_settings_request = None
            self.publishing = False
            self.render_result(result)
            if (self.command == "validate" and self.validation_revision == self.mapping_revision
                    and self.queued_saved_rule_check is None and not cancelled_now):
                self.mapping_checked = True
                self.mapping_dirty = False
            self.finish_navigation(state, result)
            if not self.reported:
                self.runtime.result_record(state)
                if self.prefs:
                    self.prefs.remember(self.path.text().strip())
                self.reported = True
        self.refresh_primary()

    def add_diagnostic_group(self, layout, code, severity, values):
        diagnostic = values[0]
        detail = diagnostic_detail_text(diagnostic.get("detail"))
        if detail:
            layout.addWidget(label(detail, bold=True))
        heading = label(f"{diagnostic_category(code)} / {diagnostic_attention(severity)} / {len(values):,}건", bold=True)
        heading.setToolTip(f"상세 진단 코드: {code}")
        layout.addWidget(heading)
        reason = diagnostic["reason"] if diagnostic["reason"] != code else diagnostic_recovery(code)
        layout.addWidget(label(("대표 사유: " if len(values) > 1 else "") + reason))
        layout.addWidget(label("영향: " + str(diagnostic.get("scope", "프로젝트")) + " / " +
                                str(diagnostic.get("pou") or "전체 범위") + " / " + str(diagnostic.get("operand") or "내용 검토"), "subtle"))
        layout.addWidget(label("다음 행동: " + diagnostic["action"], "subtle"))
        layout.addWidget(label(diagnostic_recovery(code), "subtle"))
        button = QPushButton("위치, 원문과 변환 내용 검토")
        button.clicked.connect(lambda checked=False, ds=values: self.inspect(diagnostics=ds))
        layout.addWidget(button)
        if code.startswith(("XY_", "SETTINGS_", "TEXT_SOURCE_OR_MAPPING")):
            jump = QPushButton("주소 설정으로 이동")
            jump.clicked.connect(lambda checked=False, d=diagnostic: self.edit_settings(diagnostic=d))
            layout.addWidget(jump)

    def render_result(self, result):
        status = {"HOLD": "생성 보류 (HOLD)", "REVIEW": "검토 필요", "CANDIDATE": "시험 후보"}.get(result['status'], "상세 상태 확인")
        direction = "FX5 → Q" if result['direction'] == "FX5_TO_Q" else "Q → FX5"
        self.summary.setText(f"{direction} / {len(result['programs'])}개 POU / {status}")
        readiness = {"BLOCKED": "가져오기 제한 있음", "REVIEW": "수정 및 확인 필요", "CANDIDATE": "가져오기 시험 후보"}.get(result["import_readiness"]["state"], "미확인")
        self.readiness.setText(readiness + " / 공식 GX 미실행")
        generation = result.get("file_generation", "NOT_GENERATED")
        self.generation_state.setText({"GENERATED": "생성 완료", "FAILED": "실패", "CANCELLED": "취소됨"}.get(generation, "미생성"))
        self.stages.setText("결과 생성 완료. GX 확인과 실제 장비 검증은 별도 수행합니다." if generation == "GENERATED"
                            else "보류 문제를 주소·규칙에서 수정하세요." if result.get("status") == "HOLD"
                            else "주소 규칙을 확인한 뒤 변환하세요.")
        self.ensure_mapping_editor(result)
        cpu = result.get("actual_target_cpu") or (self.actual_q_cpu.currentText().strip() if result["direction"] == "FX5_TO_Q" else self.cpu.currentText())
        self.target_summary.setText(f"실제 대상 CPU: {cpu or '미입력'} / 작업 목적: {self.mode.currentText()}")
        statement_counts = result.get("statement_summary", {})
        if statement_counts:
            self.statement_notice.setText(
                f"정책: {self.statement_policy.currentText()} / 원본 {statement_counts['source_types']} / "
                f"대상 {statement_counts['target_types']} / 전환 {statement_counts['type_changed_count']} / "
                f"보류 {statement_counts['held_count']} / NOPLF 설명 {statement_counts['generated_noplf_statement_count']}. "
                "PLC 저장 검증은 미실행입니다.")
            self.target_summary.setText(self.target_summary.text() +
                f"\nStatement: 원본 In PLC {statement_counts['source_types']['i']}, Peripheral {statement_counts['source_types']['s']}; "
                f"대상 In PLC {statement_counts['target_types']['i']}, Peripheral {statement_counts['target_types']['s']}; "
                f"전환 {statement_counts['type_changed_count']}, 보류 {statement_counts['held_count']}.")
        completed_folder = generation == "GENERATED" and bool(result.get("output_dir"))
        partial_folder = generation in {"FAILED", "CANCELLED"} and bool(result.get("preserved_folder") and result.get("remaining_files"))
        self.open_result.setText("저장된 파일 폴더 열기" if partial_folder and result.get("publication_completed")
                                else "미완료 파일 보존 폴더 열기" if partial_folder else "결과 폴더 열기")
        self.open_result.setEnabled(bool(completed_folder or partial_folder) and self.folder_future is None)
        self.guide_direction.setText("FX5 → Q: Q 프로젝트의 Language Korean과 COMMENT.csv 대상 범위를 확인하세요." if result["direction"] == "FX5_TO_Q"
                                     else "Q → FX5: 선택한 실제 FX5 CPU, Device 할당, COMMENT-import 제외 항목을 확인하세요.")
        self.recovery_box.setVisible(generation in {"FAILED", "CANCELLED"})
        if generation in {"FAILED", "CANCELLED"}:
            remaining = result.get("remaining_files", [])
            self.recovery.setText("파일 저장 후 임시 폴더 정리에 실패했습니다. 저장된 파일을 확인하세요."
                                  if result.get("publication_completed")
                                  else "남은 파일 확인이 필요합니다. 복구 상세를 확인하세요." if result.get("recovery_uncertain")
                                  else f"작업이 완료되지 않았습니다. 남은 파일 {len(remaining)}개를 확인하세요.")
            self.recovery_detail.setPlainText("남은 실제 파일: " + ("정리 미확인. 오류에 표시한 임시 폴더를 확인하세요. " + result.get("recovery_message", "") if result.get("recovery_uncertain")
                                                        else ", ".join(result.get("remaining_files", [])) or "발행된 파일 없음") +
                                  "\n보존 폴더: " + result.get("preserved_folder", "") +
                                  "\n정리가 필요한 임시 폴더: " + result.get("cleanup_folder", "없음") +
                                  "\n미생성: " + (", ".join(result.get("not_generated", [])) or "없음") +
                                  "\n다시 생성할 때 기존 결과를 덮어쓰지 않고 새 폴더를 선택합니다.")
        clear(self.issues)
        clear(self.files)
        self.issues.addWidget(label("먼저 검토할 문제", "heading"))
        self.issues.addWidget(label(result_role_summary(result)))
        self.issues.addWidget(label("파일 생성 가능 여부와 프로그램 가져오기 제한은 별도입니다. 코멘트 후보 제외는 프로그램 전체 생성 실패를 뜻하지 않습니다.", "subtle"))
        self.issues.addWidget(label(f"진단 {len(result['diagnostics']):,}건. 같은 코드를 묶어 표시합니다. 전체 위치는 보고서 상세에서 확인하세요.", "subtle"))
        groups = defaultdict(list)
        for diagnostic in result["diagnostics"]:
            groups[(diagnostic["code"], diagnostic["severity"])].append(diagnostic)
        attention_counts = {"해결 필수": 0, "검토": 0, "참고": 0}
        for diagnostic in result["diagnostics"]:
            attention_counts[diagnostic_attention(diagnostic.get("severity"))] += 1
        summary = " / ".join(f"{name} {count}건" for name, count in attention_counts.items() if count)
        if not result.get("publishable_by_engine"):
            summary = "생성할 수 없습니다. " + (summary or "보류 문제를 확인하세요.")
        elif result.get("file_generation") == "GENERATED" and (result.get("import_readiness") or {}).get("state") in {"BLOCKED", "REVIEW"}:
            summary = "파일 생성 완료. GX 가져오기 제한 있음. " + summary
        self.problem_summary.setText(summary or "보고된 진단은 없습니다. GX와 현장 검증은 미실행입니다.")
        reference = []
        for (code, severity), values in groups.items():
            if diagnostic_attention(severity) == "참고":
                reference.append((code, severity, values))
            else:
                self.add_diagnostic_group(self.issues, code, severity, values)
        if reference:
            toggle = QPushButton(f"참고 {attention_counts['참고']}건")
            toggle.setObjectName("link")
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            for code, severity, values in reference:
                self.add_diagnostic_group(box_layout, code, severity, values)
            box.hide()
            toggle.clicked.connect(lambda checked=False, b=box: b.setVisible(not b.isVisible()))
            self.issues.addWidget(toggle)
            self.issues.addWidget(box)
        elif not groups:
            self.issues.addWidget(label("보고된 진단은 없습니다. GX와 현장 검증은 미실행입니다."))
        self.files.addWidget(label("POU별 파일 용도", "heading"))
        original = result.get("comment_original_count")
        if original is not None:
            groups = result.get("comment_import", {}).get("groups", [])
            candidate_files = {}
            for group in groups:
                for candidate in [group["combined_candidate"]] if group.get("combined_candidate") else group.get("candidates", []):
                    candidate_files[candidate["csv"]] = candidate.get("count", 0)
            candidates = sum(candidate_files.values())
            retained = sum(g.get("retained_count", 0) for g in groups)
            self.files.addWidget(label(f"코멘트 원문 {original:,}개 / 가져오기 후보 {candidates:,}개 / 제외 보존 {retained:,}개"))
            self.files.addWidget(label("원문은 보존됩니다. 코멘트 후보와 각 POU의 가져오기 준비 상태를 따로 검토하세요.", "subtle"))
        for program in result["programs"]:
            self.files.addWidget(label(program.get("text_output") or program["name"], bold=True))
            self.files.addWidget(label(program["role_label"]))
            self.files.addWidget(label(program["next_action"], "subtle"))
            button = QPushButton("이 POU의 원문과 변환 내용 열기")
            button.clicked.connect(lambda checked=False, p=program["name"]: self.inspect(pou=p))
            self.files.addWidget(button)
            if program.get("text_output"):
                self.files.addWidget(label(program["text_output"], "subtle"))
        self.report.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        self.results.show()

    def show_result_details(self):
        area = self.screen().availableGeometry()
        self.result_details_dialog.resize(min(900, area.width() - 48), min(640, area.height() - 64))
        self.result_details_dialog.show()
        self.result_details_dialog.raise_()
        self.result_details_dialog.activateWindow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.adapt_display()
        self.update_context()

    def adapt_display(self):
        if getattr(self, "address_dialog", None) is not None:
            self.address_dialog.set_compact(self.height() < 940)
        if not hasattr(self, "context"):
            return
        compact_chrome = self.height() < 620
        if compact_chrome:
            if self.result_details.parentWidget() is not self.result_details_dialog:
                self.result_details_dialog.layout().insertWidget(0, self.result_details, 1)
                self.result_details.show()
        elif self.result_details.parentWidget() is self.result_details_dialog:
            self.result_details_dialog.hide()
            self.result_layout.insertWidget(self.result_details_slot, self.result_details, 1)
            self.result_details.show()
        self.result_details_button.setVisible(compact_chrome)
        short_workspace = self.height() < 1100
        host = self.workflow_tabs.parentWidget().layout()
        if short_workspace:
            host.setContentsMargins(8, 4, 8, 4)
            host.setSpacing(4)
        else:
            host.setContentsMargins(14, 10, 14, 6)
            host.setSpacing(6)
        for widget in self.header_widgets:
            widget.setVisible(not short_workspace)
        self.context.setVisible(not short_workspace)
        for widget in (self.source_name, self.direction):
            widget.setVisible(True)
        for widget in (self.source_icon, self.source_path, self.cpu_note):
            widget.setVisible(not compact_chrome)
        # Long path, mapping-source and statement text folds away on a short
        # window. Progress, failures and blocking reasons stay in the action row.
        show_action_detail = self.height() >= 1100
        for widget in (self.destination_preview, self.mapping_state, self.target_summary):
            widget.setVisible(show_action_detail)
        self.status.setVisible(True)
        self.problem_summary.setVisible(True)
        self.path.setToolTip(self.path.text())
        self.destination_preview.setToolTip(self.destination_preview.text())
        self.generation_notice.setMaximumHeight(72 if self.height() < 1100 else 16777215)
        self.problem_summary.setMaximumHeight(48 if compact_chrome else 16777215)

    def update_context(self):
        if not hasattr(self, "context"):
            return
        path = self.path.text().strip()
        forward = Path(path).suffix.lower() == ".gx3"
        cpu = self.actual_q_cpu.currentText().strip() if forward else self.cpu.currentText()
        text = f"원본: {Path(path).name if path else '미선택'} / {'FX5 → Q' if forward else 'Q → FX5'} / 실제 대상: {cpu or '미입력'}"
        self.context.setToolTip(text)
        self.context.setText(self.context.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(100, self.width() - 40)))

    def closeEvent(self, event):
        self.updates.stop()
        if getattr(self, "rule_check", None) is not None:
            self.rule_check.stop()
        if self.runtime.closed:
            self.timer.stop()
            self.debounce.stop()
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.snapshot_executor.shutdown(wait=True, cancel_futures=True)
            event.accept()
            return
        if self.runtime.framework != "QT_WIDGETS":
            self.closing = True
            self.timer.stop()
            self.debounce.stop()
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.snapshot_executor.shutdown(wait=True, cancel_futures=True)
            self.runtime.close()
            event.accept()
            return
        event.ignore()
        if self.close_future is not None:
            return
        self.closing = True
        self.debounce.stop()
        if self.prefs:
            self.prefs.save_size(self.width(), self.height())
        self.centralWidget().setEnabled(False)
        self.status.setText("소유 작업의 취소와 정리를 마친 뒤 앱을 종료합니다.")
        self.close_future = self.executor.submit(self.runtime.close)


def main():
    args = arguments()
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    from p006_app.branding import APP_NAME
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setFont(QFont("Malgun Gothic", 10))
    app.setStyleSheet(STYLE)
    runtime = Runtime(args, "QT_WIDGETS")
    try:
        window = Workspace(runtime)
        window.show()
        window.updates.start()
        return app.exec()
    finally:
        runtime.close()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()

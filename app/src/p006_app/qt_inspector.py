"""Read-only comparison of engine-provided source and serialized output rows."""
import json
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHeaderView,
                              QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                              QTabWidget, QTextEdit, QVBoxLayout, QWidget)


def display_position(source, target, source_number, direction=None):
    if source is None:
        position = "원본 대응 없음 (추가 출력 행)"
    elif source.get("step") is not None:
        if direction == "Q_TO_FX5":
            position = f"원본 행 인덱스 {source['step']} (0부터 셈)"
        elif direction == "FX5_TO_Q":
            position = f"원본 스텝 {source['step']}"
        else:
            position = f"원본 위치 값 {source['step']} (위치 종류 미확인)"
    else:
        position = f"원본 대조 항목 {source_number} (위치 정보 없음)"
    if target is not None:
        if target.get("sequence") is not None:
            position += f"\n출력 순서 {target['sequence']}"
        if target.get("target_step") is not None:
            position += f" / 출력 스텝 {target['target_step']}"
        else:
            position += " / 출력 스텝 정보 없음"
    return position


def projection_description(source, target):
    if source is None:
        return "출력 구성에 추가된 행입니다. GX에서 검토하세요."
    if source.get("diagnostic_fallback"):
        return "검토용 대체 명령입니다. 원래 기능은 구현되지 않았습니다."
    code = source.get("projection")
    descriptions = {
        "SOURCE_OPCODE": "원본 명령을 유지합니다. 주소 변경은 실제 출력 행에서 확인하세요.",
        "Q_APPROVED_PROJECTION": "승인된 Q 형식의 명령과 피연산자로 변환합니다.",
        "CLOCK_BINARY_7_WORDS": "시계 명령을 대상 형식으로 변환합니다 (7워드).",
        "PRINT_SEPARATOR_TO_PERIPHERAL_STATEMENT": "인쇄 구분을 주석 문장으로 보존합니다.",
    }
    if code in descriptions:
        return descriptions[code]
    return "변환 상세 근거에서 처리 방식과 검토 범위를 확인하세요."


def comparison_rows(programs, pou=None, *, direction=None):
    rows = []
    for program in programs:
        if pou and program.get("name") != pou:
            continue
        sources = {r["source_record_id"]: r for r in program.get("records", []) if r.get("source_record_id")}
        source_numbers = {id(record): number for number, record in enumerate(program.get("records", []), 1)}
        consumed = set()
        # Traverse serialized rows in their authoritative output order. A target
        # layout can add rows without a source ID; never attach them to a source.
        pairs = []
        for target in program.get("serialized_records", []):
            source_id = target.get("source_record_id")
            source = sources.get(source_id) if source_id else None
            if source is not None:
                consumed.add(source_id)
            pairs.append((source, target))
        pairs.extend((source, None) for source in program.get("records", [])
                     if not source.get("source_record_id") or source["source_record_id"] not in consumed)
        for source, target in pairs:
            original = (" ".join([source.get("opcode", ""), *source.get("operands", [])]).strip()
                        or source.get("text", "")) if source is not None else "원문 대응 없음 (출력에서 추가된 행)"
            output = (" ".join([target.get("opcode", ""), *target.get("operands", [])]).strip()
                      or target.get("statement", "") or target.get("note", "")) if target is not None else "출력 행 미제공. 파일에서 확인하세요."
            position = display_position(source, target, source_numbers.get(id(source)), direction)
            rows.append((program.get("name", ""), position, original, output,
                         projection_description(source, target)))
    return rows


class Inspector(QDialog):
    def __init__(self, parent, result, *, pou=None, diagnostics=None):
        super().__init__(parent)
        self.setWindowTitle("원문, 변환 내용과 진단 대조")
        self.resize(920, 680)
        layout = QVBoxLayout(self)
        notice = QLabel("Q 원본의 행 인덱스는 0부터 셉니다. FX5 원본 스텝과 출력 스텝은 각 프로그램의 위치입니다. "
                        "원본 대조 항목은 원장 순서이며 CSV 줄 번호가 아닙니다.\n"
                        "대조 표는 기존 엔진이 기록한 실제 출력 행을 보여줍니다. GX 가져오기와 현장 검증은 별도입니다.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        self.table = QTableWidget(0, 5)
        self.table.setAccessibleName("POU 원문과 실제 변환 출력 대조")
        self.table.setHorizontalHeaderLabels(["프로그램", "원본과 출력 위치", "원문", "실제 출력 행", "변환 설명"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        rows = comparison_rows(result.get("ledger_programs", []), pou, direction=result.get("direction"))
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate(row):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        for column, width in enumerate((100, 220, 150, 230, 180)):
            self.table.setColumnWidth(column, width)
        self.table.resizeRowsToContents()
        tabs.addTab(self.table, "원문과 변환 대조" if rows else "대조 행 없음. 파일에서 확인")
        details = QTextEdit()
        details.setReadOnly(True)
        selected = diagnostics if diagnostics is not None else result.get("diagnostics", [])
        paragraphs = []
        for d in selected:
            paragraphs.append(f"{d.get('code', '')} / {d.get('severity', '')}\n이유: {d.get('reason', '')}\n"
                              f"영향: {d.get('scope', '')} / {d.get('pou') or '전체'} / {d.get('operand') or '내용 검토'}\n"
                              f"위치: {d.get('locator', '미제공')} / CSV record {d.get('csv_record', '미제공')}\n"
                              f"다음 행동: {d.get('action', '')}")
        details.setPlainText("\n\n".join(paragraphs) or "보고된 진단은 없습니다. GX와 현장 검증은 미실행입니다.")
        details.setAccessibleName("진단 이유, 영향, 위치와 다음 행동")
        tabs.addTab(details, "문제와 다음 행동")
        page = QWidget()
        page_layout = QVBoxLayout(page)
        self.choice = QComboBox()
        self.choice.setAccessibleName("검토할 파일과 상세 자료")
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName("읽기 전용 파일 내용")
        self.payloads = []
        self.choice.addItem("원문과 변환의 원시 근거 (식별자, 위치, 처리 코드)")
        self.payloads.append(json.dumps([p for p in result.get("ledger_programs", [])
                                        if pou is None or p.get("name") == pou], ensure_ascii=False, indent=2))
        for name, value in result.get("previews", {}).items():
            if name != "source-target-ledger.json" and (pou is None or name.startswith(pou) or name.endswith(".tsv")):
                self.choice.addItem(name)
                self.payloads.append(value)
        self.choice.addItem("전체 진단과 검토 범위 상세")
        self.payloads.append(json.dumps({k: result.get(k) for k in ("diagnostics", "inspection_scope", "uninspected_scope",
                                      "target_review", "transformations", "comment_import", "preview_omitted")},
                                       ensure_ascii=False, indent=2))
        page_layout.addWidget(self.choice)
        page_layout.addWidget(self.text)
        tabs.addTab(page, "파일 미리보기와 상세")
        if diagnostics:
            tabs.setCurrentIndex(1)
        close = QPushButton("검토를 마치고 돌아가기")
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        layout.addWidget(close)
        self.choice.currentIndexChanged.connect(self.show_content)
        self.show_content(0)

    def show_content(self, index):
        if 0 <= index < len(self.payloads):
            self.text.setPlainText(self.payloads[index])

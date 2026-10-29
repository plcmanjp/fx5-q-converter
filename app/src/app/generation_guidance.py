"""Operator guidance without changing engine publication or CPU authority."""

# Existing product dialect, fixed bounds and native evidence cover Q06UDV.
Q_CPUS = ("Q06UDVCPU",)
DEFAULT_Q_CPU = Q_CPUS[0]
# Publish accepts only this marker from an explicit convert click.
EXPLICIT_EXECUTION_REQUEST = "explicit_convert"


def generation_guidance(result, *, idle, dirty, q_cpu, source_selected, pending_input=False):
    if result and result.get("file_generation") == "GENERATED":
        return "파일 생성이 완료되었습니다. '결과 폴더 열기'에서 실제 저장 폴더로 이동하세요. 파일 생성과 GX 가져오기 제한은 별도이며, 현장 검증도 별도입니다."
    if not source_selected:
        return "파일 등록에서 .gx3 또는 .gxw 원본을 선택하세요."
    if not idle:
        return "최신 입력을 검사 중입니다. 주소 규칙 화면과 편집 위치는 유지합니다."
    if result and result.get("direction") == "FX5_TO_Q" and q_cpu not in Q_CPUS:
        return "대상 Q CPU 저장값이 선택 범위에 없습니다. Q06UDVCPU를 선택하세요. 기본 선택은 실제 장비 검증이 아닙니다."
    if result and result.get("output_destination_validation") == "INVALID":
        return "결과 저장 경로를 사용할 수 없습니다. '위치 선택'에서 존재하는 일반 폴더를 선택하세요. 실제 오류와 경로는 보고서 상세에 보존됩니다."
    if pending_input or dirty:
        return "입력 중인 주소 규칙을 저장하거나 취소하세요. 저장되지 않은 규칙으로는 변환하지 않습니다."
    if not result:
        return "원본과 설정을 확인하는 중입니다."
    if not result.get("publishable_by_engine"):
        codes = [str(d.get("code", "")) for d in result.get("diagnostics", [])]
        if any("OUTPUT" in code or "DESTINATION" in code for code in codes):
            return "결과 저장 경로 문제로 생성할 수 없습니다. 저장 위치를 바꾼 뒤 자동 검사를 기다리세요."
        if any(code.startswith(("XY_", "SETTINGS_", "TEXT_SOURCE_OR_MAPPING")) for code in codes):
            return "주소 설정의 보류 문제가 있어 생성할 수 없습니다. 주소 규칙에서 해당 위치를 수정하세요."
        return "보류 문제가 있어 생성할 수 없습니다. 해결할 문제에서 원문과 위치를 확인하세요."
    programs = result.get("programs") or []
    readiness = (result.get("import_readiness") or {}).get("state")
    if any(p.get("role") != "TEXT_CANDIDATE" for p in programs) or readiness in {"BLOCKED", "REVIEW"}:
        return "검토용 파일을 생성할 수 있습니다. GX 가져오기 제한과 현장 검증은 별도입니다."
    return "현재 입력으로 변환할 수 있습니다. GX와 현장 검증은 별도입니다."


def diagnostic_attention(severity):
    """Separate blocking, review and reference diagnostics without using one label."""
    value = str(severity).lower()
    if value == "error":
        return "해결 필수"
    if value in {"info", "information", "note"}:
        return "참고"
    return "검토"


def result_role_summary(result):
    programs = result.get("programs", [])
    candidates = sum(p.get("role") == "TEXT_CANDIDATE" for p in programs)
    review = sum(p.get("role") == "IMPORT_REVIEW_CSV" for p in programs)
    other = len(programs) - candidates - review
    return f"프로그램 출력: 가져오기 시험 후보 {candidates}개 / 수정 검토본 {review}개 / 기타 검토 자료 {other}개"


def diagnostic_recovery(code):
    if code == "TEXT_SOURCE_COMMENT_UNDECODED":
        return "원본 코멘트의 미해독 주소와 저장 형식을 상세에서 확인하세요. 주소 매핑 변경으로 해결되는 차단이 아닙니다."
    if "UNMAPPED" in code or "UNASSIGNED" in code:
        return "주소 규칙에서 원본 구간의 대상 주소를 저장하세요. 저장하면 다시 검사합니다."
    if code.startswith(("XY_", "SETTINGS_", "TEXT_SOURCE_OR_MAPPING")):
        return "주소 규칙 또는 상세 설정의 JSON에서 해당 위치를 수정하세요. 저장하면 다시 검사합니다."
    if "TARGET" in code or "Q_IMPORT" in code:
        return "대상 Q 기능과 프로그램별 출력 용도를 검토하세요. 미지원 기능의 수정은 별도 엔지니어링이 필요합니다."
    if "SOURCE" in code or "MINING" in code:
        return "원본 위치와 상세 원문을 확인하세요. 원본 재분석 또는 지원 구조 확인이 필요합니다."
    return "결과의 위치와 원문 상세에서 해당 사유를 확인하세요."


def diagnostic_category(code):
    if code == "TEXT_SOURCE_COMMENT_UNDECODED":
        return "원본 코멘트 해독 확인"
    if "UNMAPPED" in code or "UNASSIGNED" in code:
        return "주소 미배정 확인"
    if code.startswith(("XY_", "SETTINGS_", "TEXT_SOURCE_OR_MAPPING")):
        return "주소 설정 확인"
    if "COMMENT" in code:
        return "코멘트 후보와 제외 보존 확인"
    if "TARGET" in code or "Q_IMPORT" in code:
        return "대상 기능과 가져오기 제한 확인"
    if "SOURCE" in code or "MINING" in code:
        return "원본 구조와 위치 확인"
    return "변환 검토 사항"

"""Numeric, unique-device range selection independent of screen row numbers."""
from dataclasses import dataclass


def interval_key(row, radix):
    start, end = row["source_start"].upper(), row["source_end"].upper()
    if start[0] not in "XY" or start[0] != end[0]:
        raise ValueError("X와 Y가 섞인 구간은 선택할 수 없습니다.")
    first, last = int(start[1:], radix), int(end[1:], radix)
    if last < first:
        raise ValueError("원본 구간의 끝 주소가 시작보다 작습니다.")
    return start[0], first, last


@dataclass(frozen=True)
class SourceSelection:
    family: str
    first: int
    last: int
    points: int

    def spelling(self, number, radix):
        return self.family + format(number, "X" if radix == 16 else "o")


def select_source_range(rows, anchor, endpoint, radix):
    """Use all authoritative intervals, retaining anchor across page/order changes."""
    keys = [interval_key(row, radix) for row in rows]
    if anchor not in keys or endpoint not in keys:
        raise ValueError("주소 목록이 바뀌었습니다. 원본 구간을 일반 클릭하여 다시 선택하세요.")
    if anchor[0] != endpoint[0]:
        raise ValueError("X와 Y를 하나의 연속 규칙으로 선택할 수 없습니다. 기존 입력을 보존합니다.")
    family = anchor[0]
    first, last = min(anchor[1], endpoint[1]), max(anchor[2], endpoint[2])
    intervals = sorted((max(a, first), min(b, last)) for f, a, b in keys
                       if f == family and a <= last and b >= first)
    cursor = first
    for start, end in intervals:
        if start > cursor:
            raise ValueError("선택 구간에 실제 미사용 주소 공백이 있습니다. 기존 입력을 보존합니다.")
        cursor = max(cursor, end + 1)
    if cursor <= last:
        raise ValueError("선택 구간에 실제 주소 공백이 있습니다. 기존 입력을 보존합니다.")
    return SourceSelection(family, first, last, last - first + 1)

# GXW 보조 관측 패키지

이 디렉터리는 별도 프로세스의 읽기 전용 보조 보고서에 쓰는 정확한 wheel과 라이선스, 스키마, 출처를 담습니다. `provenance.json`의 wheel과 모듈 해시는 `converter.gxw_diagnostics`의 고정 값으로 다시 검사합니다. 원래 변환 reader와 pin은 바꾸지 않습니다.

보고서는 원문을 가리고 항상 `PARTIAL` 또는 `NOT_ANALYZED`입니다. `Q06UDV` 또는 `Q06UDVCPU` 표식이 관측되지 않거나 다른 CPU 표식이 함께 있으면 분석하지 않습니다. 표식은 CPU 인증이 아닙니다. 주소가 보고서에 없다고 미사용으로 판정하지 않으며, 코멘트 소유자와 Note subtype을 추정하지 않습니다. 변환, 대상 GX 수락과 PLC 운전 권한은 주지 않습니다.

wheel은 106의 원래 builder가 만든 새 artifact입니다. 과거 보고서의 wheel 해시를 재사용하지 않습니다. `source_commit`과 11개 모듈 바이트가 같다는 결속과 candidate의 독립 Git commit은 출처 파일에 있습니다. 일반 parser 수정은 106에서 소유하며, 이 패키지를 107에서 수정하거나 독립 fork로 유지하지 않습니다.

롤백은 보조 보고서 호출과 이 소비 패키지만 제거합니다. 기존 reader, 변환 결과와 고정 검증 자료는 그대로 유지합니다.

Windows에서는 새 worker를 일시 중단 상태로 만든 뒤 별도 Job에 결속하고 실행합니다. 시간 초과 또는 Job 종료 시 이 worker와 그 자식만 종료합니다. 프로세스 트리의 총 committed memory 상한은 512 MiB입니다. 파일/응답/행 수 한도와 함께 쓰지만, 전체 OS 보안 sandbox나 네트워크 차단을 제공한다는 뜻은 아닙니다. 기존에 실행 중인 다른 앱과 프로세스는 이 Job에 넣지 않습니다.

원문 라이선스는 `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`에 있습니다. Apache-2.0 소스와 서비스 entry point를 포함하지만 제품 어댑터는 snapshot API만 사용합니다. 어댑터가 실제로 소비하는 외부 의존성은 기존 고정 `olefile` 0.47이며, Windows 파일 snapshot은 ctypes와 msvcrt를 사용합니다. wheel METADATA의 pywin32 선언은 upstream 배포 계약으로 보존합니다.

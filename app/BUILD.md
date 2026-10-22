# 실행과 빌드

Windows x64와 Python 3.12.13을 사용합니다. 새 checkout과 새 가상환경에서 다음 명령을 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r app/packaging/requirements-qt.txt
$env:PYTHONPATH = (Join-Path (Get-Location) 'app/src')
.venv\Scripts\python.exe -B -m p006_app.qt_app
```

빌드는 Git에 커밋된 깨끗한 `app/` 소스를 요구합니다. 기존 `app/dist`가 있으면 덮어쓰지 않고 중단하므로 새 checkout에서 실행합니다.

```powershell
.venv\Scripts\python.exe -B app/packaging/build-qt.py --label public
```

`app/dist/Q-FX-Converter-v버전.zip`과 파일별 해시가 있는 `package.json`을 생성합니다. 압축파일 전체를 배포합니다. 고정 parser wheel과 규칙 데이터는 이미 포함되어 있으며 고객 원본이나 비공개 GX 검증 프로젝트는 필요하지 않습니다.

## Qt/PySide6 교체

이 앱은 Qt/PySide6 DLL을 동적으로 로드하는 onedir 방식입니다. `_internal/PySide6`과 `_internal/shiboken6`의 라이브러리를 ABI가 호환되는 수정 빌드로 교체할 수 있으며, 수정된 LGPL 라이브러리를 사용하기 위한 역공학과 재링크를 제한하지 않습니다. 원본 배포 폴더를 보존하고 별도 사본에서 교체하세요.

직접 재빌드하려면 Release의 Qt 및 PySide 소스 압축파일과 각 소스의 빌드 안내를 사용합니다. 사용자 수정판은 `app/packaging/requirements-qt.txt`와 `build-qt.py`의 버전 검사를 해당 빌드에 맞게 수정하고 커밋한 뒤 빌드할 수 있습니다. 검사는 공식 배포 재현성을 위한 것이며 사용자 수정판 실행을 잠그는 서명 검사는 없습니다.

## 업데이트 배포

버전은 `app/VERSION`을 따릅니다. Release tag는 `vMAJOR.MINOR.PATCH`, Windows asset은 `Q-FX-Converter-vMAJOR.MINOR.PATCH.zip`이어야 합니다. 정식 Release로 게시되고 ZIP 업로드가 끝나야 업데이트 확인 대상이 됩니다. 같은 버전과 이전 버전은 업데이트로 알리지 않습니다.

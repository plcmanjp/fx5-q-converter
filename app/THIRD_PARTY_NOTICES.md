# 제3자 고지와 소스 제공

MIT는 자체 작성 앱 코드와 문서에 적용합니다. 아래 구성요소의 원래 라이선스와 고지를 변경하지 않습니다.

| 구성요소 | 적용 조건과 위치 |
|---|---|
| GX RE Toolkit의 Q/FX5 reader | Apache-2.0. 포함된 wheel의 LICENSE, NOTICE와 `app/src/converter/resources/q-reader/`의 고지를 유지합니다. |
| Qt 6.11.2의 Core/Gui/Widgets 및 함께 배포된 LGPL 모듈 | LGPL-3.0. 라이선스 원문은 `app/packaging/third-party/qtbase/`, 바이너리 배포는 `THIRD-PARTY-LICENSES/`에 있습니다. |
| PySide6-Essentials 및 shiboken6 6.11.2 | LGPL-3.0과 포함된 제3자 고지. `app/packaging/third-party/pyside/` 및 설치 배포판의 라이선스를 동봉합니다. |
| Python 3.12.13 | PSF와 포함 구성요소의 고지. 바이너리의 `THIRD-PARTY-LICENSES/python-LICENSE.txt`를 확인하세요. |
| olefile 0.47 | BSD 계열. 설치 배포판의 라이선스를 동봉합니다. |
| PyInstaller | GPL과 bootloader 배포 예외. 생성된 앱에 GPL을 강제하지 않는 예외가 있으며 고지를 동봉합니다. |

Qt 내부의 압축, 이미지, 글꼴 등 제3자 구성요소는 Qt 소스에 포함된 해당 라이선스를 유지합니다. 상용 라이선스 원문이 자료에 포함돼 있어도 상용 라이선스를 선택했다는 뜻은 아닙니다. 이 배포는 LGPL 옵션을 사용합니다.

앱 Release 본문의 지원 자료 링크에서 동봉된 Qt/PySide에 대응하는 소스 압축파일을 무료로 제공합니다. [2.2.65 지원 자료](https://github.com/plcmanjp/fx5-q-converter/releases/tag/support-v2.2.65)에는 Qt 6.11.2와 PySide6/shiboken6 6.11.2의 변경하지 않은 upstream 소스를 보존합니다. 취득 URL과 SHA-256은 같은 위치의 third-party-sources.json에 기록하며 Qt DLL 구성에 맞는 소스 모듈을 포함합니다. 실행 ZIP은 앱 Release에서 받습니다. [빌드 및 교체 안내](BUILD.md)를 통해 수정된 라이브러리를 재빌드하고 교체할 수 있습니다.

공식 안내: [MIT](https://opensource.org/license/mit), [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0), [Qt LGPL 의무](https://www.qt.io/development/open-source-lgpl-obligations), [Qt for Python 라이선스](https://doc.qt.io/qtforpython-6/licenses.html), [PyInstaller 라이선스](https://pyinstaller.org/en/stable/license.html).

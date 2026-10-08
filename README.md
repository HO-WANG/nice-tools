# nice-tools

내부망에서 사용하는 작은 웹 도구 모음입니다. Python + FastAPI 서버와
외부 CDN 없이 동작하는 HTML·CSS·JavaScript 화면으로 구성했습니다.
접을 수 있는 왼쪽 메뉴에서 도구를 선택합니다.

첫 도구는 **디렉토리 텍스트 비교**입니다. 사용자 PC의 폴더 두 개를
브라우저에서 선택하고, 같은 하위 상대 경로의 파일을 비교합니다.

## 시작하기

Python 3.12 이상이 필요합니다. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn nice_tools.main:app --reload --host 127.0.0.1 --port 8000
```

실행한 PC의 브라우저에서 `http://127.0.0.1:8000` 주소를 엽니다.
폴더 선택 기능을 지원하는 Chrome 또는 Edge 사용을 권장합니다.

Windows에서는 가상 환경의 Python 경로를 다음처럼 바꿉니다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn nice_tools.main:app --reload --host 127.0.0.1 --port 8000
```

## 디렉토리 비교

1. 왼쪽 폴더와 오른쪽 폴더를 선택합니다.
2. 필요한 비교 옵션을 설정합니다.
3. **비교 시작**을 누릅니다.
4. 요약과 파일별 결과를 확인하고, 상태 필터로 필요한 파일만 봅니다.

예를 들어 `before/sub/a.txt`와 `after/sub/a.txt`는 서로 비교합니다.
최상위 폴더 이름은 달라도 됩니다. 결과 상태는 동일, 차이, 왼쪽만,
오른쪽만, 읽기 오류입니다. 하위 디렉토리를 포함하며 확장자로 파일을 제외하지 않습니다.

| 옵션 | 동작 |
| --- | --- |
| 줄 정렬 | 변환한 줄들을 정렬한 뒤 비교합니다. 중복 줄은 유지합니다. |
| 공백 무시 | 각 줄 안의 스페이스, 탭과 기타 Unicode 공백을 제거합니다. 빈 줄은 유지합니다. |
| 앞에서 무시할 글자 수 | 각 줄의 처음에서 지정한 수만큼 제외합니다. |
| 뒤에서 무시할 글자 수 | 각 줄의 끝에서 지정한 수만큼 제외합니다. |

처리 순서는 **앞·뒤 글자 제외 → 공백 제거 → 줄 정렬 → 비교**입니다.
글자 수는 Unicode 코드 포인트 기준이며, 한글과 이모지도 바이트로 자르지 않습니다.
앞·뒤 제외 범위가 겹치면 해당 줄은 빈 문자열이 됩니다.

UTF-8과 UTF-8 BOM을 지원합니다. CRLF, LF, CR은 동일한 줄바꿈으로 처리하며
마지막 줄 끝의 줄바꿈 유무는 무시합니다. 중간의 빈 줄은 비교 대상입니다.
UTF-8로 해석할 수 없거나 NUL 문자가 포함된 파일은 읽기 오류로 표시합니다.
한쪽에만 있는 파일은 내용 해석 없이 존재 상태를 표시합니다.

양쪽 합계 **1,000개 파일**, **개별 파일 10 MiB**, **전체 업로드 요청 50 MiB**까지
처리합니다. 전체 요청 크기에는 multipart 경로·메타데이터도 포함됩니다.
브라우저가 제공하지 않는 빈 폴더와 빈 디렉토리 구조는 비교할 수 없습니다.

선택한 파일은 비교를 위해 서버로 전송합니다. 비교 과정에서 메모리를 사용하며,
프레임워크가 만든 임시 업로드 파일은 요청 완료 후 정리합니다.
파일과 비교 기록을 영구 저장하지 않습니다.

## 테스트와 실행 확인

```bash
.venv/bin/python -m pytest -q
```

서버를 실행한 상태에서 실제 HTTP 요청을 확인합니다.

```bash
.venv/bin/python scripts/smoke.py
```

Chromium 또는 Chrome이 설치되어 있으면 실제 폴더 선택과 화면 동작도 확인할 수 있습니다.
개발 의존성의 Playwright는 실행 중인 서버와 설치된 브라우저를 사용합니다.

```bash
.venv/bin/python tests/browser_smoke.py --chromium /usr/bin/chromium
```

다른 서버/포트에는 `--url`을 사용합니다. Chrome 실행 파일이 다른 위치에 있으면
`--chromium`에 해당 경로를 지정합니다. 화면 캡처는 선택적으로 `--screenshot 경로.png`를 지정합니다.

## 내부망 서버에서 실행

런타임 의존성만 설치하고 서버가 접근할 내부망 주소에 바인딩할 수 있습니다.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn nice_tools.main:app --host 0.0.0.0 --port 8000
```

서비스 상태 확인 경로는 `GET /health`입니다.
운영 시 사내 서비스 관리자나 리버스 프록시에 실행 명령을 등록합니다.
첫 버전은 별도 로그인 없이 사용하는 내부 도구입니다.

인터넷이 차단된 서버에는, 같은 OS·CPU·Python 버전의 인터넷 가능 PC에서
의존성 파일을 준비해 옮길 수 있습니다.

```bash
python3 -m pip download -r requirements.txt --dest wheelhouse
# requirements.txt와 wheelhouse를 내부망 서버로 옮긴 뒤:
.venv/bin/python -m pip install --no-index --find-links wheelhouse -r requirements.txt
```

## 구조와 도구 추가

- `nice_tools/comparison.py`: 텍스트 정규화와 디렉토리 비교
- `nice_tools/uploads.py`: 업로드·경로·옵션 검증
- `nice_tools/limits.py`: 본문 수신 중 요청 크기 제한
- `nice_tools/main.py`: 정적 화면, 상태 확인, 비교 API
- `nice_tools/static/`: 메뉴와 도구 화면
- `tests/`: 비교·API·화면 테스트

새 도구를 추가할 때는 메뉴와 도구 화면을 연결하고, 필요한 Python 로직을
별도 모듈과 API로 추가합니다. 비교 로직은 HTTP에 의존하지 않습니다.
의존성 버전은 런타임과 개발 요구 파일에 전이 의존성까지 고정했습니다.

클라우드 작업은 기존 `/workspace/nice-tools` 체크아웃을 사용합니다.
각 작업이 이미 격리되어 있으므로 별도 Git worktree를 만들 필요가 없습니다.

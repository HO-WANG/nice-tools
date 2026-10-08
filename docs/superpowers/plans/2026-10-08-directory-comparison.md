# Directory Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 사용자 PC의 폴더 두 개를 비교하는 첫 도구와 접이식 메뉴를 제공한다.

**Architecture:** FastAPI가 정적 HTML 화면과 multipart 비교 API를 제공한다. Python 비교 함수는 HTTP와 분리하고, JavaScript는 폴더 선택·옵션 전송·결과 표시를 담당한다.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, python-multipart, pytest, httpx, HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-08-nice-tools-design.md`

## Global Constraints

- Python 3.12 이상, 기본 HTML/CSS/JavaScript, 외부 CDN 및 프런트엔드 빌드 없음.
- UTF-8/BOM 허용, CRLF/LF/CR 동일 처리, 마지막 줄 끝 줄바꿈 무시, 중간 빈 줄 유지.
- 앞/뒤 글자 제외 → Unicode 공백 제거 → 줄 정렬 → 비교. 중복 줄 보존.
- 양쪽 합계 1,000개 파일, 요청 전체 50 MiB, 개별 파일 10 MiB.
- 한국어 UI, 상대 경로 짝짓기, 접근 가능한 메뉴와 진행/오류 알림.
- 프로젝트에 업로드 파일 영구 저장 없음, 임시 업로드 자원은 요청 뒤 닫기.
- 기존 체크아웃 사용, Git worktree 생성 없음. 버전 고정 의존성·실행 문서·클라우드 설정 저장.

## Review Focus

- 이름이 다른 최상위 폴더와 같은 하위 파일: 최상위 이름을 뺀 경로로 짝짓기.
- 이모지/한글과 겹치는 앞/뒤 제외 범위: 코드 포인트로 자르고 범위가 겹치면 빈 줄.
- 중복·절대·이탈 경로 또는 잘못된 UTF-8: 경로는 요청 오류, 인코딩은 파일별 오류.
- Content-Length가 없는 과대 업로드: 본문 수신 중에도 50 MiB 제한을 지키고 다음 요청 처리 가능.
- 결과에 HTML처럼 보이는 파일명과 재실행: 텍스트로 표시하고 새 결과로 이전 결과 대체.

---

### Task 1: 독립 비교 로직과 테스트 환경

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`.
- Create: `nice_tools/__init__.py`, `nice_tools/comparison.py`.
- Test: `tests/test_comparison.py`.

**Interfaces:**
- Produces: `CompareOptions(sort_lines: bool = False, ignore_whitespace: bool = False, ignore_start: int = 0, ignore_end: int = 0)`.
- Produces: `normalize_lines(content: bytes, options: CompareOptions) -> list[str]`.
- Produces: `compare_directories(left: dict[str, bytes], right: dict[str, bytes], options: CompareOptions) -> dict`.
- Result: `summary` with `total`, `same`, `different`, `left_only`, `right_only`, `error`; `results` with sorted `path`, `status`, optional `message`.

- [x] **Step 1: Write failing comparison tests.**
  `test_summary_and_path_pairing`: 같은 하위 상대 경로끼리 대응; `same=1`, `different=1`, `left_only=1`, `right_only=1`, `total=4`.
  Parametrize normalization for BOM/newlines, empty files, internal empty lines, sorting with duplicates, Unicode whitespace, prefix/suffix removal, combined options.
  `test_unicode_and_overlapping_slices`: `"😀가나다"` with start 1/end 1 gives `"가나"`; start 3/end 3 gives `""`.
  `test_invalid_text_reports_per_file_error`: invalid UTF-8 and NUL are `error`, other pairs still compare.
- [x] **Step 2: Create `.venv`, pin/install dependencies and run `.venv/bin/python -m pytest tests/test_comparison.py -q`.** Expect missing comparison module (red).
- [x] **Step 3: Implement interfaces in `nice_tools/comparison.py`.** Use immutable validated options, explicit CR/LF normalization, Python string slices, `str.isspace`, sorted lists preserving duplicates, sorted path union.
- [x] **Step 4: Run `.venv/bin/python -m pytest tests/test_comparison.py -q`.** Expect all collected cases pass.
- [x] **Step 5: Commit comparison core and dependency configuration.**

### Task 2: 실제 업로드 API와 수신 제한

**Files:**
- Create: `nice_tools/main.py`, `nice_tools/uploads.py`, `nice_tools/limits.py`.
- Test: `tests/test_api.py`.

**Interfaces:**
- Consumes: Task 1 `CompareOptions`, `compare_directories`.
- Produces: ASGI `app` at `nice_tools.main:app`.
- `GET /health` returns `{"status":"ok"}`.
- `POST /api/compare`: multipart fields `left_files`, `right_files` (repeated file parts), `left_paths`, `right_paths` (JSON arrays), `sort_lines`, `ignore_whitespace`, `ignore_start`, `ignore_end`.
- API returns Task 1 result JSON; malformed input is 400; body/file size excess is 413.
- `read_uploads(form) -> tuple[dict[str, bytes], dict[str, bytes], CompareOptions]` validates uploaded data and paths; the endpoint uses `async with request.form(...)` to close files on success and failure.
- `BodyLimitMiddleware(app, max_bytes: int = 50 * 1024 * 1024)` bounds declared and actually received body sizes.

- [x] **Step 1: Write failing API tests with `TestClient`.**
  Assert a real two-sided multipart request produces expected summary/statuses; invalid paths (`../a`, `/a`, duplicate, Windows drive), mismatched path counts, negative/fractional options return 400.
  Assert 1,001 files, >10 MiB file, >50 MiB declared body fail; oversized streaming body without Content-Length fails 413 followed by healthy request.
  Assert upload handles close after success/error and invalid text is a per-file result.
- [x] **Step 2: Run `.venv/bin/python -m pytest tests/test_api.py -q`.** Expect missing HTTP app (red).
- [x] **Step 3: Implement multipart validation, bounded receive middleware and app.** Decode path arrays strictly, reject unsafe/duplicate paths, check total file count and per-file size, parse checkbox booleans and nonnegative integers explicitly, execute CPU comparison outside async event loop.
- [x] **Step 4: Run `.venv/bin/python -m pytest tests/test_comparison.py tests/test_api.py -q`.** Expect full pass, no skipped cases.
- [x] **Step 5: Commit API and limits.**

### Task 3: 화면, 실제 사용 검증과 재사용 설정

**Files:**
- Create: `nice_tools/static/index.html`, `nice_tools/static/styles.css`, `nice_tools/static/app.js`.
- Modify: `nice_tools/main.py` to serve `/` and `/static` from absolute package path.
- Create: `README.md`, `scripts/smoke.py`.
- Test: `tests/test_frontend.py`, `tests/browser_smoke.py`.

**Interfaces:**
- Consumes: Task 2 multipart API and Task 1 result field names/statuses.
- Produces: Korean HTML UI at `/`, native directory pickers, options, accessible collapsible sidebar, summary/status-filtered results.
- Produces: `.venv/bin/python scripts/smoke.py` verifying health, static assets and representative multipart response of a running server at default loopback port 8000.

- [x] **Step 1: Add failing static/functional tests.** `GET /` contains the tool and controls; static script/style are available. Browser smoke selects two fixture folders with different root names, asserts summary, changes all four options and rechecks results; toggles sidebar; retries after an error; displays a filename containing HTML syntax as plain text.
- [x] **Step 2: Run frontend HTTP tests and browser smoke.** Expect absent screen/assets or controls (red).
- [x] **Step 3: Implement HTML/CSS/JS and static routes.** Strip one root component from webkit paths; client bounds checks match server; multipart payload encodes exact fields; render via textContent; prevent duplicate submissions; reset stale results on changes; handle failures and retry. Avoid third-party UI dependencies.
- [x] **Step 4: Run all pytest tests; run live Uvicorn + smoke and browser tests.** Expect real comparisons and sidebar interactions pass.
- [x] **Step 5: Document setup/run/test, encoding, options and upload limits; pin transitive dependencies; validate install repeatability.**
- [ ] **Step 6: Save tested `install_script` and `start_skill` to cloud draft, with work directory, service restart/readiness and existing-checkout guidance.**
- [ ] **Step 7: Review complete changes, commit on `main`, check/push the new remote branch without force, verify remote SHA and save repository metadata when required.**

## Execution

현재 세션에서 직접 구현한다. 요구사항은 하나의 화면/API/비교 로직으로 연결되어 있으므로
순서대로 구현하고 각 단계에서 필요한 테스트를 실행한다.
사용자에게 구현과 검증 결과, 원격 반영 여부, 저장한 설정과 게시 단계를 보고한다.

"""Validate the integrated ASE screen with a native-file/API mock in Chromium.

Run the development server first, then use:
    .venv/bin/python scripts/browser_ase.py --url http://127.0.0.1:8000

The mock exercises the browser contract without an ASE server, Windows, or bcp.
It deliberately uses opaque native-picker tokens and hostile metadata text.
"""

import argparse
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


SESSION = "session-1"
PASSWORD = "browser-only-password-7!"
FIXED_PROFILE_KEY = "nice1234!!"
PROFILE_PATH = r"C:\nice-tools\ase-connections.txt"
CLIENT_PATHS = {"bcp_path": r"C:\SAP\OCS-16_0\bin\bcp.exe"}
ODBC = {"driver": "Adaptive Server Enterprise", "drivers": ["Adaptive Server Enterprise"]}
PROFILES = [
    {"id": "main", "name": "운영 ASE", "host": "ase.saved.internal", "port": 5001, "user": "saved_user", "database": "db1"},
    {"id": "hostile", "name": '<img src=x onerror="window.__aseXss=true">', "host": "ase.backup.internal", "port": 5002, "user": "backup_user", "database": "missing_db"},
]
JASYPT = {"algorithm": "PBEWithMD5AndDES", "iterations": 1000, "salt_generator": "RandomSaltGenerator", "iv_generator": "NoIvGenerator", "output_type": "base64"}
FILE_PATH = r"C:\files\sample.txt"
HOSTILE = '<img src=x onerror="window.__aseXss=true">'
HOSTILE_ERROR = '<script>window.__aseXss=true</script> request failed'
HOSTILE_SAMPLE = '<svg onload="window.__aseXss=true">sample</svg>'
TABLE = {"database": "db1", "owner": "dbo", "owner_id": 1, "id": 10, "name": "customer_rows", "kind": "U"}
DUPLICATE_TABLE = {**TABLE, "owner": "analytics", "owner_id": 2, "id": 14}
HOSTILE_TABLE = {**TABLE, "id": 11, "name": HOSTILE}
VIEW = {**TABLE, "id": 12, "name": "customer_view", "kind": "V"}
PROCEDURE = {**TABLE, "id": 13, "name": "customer_proc", "kind": "P"}
COLUMNS = [
    {"position": 1, "name": "id", "type_id": 56, "type_name": "int", "length": 4, "precision": 10, "scale": 0, "nullable": False, "identity": True},
    {"position": 2, "name": HOSTILE, "type_id": 39, "type_name": "varchar", "length": 255, "precision": 0, "scale": 0, "nullable": True, "identity": False},
]


@dataclass
class ASEMock:
    """Keep strict request assertions at the boundary, not in the UI mock."""

    calls: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    picker_number: int = 0
    preview_number: int = 0
    job_number: int = 0
    status_polls: dict = field(default_factory=dict)
    hold_job: bool = False
    cancel_picker: bool = False
    fail_columns: bool = False
    delay_owners: bool = False
    delay_preview: bool = False
    pending_owners: list = field(default_factory=list)
    delay_objects: bool = False
    pending_objects: list = field(default_factory=list)
    pending_previews: list = field(default_factory=list)
    reviews: dict = field(default_factory=dict)
    profiles: list = field(default_factory=list)
    profiles_exist: bool = False
    fail_profiles: bool = False
    delay_profiles: bool = False
    delay_connect: bool = False
    delay_start: bool = False
    runtime_available: bool = True
    bcp_available: bool = True
    pending_profiles: list = field(default_factory=list)
    pending_connects: list = field(default_factory=list)
    pending_starts: list = field(default_factory=list)
    allowed_sessions: set = field(default_factory=lambda: {SESSION})
    job_directions: dict = field(default_factory=dict)
    completed_rows: int = 1_234_567
    completed_statistics: dict | None = field(default_factory=lambda: {"elapsed_seconds": 1.25, "rows_per_second": 987_653.6, "bytes_written": 5_242_880})
    completed_bcp_statistics: dict | None = None
    terminal_state: str = "succeeded"
    owners_override: list | None = None
    timings: dict | None = field(default_factory=lambda: {"login_seconds": 0.1, "query_seconds": 0.5, "total_seconds": 0.75})
    last_profile_id: str = ""
    auto_save: bool = False
    fail_save: bool = False
    fail_connect: bool = False
    replace_profile_after_save: bool | str = False
    legacy_profile: bool = False
    data_sources: list | None = None
    data_sources_detail: str = ""
    expected_dsn: str = ""

    def timed(self, payload):
        return {**payload, **({"timings": self.timings} if self.timings is not None else {})}

    def profile_response(self):
        return {"path": PROFILE_PATH, "exists": self.profiles_exist, "profiles": self.profiles, "jasypt": JASYPT, "clients": CLIENT_PATHS, "odbc": ODBC, "last_profile_id": self.last_profile_id}

    def respond(self, route, payload, status=200):
        route.fulfill(status=status, json=payload, headers={"Cache-Control": "no-store"})

    def status(self, job, state="running"):
        direction = self.job_directions.get(job, "out")
        result = {"rows": self.completed_rows, "path": FILE_PATH, "direction": direction} if state == "succeeded" else {}
        if state == "succeeded" and direction == "out" and self.completed_statistics is not None:
            result["statistics"] = self.completed_statistics
        if state == "succeeded" and direction == "out" and self.completed_bcp_statistics is not None:
            result.update(self.completed_bcp_statistics)
        return {
            "job": job, "state": state, "phase": "export", "message": f"mock job {state}",
            "rows": self.completed_rows if state == "succeeded" else 2,
            "events": [{"phase": "export", "message": HOSTILE_SAMPLE}],
            "result": result,
        }

    def handle(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        endpoint = parsed.path.removeprefix("/api/ase/")
        payload = request.post_data_json if request.method == "POST" else None
        headers = request.headers
        self.calls.append({"endpoint": endpoint, "method": request.method, "payload": payload, "url": request.url, "headers": headers})
        try:
            assert not parsed.query, "ASE request has a query string"
            assert PASSWORD not in request.url, "Password leaked into URL"
            assert FIXED_PROFILE_KEY not in request.url, "Fixed profile key must not be sent by the browser"
            assert "master_password" not in (payload or {}), "ASE profile key must not be supplied by the browser"
            assert headers.get("cache-control") == "no-store", "ASE request must disable caching"
            assert headers.get("x-nice-tools") == "ase", "ASE request is missing its browser-origin guard"
            if endpoint == "status":
                assert request.method == "GET"
                odbc = {**ODBC, **({"data_sources": self.data_sources} if self.data_sources is not None else {}), **({"data_sources_detail": self.data_sources_detail} if self.data_sources_detail else {})}
                return self.respond(route, {"available": self.runtime_available, "backend": "ODBC", "detail": "ready" if self.runtime_available else "SAP ASE ODBC 드라이버를 찾을 수 없습니다.", "platform": "win32", "odbc": odbc, "bcp_available": self.bcp_available, "bcp_detail": "ready" if self.bcp_available else "SAP bcp.exe를 찾을 수 없습니다.", **(CLIENT_PATHS if self.bcp_available else {})})
            if endpoint == "profiles":
                assert request.method == "GET"
                if self.delay_profiles:
                    self.delay_profiles = False
                    self.pending_profiles.append((route, self.profile_response()))
                    return
                if self.fail_profiles:
                    self.fail_profiles = False
                    return self.respond(route, {"detail": HOSTILE_ERROR}, status=400)
                return self.respond(route, self.profile_response())
            assert request.method == "POST", f"Unexpected {request.method} for {endpoint}"
            assert headers.get("content-type", "").startswith("application/json"), "ASE uses JSON, not uploads"
            assert isinstance(payload, dict)
            if endpoint == "connect":
                if self.fail_connect:
                    self.fail_connect = False
                    return self.respond(route, {"detail": "서버에 연결하지 못했습니다."}, status=400)
                response = self.timed({"session": SESSION, "databases": [{"id": 1, "name": "db1"}, {"id": 2, "name": "db2"}]})
                if "profile_id" in payload:
                    assert set(payload) == {"profile_id"}
                    if self.legacy_profile:
                        return self.respond(route, {"detail": "저장된 비밀번호를 복호화할 수 없습니다. 직접 입력으로 접속해 다시 저장하세요."}, status=400)
                    profile = next(profile for profile in self.profiles if profile["id"] == payload["profile_id"])
                    self.last_profile_id = profile["id"]
                    response.update({"profile_id": profile["id"], "profile_name": profile["name"], "database": profile.get("database")})
                else:
                    assert {key: value for key, value in payload.items() if key != "dsn"} in [
                        {"host": "ase.example.internal", "port": 5000, "user": "tester", "password": PASSWORD},
                        {"host": "ase.example.internal", "port": 5000, "user": "tester", "password": ""},
                    ]
                    assert payload.get("dsn", "") == self.expected_dsn
                    assert "dsn" not in payload or isinstance(payload["dsn"], str) and payload["dsn"], "Empty DSN must be omitted"
                    if self.fail_save:
                        response.update({"saved": False, "save_warning": HOSTILE_ERROR})
                    elif self.auto_save:
                        saved = {"id": "saved-new", "name": f"{payload['host']}-{payload['user']}", "host": payload["host"], "port": payload["port"], "user": payload["user"]}
                        if "dsn" in payload:
                            saved["dsn"] = payload["dsn"]
                        self.profiles = [profile for profile in self.profiles if profile["id"] != saved["id"]] + [saved]
                        self.profiles_exist = True
                        self.last_profile_id = saved["id"]
                        response.update({"saved": True, "profile_id": saved["id"], "profile_name": saved["name"]})
                        if self.replace_profile_after_save:
                            # An external file edit invalidates whole-file profile IDs before GET.
                            self.profiles = list(PROFILES)
                            self.last_profile_id = "main"
                            if self.replace_profile_after_save == "rotated":
                                self.profiles.append({**saved, "id": "rotated-profile"})
                            if self.replace_profile_after_save == "different-dsn":
                                self.profiles.append({**saved, "id": "other-dsn-profile", "dsn": "OTHER_ASE_DSN"})
                                self.last_profile_id = "other-dsn-profile"
                if self.delay_connect:
                    self.delay_connect = False
                    response["session"] = f"late-session-{len(self.endpoint_calls('connect'))}"
                    self.allowed_sessions.add(response["session"])
                    self.pending_connects.append((route, response))
                    return
                return self.respond(route, response)
            if endpoint == "disconnect":
                assert set(payload) == {"session"}
                assert payload["session"] in self.allowed_sessions
                return self.respond(route, {"ok": True})
            assert payload.get("session") == SESSION
            assert "password" not in payload and "master_password" not in payload and "host" not in payload and "user" not in payload, "Credentials must only be sent to connect"
            assert PASSWORD not in str(payload), "Credentials leaked into a later API request"
            assert FIXED_PROFILE_KEY not in str(payload), "Fixed profile key must not be sent by the browser"
            if endpoint == "owners":
                assert set(payload) == {"session", "database"}
                if self.delay_owners and payload["database"] == "db1":
                    self.delay_owners = False
                    self.pending_owners.append(route)
                    return
                owners = self.owners_override if self.owners_override is not None else [{"id": 1, "name": "dbo"}, {"id": 2, "name": "analytics"}, {"id": 3, "name": "empty_owner"}] if payload["database"] == "db1" else [{"id": 2, "name": "db2_owner"}]
                return self.respond(route, self.timed({"owners": owners}))
            if endpoint == "objects":
                assert set(payload) == {"session", "database", "owner_id", "search", "kind"}
                assert payload["owner_id"] is None or isinstance(payload["owner_id"], int)
                assert isinstance(payload["search"], str) and len(payload["search"]) <= 255
                assert payload["kind"] in {"all", "U", "V", "P"}
                objects = [TABLE, DUPLICATE_TABLE, HOSTILE_TABLE, VIEW, PROCEDURE] if payload["database"] == "db1" else []
                objects = [obj for obj in objects if (payload["owner_id"] is None or obj["owner_id"] == payload["owner_id"]) and (payload["kind"] == "all" or obj["kind"] == payload["kind"]) and payload["search"].casefold() in obj["name"].casefold()]
                response = self.timed({"objects": objects})
                if self.delay_objects:
                    self.delay_objects = False
                    self.pending_objects.append((route, response))
                    return
                return self.respond(route, response)
            if endpoint == "columns":
                assert set(payload) == {"session", "object"}
                if self.fail_columns:
                    self.fail_columns = False
                    return self.respond(route, {"detail": HOSTILE_ERROR}, status=500)
                return self.respond(route, self.timed({"object": payload["object"], "columns": COLUMNS}))
            if endpoint == "pick-file":
                assert set(payload) == {"session", "direction", "suggested_name"}
                assert payload["direction"] in {"in", "out"}
                if self.cancel_picker:
                    self.cancel_picker = False
                    return self.respond(route, {"cancelled": True})
                self.picker_number += 1
                return self.respond(route, {"cancelled": False, "file": f"native-file-token-{self.picker_number}", "path": FILE_PATH})
            if endpoint == "preview":
                assert set(payload) == {"session", "object", "file", "options"}
                assert payload["file"] == f"native-file-token-{self.picker_number}", "Preview must use the native token"
                assert payload["file"] != FILE_PATH and "path" not in payload, "Display paths must never select files"
                options = payload["options"]
                assert set(options) == {"direction", "encoding", "field_terminator", "row_terminator", "batch_size", "keep_identity"}
                assert options["encoding"] in {"utf-8", "cp949", "euc-kr"}
                assert isinstance(options["batch_size"], int)
                assert isinstance(options["keep_identity"], bool)
                assert options["direction"] in {"in", "out"}
                assert payload["object"]["kind"] == "U" or options["direction"] == "out"
                assert payload["object"]["kind"] != "P", "Procedures cannot use BCP"
                self.preview_number += 1
                review = f"review-{self.preview_number}"
                self.reviews[review] = payload
                response = {"review": review, "object": payload["object"], "columns": COLUMNS, "path": FILE_PATH, "rows": 3, "bytes": 100, "sample": [["1", HOSTILE_SAMPLE]], "warnings": []}
                if self.delay_preview:
                    self.delay_preview = False
                    self.pending_previews.append((route, response))
                    return
                return self.respond(route, response)
            if endpoint == "jobs":
                assert set(payload) == {"session", "review"}
                assert payload["review"] in self.reviews, "A reviewed preview is required before starting"
                self.job_number += 1
                job = f"job-{self.job_number}"
                self.status_polls[job] = 0
                self.job_directions[job] = self.reviews[payload["review"]]["options"]["direction"]
                if self.delay_start:
                    self.delay_start = False
                    self.pending_starts.append((route, {"job": job}))
                    return
                return self.respond(route, {"job": job})
            if endpoint == "job":
                assert set(payload) == {"session", "job"}
                job = payload["job"]
                self.status_polls[job] += 1
                state = "running" if self.hold_job or self.status_polls[job] < 2 else self.terminal_state
                return self.respond(route, self.status(job, state))
            if endpoint == "cancel":
                assert set(payload) == {"session", "job"}
                return self.respond(route, self.status(payload["job"], "cancelled"))
            raise AssertionError(f"Unexpected ASE endpoint: {endpoint}")
        except AssertionError as error:
            self.violations.append(str(error))
            self.respond(route, {"detail": "Browser mock contract assertion failed"}, status=500)

    def endpoint_calls(self, name):
        return [call for call in self.calls if call["endpoint"] == name]


def select_table(page, name=TABLE["name"], owner="dbo"):
    page.locator("#ase-object-body tr").filter(has=page.get_by_role("cell", name=owner, exact=True)).get_by_role("button", name=name, exact=True).click()
    expect(page.locator("#ase-column-body")).to_contain_text(HOSTILE)
    assert page.locator("#ase-column-body img").count() == 0


def focus_owner(page):
    # select_option changes the value without a real click's focus movement.
    page.locator("#ase-search").focus()
    page.locator("#ase-owner").focus()


def pick_and_preview(page, mock):
    page.locator("#ase-pick-file").click()
    expect(page.locator("#ase-path")).to_contain_text(FILE_PATH)
    expect(page.locator("#ase-preview")).to_be_enabled()
    page.locator("#ase-preview").click()
    expect(page.locator("#ase-review")).to_be_visible()
    expect(page.locator("#ase-review")).to_contain_text(HOSTILE_SAMPLE)
    assert page.locator("#ase-review svg, #ase-review script, #ase-review img").count() == 0
    assert PASSWORD not in page.locator("#ase-review").inner_text()
    assert not mock.violations, mock.violations


def assert_invalidated(page):
    expect(page.locator("#ase-review")).to_be_hidden()
    expect(page.locator("#ase-start")).to_be_disabled()


def validate(page, mock, url, screenshot, mobile_screenshot=None):
    errors, requests, uploads, console = [], [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    page.on("filechooser", lambda chooser: uploads.append(chooser))
    page.on("console", lambda message: console.append(message.text))
    page.add_init_script("""
        window.__aseXss = false;
        window.__aseStorageWrites = [];
        const originalSetItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          window.__aseStorageWrites.push({key, value});
          return originalSetItem.call(this, key, value);
        };
    """)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    ase_link = page.locator('a[data-tool="ase-tool"]')
    tool_title = ase_link.inner_text()
    ase_link.click()
    expect(page.locator("#ase-tool")).to_be_visible()
    expect(page.locator("#directory-tool")).to_be_hidden()
    expect(page.locator("#jasypt-tool")).to_be_hidden()
    expect(page.locator("#current-tool")).to_have_text(tool_title)
    expect(page.locator("#ase-password")).to_have_attribute("type", "password")
    assert page.locator("#ase-master-password, #ase-master-key-field").count() == 0
    expect(page.locator("#ase-odbc-driver")).to_contain_text(ODBC["driver"])
    expect(page.locator("#ase-client-paths")).to_contain_text(CLIENT_PATHS["bcp_path"])
    assert len(mock.endpoint_calls("profiles")) == 1, "ASE must load saved profiles during initialization"
    expect(page.locator("#ase-profile")).to_have_value("")
    expect(page.locator("#ase-profile-help")).to_contain_text("EXE와 같은 폴더")
    expect(page.locator("#ase-profile-path")).to_contain_text(PROFILE_PATH)
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-catalog-timings")).to_be_visible()
    expect(page.locator("#ase-catalog-timings")).to_contain_text("서버 연결 · 총 0.75초")
    expect(page.locator("#ase-catalog-timings")).to_contain_text("조회 0.50초")
    expect(page.locator("#ase-catalog-timings")).to_contain_text("앱 준비 0.15초")
    expect(page.locator("#ase-catalog-timings")).to_contain_text("로그인 0.10초")
    assert "CLI" not in page.locator("#ase-catalog-timings").inner_text()
    mock.timings = {"login_seconds": 0, "query_seconds": 0.5, "total_seconds": 0.75}
    page.locator("#ase-database").select_option("db1")
    expect(page.locator("#ase-owner")).to_be_enabled()
    assert not mock.endpoint_calls("owners"), "DB selection must remain local until Search"
    assert not mock.endpoint_calls("objects"), "DB selection must not fetch objects"
    expect(page.locator("#ase-owner")).to_have_value("")
    expect(page.locator("#ase-search-button")).to_be_enabled()
    expect(page.locator("#ase-catalog-timings")).to_be_hidden()
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-body button")).to_have_count(5)
    assert mock.endpoint_calls("objects")[-1]["payload"] == {"session": SESSION, "database": "db1", "owner_id": None, "search": "", "kind": "all"}
    expect(page.locator("#ase-object-body")).to_contain_text("analytics")
    expect(page.locator("#ase-catalog-timings")).to_contain_text("객체 검색 · 총 0.75초")
    select_table(page, owner="analytics")
    assert mock.endpoint_calls("columns")[-1]["payload"]["object"] == DUPLICATE_TABLE
    expect(page.locator("#ase-object-name")).to_have_text("db1.analytics.customer_rows")
    focus_owner(page)
    expect(page.locator("#ase-owner option[value='3']")).to_have_count(1)
    page.locator("#ase-owner").select_option("1")
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-body button")).to_have_count(4)
    expect(page.locator("#ase-object-body")).to_contain_text(HOSTILE)
    assert page.locator("#ase-object-body img").count() == 0

    # All filter changes stay local and invalidate old selections until Search.
    object_calls = len(mock.endpoint_calls("objects"))
    page.locator("#ase-kind").select_option("U")
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    page.locator("#ase-search").fill("customer_rows")
    assert len(mock.endpoint_calls("objects")) == object_calls
    for composition in ({"key": "Enter", "isComposing": True}, {"key": "Enter", "keyCode": 229}):
        page.locator("#ase-search").dispatch_event("keydown", composition)
        page.wait_for_timeout(30)
        assert len(mock.endpoint_calls("objects")) == object_calls, "IME completion Enter must not submit Search"
    page.locator("#ase-search").press("Enter")
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    assert mock.endpoint_calls("objects")[-1]["payload"] == {"session": SESSION, "database": "db1", "owner_id": 1, "search": "customer_rows", "kind": "U"}
    select_table(page)
    pick_and_preview(page, mock)
    page.locator("#ase-search").fill("customer")
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    assert_invalidated(page)
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    expect(page.locator("#ase-path")).not_to_contain_text(FILE_PATH)
    page.locator("#ase-kind").select_option("V")
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    expect(page.locator("#ase-object-body")).to_contain_text(VIEW["name"])
    page.locator("#ase-kind").select_option("P")
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    expect(page.locator("#ase-object-body")).to_contain_text(PROCEDURE["name"])
    page.locator("#ase-kind").select_option("all")
    page.locator("#ase-search").fill("")
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-body button")).to_have_count(4)

    # Missing and invalid timing payloads must not retain prior measurements.
    for timings in (None, {"total_seconds": -1}, {"total_seconds": HOSTILE_SAMPLE}, {"total_seconds": 0.5, "query_seconds": "0.1"}, {"total_seconds": 1, "login_seconds": -0.1}):
        mock.timings = timings
        select_table(page)
        expect(page.locator("#ase-catalog-timings")).to_be_hidden()
    mock.timings = {"login_seconds": 0, "query_seconds": 0.5, "total_seconds": 0.75}
    select_table(page)
    expect(page.locator("#ase-catalog-timings")).to_contain_text("컬럼 조회")
    expect(page.locator("#ase-catalog-timings")).to_contain_text("조회 0.50초")
    assert page.locator("#ase-catalog-timings svg").count() == 0
    mock.timings = {"login_seconds": 0, "query_seconds": 0.15, "total_seconds": 5.0}
    select_table(page)
    expect(page.locator("#ase-catalog-timings")).to_contain_text("앱 준비 4.85초")
    mock.timings = {"login_seconds": 0, "query_seconds": 0.5, "total_seconds": 0.75}

    # User tables import/export, views export, and procedures remain browse-only.
    select_table(page)
    expect(page.locator("#ase-in")).to_be_enabled()
    page.locator("#ase-in").check()
    expect(page.locator("#ase-in")).to_be_checked()
    select_table(page, VIEW["name"])
    expect(page.locator("#ase-in")).to_be_disabled()
    expect(page.locator("#ase-out")).to_be_checked()
    expect(page.locator("#ase-pick-file")).to_be_enabled()
    select_table(page, PROCEDURE["name"])
    expect(page.locator("#ase-in")).to_be_disabled()
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    expect(page.locator("#ase-preview")).to_be_disabled()
    select_table(page)

    # A new review is required whenever an option, mode, file, or object changes.
    page.locator("#ase-in").check()
    mutations = [
        lambda: page.locator("#ase-encoding").select_option("cp949"),
        lambda: page.locator("#ase-field-terminator").fill("|"),
        lambda: page.locator("#ase-row-terminator").fill(r"\r\n"),
        lambda: page.locator("#ase-batch-size").fill("2000"),
        lambda: page.locator("#ase-keep-identity").check(),
        lambda: page.locator("#ase-out").check(),
        lambda: page.locator("#ase-in").check(),
    ]
    for index, mutate in enumerate(mutations):
        pick_and_preview(page, mock)
        if index == 0:
            for width in (390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE review overflow at {width}px"
            if screenshot:
                page.screenshot(path=screenshot, full_page=True)
            if mobile_screenshot:
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=mobile_screenshot, full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
        mutate()
        assert_invalidated(page)
    pick_and_preview(page, mock)
    page.locator("#ase-pick-file").click()
    assert_invalidated(page)
    pick_and_preview(page, mock)
    select_table(page, HOSTILE)
    assert_invalidated(page)
    select_table(page)
    pick_and_preview(page, mock)
    mock.cancel_picker = True
    page.locator("#ase-pick-file").click()
    assert_invalidated(page)
    expect(page.locator("#ase-preview")).to_be_disabled()
    assert FILE_PATH not in page.locator("#ase-path").inner_text()

    # A late preview must not revive a review invalidated by changed settings.
    page.locator("#ase-pick-file").click()
    expect(page.locator("#ase-preview")).to_be_enabled()
    mock.delay_preview = True
    page.locator("#ase-preview").click()
    page.wait_for_function("document.querySelector('#ase-preview').disabled")
    page.locator("#ase-encoding").select_option("euc-kr")
    assert mock.pending_previews
    route, response = mock.pending_previews.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    assert_invalidated(page)

    # Successful jobs poll repeatedly, render event text safely, then unlock.
    page.locator("#ase-out").check()
    pick_and_preview(page, mock)
    expect(page.locator("#ase-start")).to_be_enabled()
    mock.delay_start = True
    page.locator("#ase-start").click()
    page.wait_for_timeout(30)
    assert mock.pending_starts
    expect(page.locator("#ase-profile")).to_be_disabled()
    expect(page.locator("#ase-profile-reload")).to_be_disabled()
    profile_calls = len(mock.endpoint_calls("profiles"))
    page.locator("#ase-profile-reload").dispatch_event("click")
    page.locator("#ase-profile").dispatch_event("change")
    assert len(mock.endpoint_calls("profiles")) == profile_calls
    route, response = mock.pending_starts.pop()
    mock.respond(route, response)
    expect(page.locator("#ase-out-statistics")).to_be_hidden()
    expect(page.locator("#ase-cancel")).to_be_enabled()
    expect(page.locator("#ase-database")).to_be_disabled()
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    expect(page.locator("#ase-profile")).to_be_disabled()
    expect(page.locator("#ase-profile-reload")).to_be_disabled()
    profile_calls = len(mock.endpoint_calls("profiles"))
    page.locator("#ase-profile-reload").dispatch_event("click")
    page.locator("#ase-profile").dispatch_event("change")
    assert len(mock.endpoint_calls("profiles")) == profile_calls
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    assert mock.status_polls["job-1"] >= 2
    expect(page.locator("#ase-cancel")).to_be_hidden()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert page.locator("#ase-job-status svg, #ase-job-status script, #ase-job-status img").count() == 0
    expect(page.locator("#ase-out-statistics")).to_be_visible()
    expect(page.locator("#ase-stat-rows")).to_have_text("1,234,567행")
    expect(page.locator("#ase-stat-elapsed")).to_have_text("1.25초")
    expect(page.locator("#ase-stat-rate")).to_have_text("987,653.6행/초")
    expect(page.locator("#ase-stat-bytes")).to_have_text("5 MiB")
    expect(page.locator("#ase-stat-bytes-exact")).to_have_text("5,242,880 바이트")
    expect(page.locator("#ase-out-statistics-note")).to_contain_text("작업 시작부터 파일 저장 완료까지")
    expect(page.locator("#ase-bcp-statistics")).to_be_hidden()

    # CLI duration and throughput are separate from total app overhead.
    mock.completed_rows = 12
    mock.completed_statistics = {"elapsed_seconds": 5, "rows_per_second": 2.4, "bytes_written": 100}
    mock.completed_bcp_statistics = {"bcp_seconds": 2, "bcp_rows_per_second": 6}
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    expect(page.locator("#ase-stat-elapsed")).to_have_text("5.00초")
    expect(page.locator("#ase-stat-rate")).to_have_text("2.4행/초")
    expect(page.locator("#ase-bcp-statistics")).to_contain_text("bcp.exe 실행 시간 2.00초")
    expect(page.locator("#ase-bcp-statistics")).to_contain_text("bcp.exe 실행 속도 6행/초")
    expect(page.locator("#ase-bcp-statistics")).to_contain_text("로그인 포함")
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE statistics overflow at {width}px"
    if screenshot:
        page.screenshot(path=screenshot, full_page=True)
    if mobile_screenshot:
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=mobile_screenshot, full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    for cli_statistics in ({"bcp_seconds": -1, "bcp_rows_per_second": 6}, {"bcp_seconds": 2, "bcp_rows_per_second": HOSTILE_SAMPLE}):
        mock.completed_bcp_statistics = cli_statistics
        pick_and_preview(page, mock)
        page.locator("#ase-start").click()
        expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
        expect(page.locator("#ase-out-statistics")).to_be_visible()
        expect(page.locator("#ase-bcp-statistics")).to_be_hidden()
        assert not page.locator("#ase-bcp-statistics svg, #ase-bcp-statistics script").count()

    # A zero-row export is a valid success and does not reuse prior totals.
    page.locator("#ase-field-terminator").fill(";")
    expect(page.locator("#ase-out-statistics")).to_be_hidden()
    assert page.locator("#ase-stat-rows").text_content() == ""
    mock.completed_rows = 0
    mock.completed_bcp_statistics = None
    mock.completed_statistics = {"elapsed_seconds": 0.5, "rows_per_second": 0, "bytes_written": 0}
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-out-statistics")).to_be_hidden()
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    expect(page.locator("#ase-out-statistics")).to_be_visible()
    expect(page.locator("#ase-stat-rows")).to_have_text("0행")
    expect(page.locator("#ase-stat-elapsed")).to_have_text("0.50초")
    expect(page.locator("#ase-stat-rate")).to_have_text("0행/초")
    expect(page.locator("#ase-stat-bytes")).to_have_text("0 B")
    expect(page.locator("#ase-bcp-statistics")).to_be_hidden()

    # An unmeasurably short duration must never display infinity or invented speed.
    mock.completed_statistics = {"elapsed_seconds": 0, "rows_per_second": None, "bytes_written": 0}
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    expect(page.locator("#ase-out-statistics")).to_be_visible()
    expect(page.locator("#ase-stat-rate")).to_have_text("측정 불가")

    # Old/invalid server responses and failed/cancelled exports cannot claim success metrics.
    for statistics in (None, {"elapsed_seconds": -1, "rows_per_second": 3, "bytes_written": 0}, {"elapsed_seconds": 1, "rows_per_second": HOSTILE_SAMPLE, "bytes_written": 0}):
        mock.completed_statistics = statistics
        pick_and_preview(page, mock)
        page.locator("#ase-start").click()
        expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
        expect(page.locator("#ase-out-statistics")).to_be_hidden()
        assert page.locator("#ase-out-statistics svg, #ase-out-statistics script, #ase-out-statistics img").count() == 0
    mock.completed_statistics = {"elapsed_seconds": 1.25, "rows_per_second": 2.4, "bytes_written": 100}
    for terminal_state in ("failed", "cancelled"):
        mock.terminal_state = terminal_state
        pick_and_preview(page, mock)
        page.locator("#ase-start").click()
        expect(page.locator("#ase-job-status")).to_contain_text(terminal_state, timeout=10000)
        expect(page.locator("#ase-out-statistics")).to_be_hidden()
    mock.terminal_state = "succeeded"
    page.locator("#ase-in").check()
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    expect(page.locator("#ase-out-statistics")).to_be_hidden()

    # Cancel an active job, and make metadata errors literal display text.
    mock.hold_job = True
    page.locator("#ase-in").check()
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-cancel")).to_be_enabled()
    page.locator("#ase-cancel").click()
    expect(page.locator("#ase-job-status")).to_contain_text("cancelled", timeout=10000)
    expect(page.locator("#ase-out-statistics")).to_be_hidden()
    expect(page.locator("#ase-job-warning")).to_be_visible()
    expect(page.locator("#ase-job-warning")).to_contain_text("완료된 배치")
    expect(page.locator("#ase-job-warning")).to_contain_text("중복")
    expect(page.locator("#ase-database")).to_be_enabled()
    assert len(mock.endpoint_calls("cancel")) == 1
    mock.fail_columns = True
    page.locator("#ase-object-body").get_by_role("button", name=VIEW["name"], exact=True).click()
    expect(page.locator("#ase-feedback")).to_contain_text(HOSTILE_ERROR)
    assert page.locator("#ase-feedback script").count() == 0

    # Selecting a DB is local; an optional owner lookup starts only on focus.
    page.locator("#ase-database").select_option("db2")
    focus_owner(page)
    expect(page.locator("#ase-owner option[value='2']")).to_have_count(1)
    mock.delay_owners = True
    page.locator("#ase-database").select_option("db1")
    focus_owner(page)
    page.wait_for_timeout(30)
    assert mock.pending_owners
    page.locator("#ase-database").select_option("db2")
    focus_owner(page)
    expect(page.locator("#ase-owner option[value='2']")).to_have_count(1)
    mock.respond(mock.pending_owners.pop(), {"owners": [{"id": 1, "name": "stale_owner"}]})
    page.wait_for_timeout(100)
    expect(page.locator("#ase-database")).to_have_value("db2")
    expect(page.locator("#ase-owner")).to_contain_text("db2_owner")
    assert "stale_owner" not in page.locator("#ase-owner").inner_text()

    # A late object search cannot replace the current DB or changed filters.
    page.locator("#ase-database").select_option("db1")
    mock.delay_objects = True
    page.locator("#ase-search-button").click()
    page.wait_for_timeout(30)
    assert mock.pending_objects
    page.locator("#ase-database").select_option("db2")
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-object-empty")).to_have_text("조건에 맞는 객체가 없습니다.")
    route, response = mock.pending_objects.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    expect(page.locator("#ase-owner")).to_have_value("")
    page.locator("#ase-database").select_option("db1")
    mock.delay_objects = True
    page.locator("#ase-search-button").click()
    page.wait_for_timeout(30)
    assert mock.pending_objects
    page.locator("#ase-search").fill("changed")
    route, response = mock.pending_objects.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    expect(page.locator("#ase-object-empty")).to_contain_text("검색")
    expect(page.locator("#ase-catalog-timings")).to_be_hidden()

    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE overflow at {width}px"

    # Disconnect also cancels an active import and explains committed batches.
    page.locator("#ase-database").select_option("db1")
    expect(page.locator("#ase-owner")).to_be_enabled()
    page.locator("#ase-search").fill("")
    page.locator("#ase-search-button").click()
    select_table(page)
    page.locator("#ase-in").check()
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-cancel")).to_be_enabled()
    page.locator("#ase-disconnect").click()
    expect(page.locator("#ase-host")).to_be_enabled()
    expect(page.locator("#ase-database")).to_be_disabled()
    expect(page.locator("#ase-start")).to_be_disabled()
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-catalog-timings")).to_be_hidden()
    expect(page.locator("#ase-feedback")).to_contain_text("완료된 배치")
    expect(page.locator("#ase-feedback")).to_contain_text("중복")
    assert len(mock.endpoint_calls("cancel")) == 2
    assert len(mock.endpoint_calls("disconnect")) == 1
    assert len(mock.endpoint_calls("connect")) == 1
    assert len(mock.endpoint_calls("status")) >= 1
    assert not mock.violations, mock.violations
    assert not uploads, "ASE must use the desktop native picker, not a browser upload"
    assert page.locator("#ase-tool input[type='file']").count() == 0
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert page.evaluate("window.__aseStorageWrites") == []
    assert page.evaluate("window.__aseXss") is False
    assert all(PASSWORD not in message for message in console), "Credentials leaked into the browser console"
    assert not errors, errors
    assert all(urlsplit(request).netloc == urlsplit(url).netloc for request in requests), requests


def validate_profiles(page, url):
    """Exercise public profiles, password-free saved login, fallback, and races in the real UI."""
    mock = ASEMock(profiles=list(PROFILES), profiles_exist=True)
    errors, console = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: console.append(message.text))
    page.add_init_script("""
        window.__aseXss = false;
        window.__aseStorageWrites = [];
        const originalSetItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          window.__aseStorageWrites.push({key, value});
          return originalSetItem.call(this, key, value);
        };
    """)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-profile")).to_have_value("main")
    expect(page.locator("#ase-profile")).to_contain_text(HOSTILE)
    expect(page.locator("#ase-host")).to_have_value("ase.saved.internal")
    expect(page.locator("#ase-port")).to_have_value("5001")
    expect(page.locator("#ase-user")).to_have_value("saved_user")
    for selector in ("#ase-host", "#ase-port", "#ase-user"):
        expect(page.locator(selector)).to_have_attribute("readonly", "")
    expect(page.locator("#ase-password-field")).to_be_hidden()
    expect(page.locator("#ase-password")).to_be_disabled()
    expect(page.locator("#ase-profile-jasypt")).to_contain_text("PBEWithMD5AndDES")
    expect(page.locator("#ase-profile-jasypt")).to_contain_text("1,000")
    page.locator("#ase-profile").select_option("hostile")
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-host")).to_have_value("ase.backup.internal")
    page.locator("#ase-profile").select_option("main")
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-database")).to_have_value("db1")
    expect(page.locator("#ase-owner")).to_have_value("")
    assert not mock.endpoint_calls("owners"), "Profile default DB must not query owners automatically"
    assert not mock.endpoint_calls("objects"), "Profile default DB must await Search"
    focus_owner(page)
    expect(page.locator("#ase-owner option[value='1']")).to_have_count(1)
    page.locator("#ase-owner").select_option("1")
    assert not mock.endpoint_calls("objects"), "Choosing an owner must await Search"
    page.locator("#ase-search-button").click()
    select_table(page)
    pick_and_preview(page, mock)
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-database")).to_be_disabled()
    expect(page.locator("#ase-database")).to_have_value("")
    expect(page.locator("#ase-owner")).to_have_value("")
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    assert_invalidated(page)
    expect(page.locator("#ase-path")).not_to_contain_text(FILE_PATH)
    expect(page.locator("#ase-connect")).to_be_enabled()
    assert len(mock.endpoint_calls("disconnect")) == 1

    # An unknown configured database leaves normal DB selection available.
    page.locator("#ase-profile").select_option("hostile")
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-database")).to_have_value("")
    page.locator("#ase-profile").select_option("")
    expect(page.locator("#ase-connect")).to_be_enabled()
    expect(page.locator("#ase-password-label")).to_have_text("DB 비밀번호")
    for selector in ("#ase-host", "#ase-port", "#ase-user"):
        expect(page.locator(selector)).not_to_have_attribute("readonly", "")
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-host").fill("ase.example.internal")
    expect(page.locator("#ase-password")).to_have_value("")
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert mock.endpoint_calls("connect")[-1]["payload"]["password"] == ""
    page.locator("#ase-disconnect").click()
    expect(page.locator("#ase-connect")).to_be_enabled()

    # A malformed file reports literal text and keeps direct entry available.
    page.locator("#ase-profile").select_option("main")
    mock.fail_profiles = True
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-profile-help")).to_contain_text(HOSTILE_ERROR)
    page.locator("#ase-profile").select_option("")
    expect(page.locator("#ase-connect")).to_be_enabled()
    assert page.locator("#ase-profile-help script, #ase-profile option img").count() == 0
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-profile")).to_have_value("hostile")
    page.locator("#ase-profile").select_option("main")

    # Switching profiles while a reload is held must invalidate that response and key.
    mock.delay_profiles = True
    page.locator("#ase-profile-reload").click()
    page.wait_for_timeout(30)
    assert mock.pending_profiles
    page.locator("#ase-profile").select_option("hostile")
    route, response = mock.pending_profiles.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-profile")).to_have_value("hostile")
    expect(page.locator("#ase-password")).to_have_value("")

    # Reversed reload completion cannot replace the newest file contents.
    mock.delay_profiles = True
    page.locator("#ase-profile-reload").click()
    page.wait_for_timeout(30)
    assert mock.pending_profiles
    mock.profiles = [PROFILES[0]]
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-profile")).to_have_value("main")
    route, response = mock.pending_profiles.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-profile option[value='hostile']")).to_have_count(0)
    expect(page.locator("#ase-password")).to_have_value("")

    # Reloading during connect abandons its late session without reviving the key.
    mock.delay_connect = True
    page.locator("#ase-connect").click()
    page.wait_for_timeout(30)
    assert mock.pending_connects
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-connect")).to_be_enabled()
    # A new connection can browse while the abandoned request still completes.
    mock.delay_owners = True
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_have_value("db1")
    focus_owner(page)
    page.wait_for_timeout(30)
    assert mock.pending_owners
    expect(page.locator("#ase-browser")).to_have_attribute("aria-busy", "true")
    route, response = mock.pending_connects.pop()
    disconnects = len(mock.endpoint_calls("disconnect"))
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-database")).to_have_value("db1")
    expect(page.locator("#ase-browser")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#ase-password")).to_have_value("")
    assert len(mock.endpoint_calls("disconnect")) == disconnects + 1, "Abandoned connect must release its returned session"
    assert mock.endpoint_calls("disconnect")[-1]["payload"] == {"session": response["session"]}
    assert response["session"] != SESSION, "Stale cleanup must not disconnect the current session"
    mock.respond(mock.pending_owners.pop(), {"owners": [{"id": 1, "name": "dbo"}]})
    expect(page.locator("#ase-browser")).to_have_attribute("aria-busy", "false")

    # Switching profiles is also available during a connection request and cancels it.
    mock.profiles = list(PROFILES)
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-profile option[value='hostile']")).to_have_count(1)
    mock.delay_connect = True
    page.locator("#ase-connect").click()
    page.wait_for_timeout(30)
    assert mock.pending_connects
    page.locator("#ase-profile").select_option("hostile")
    route, response = mock.pending_connects.pop()
    disconnects = len(mock.endpoint_calls("disconnect"))
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-profile")).to_have_value("hostile")
    expect(page.locator("#ase-host")).to_have_value("ase.backup.internal")
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-database")).to_be_disabled()
    assert len(mock.endpoint_calls("disconnect")) == disconnects + 1, "Changed profile must release an abandoned session"

    # Removing the settings file returns to empty manual fields, without an old key.
    mock.profiles = []
    mock.profiles_exist = False
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-profile")).to_have_value("")
    expect(page.locator("#ase-host")).to_have_value("")
    expect(page.locator("#ase-user")).to_have_value("")
    expect(page.locator("#ase-password")).to_have_value("")
    mock.profiles = [PROFILES[0]]
    mock.profiles_exist = True
    page.locator("#ase-profile-reload").click()
    expect(page.locator("#ase-profile")).to_have_value("main")

    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE profile overflow at {width}px"
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert page.evaluate("window.__aseStorageWrites") == []
    assert page.evaluate("window.__aseXss") is False
    assert all(PASSWORD not in message and FIXED_PROFILE_KEY not in message for message in console)
    assert not mock.violations, mock.violations
    assert not errors, errors

    # Initial loading remains independent of the native availability check.
    mock.runtime_available = False
    page.reload(wait_until="networkidle")
    expect(page.locator("#ase-profile")).to_have_value("main")
    expect(page.locator("#ase-connect")).to_be_disabled()
    expect(page.locator("#ase-runtime")).to_contain_text("SAP ASE ODBC 드라이버를 찾을 수 없습니다.")

    # A delayed initial file read must not overwrite manual fields or a newly typed DB password.
    mock.runtime_available = True
    mock.delay_profiles = True
    page.reload(wait_until="domcontentloaded")
    page.locator('a[data-tool="ase-tool"]').click()
    page.locator("#ase-host").fill("manual.changed.internal")
    page.locator("#ase-password").fill(PASSWORD)
    assert mock.pending_profiles
    route, response = mock.pending_profiles.pop()
    mock.respond(route, response)
    page.wait_for_timeout(100)
    expect(page.locator("#ase-profile")).to_have_value("")
    expect(page.locator("#ase-host")).to_have_value("manual.changed.internal")
    expect(page.locator("#ase-password")).to_have_value(PASSWORD)
    assert not mock.violations, mock.violations
    assert not errors, errors


def validate_saved_connections(page, url):
    """Saving a successful manual login must preserve its session and next-launch choice."""
    mock = ASEMock(profiles=list(PROFILES), profiles_exist=True, last_profile_id="hostile", auto_save=True)
    errors, console = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: console.append(message.text))
    page.add_init_script("""
        window.__aseStorageWrites = [];
        const originalSetItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          window.__aseStorageWrites.push({key, value});
          return originalSetItem.call(this, key, value);
        };
    """)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-profile")).to_have_value("hostile")
    assert page.locator("#ase-master-key-field").count() == 0
    page.locator("#ase-profile").select_option("")
    expect(page.locator("#ase-password-field")).to_be_visible()
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-profile")).to_have_value("saved-new")
    expect(page.locator("#ase-profile option:checked")).to_contain_text("ase.example.internal-tester")
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-password")).to_have_value("")
    expect(page.locator("#ase-feedback")).to_contain_text("저장")
    assert not mock.endpoint_calls("disconnect"), "Refreshing saved profiles must preserve the newly opened session"
    page.locator("#ase-database").select_option("db1")
    page.locator("#ase-search-button").click()
    select_table(page)
    assert not mock.violations, mock.violations
    assert len(mock.endpoint_calls("connect")) == 1
    assert len(mock.endpoint_calls("profiles")) == 2

    page.locator("#ase-disconnect").click()
    expect(page.locator("#ase-connect")).to_be_enabled()
    page.reload(wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-profile")).to_have_value("saved-new")
    expect(page.locator("#ase-host")).to_have_value("ase.example.internal")
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert mock.endpoint_calls("connect")[-1]["payload"] == {"profile_id": "saved-new"}
    page.locator("#ase-disconnect").click()

    # A file write error keeps the successful login usable and shows only safe text.
    page.locator("#ase-profile").select_option("")
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    mock.fail_save = True
    profiles_before = len(mock.endpoint_calls("profiles"))
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-feedback")).to_contain_text(HOSTILE_ERROR)
    assert not page.locator("#ase-feedback script").count()
    expect(page.locator("#ase-password")).to_have_value("")
    assert len(mock.endpoint_calls("profiles")) == profiles_before
    page.locator("#ase-disconnect").click()

    mock.fail_connect = True
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-feedback")).to_contain_text("서버에 연결하지 못했습니다.")
    expect(page.locator("#ase-database")).to_be_disabled()
    expect(page.locator("#ase-password")).to_have_value("")
    assert len(mock.endpoint_calls("profiles")) == profiles_before, "Failed login must not refresh or save a profile"
    assert mock.last_profile_id == "saved-new"
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert page.evaluate("window.__aseStorageWrites") == []
    assert all(PASSWORD not in message and FIXED_PROFILE_KEY not in message for message in console)
    assert not mock.violations, mock.violations
    assert not errors, errors


def validate_saved_profile_race(page, url, retain_profile=False, different_dsn=False):
    """A changed file must not make a live connection appear to target another server."""
    replacement = "different-dsn" if different_dsn else "rotated" if retain_profile else True
    mock = ASEMock(auto_save=True, replace_profile_after_save=replacement, expected_dsn="TEST_ASE_DSN" if different_dsn else "", data_sources=[{"name": "TEST_ASE_DSN", "driver": ODBC["driver"]}] if different_dsn else None)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    if different_dsn:
        page.locator("#ase-dsn").select_option("TEST_ASE_DSN")
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-profile")).to_have_value("rotated-profile" if retain_profile else "")
    expect(page.locator("#ase-host")).to_have_value("ase.example.internal")
    expect(page.locator("#ase-port")).to_have_value("5000")
    expect(page.locator("#ase-user")).to_have_value("tester")
    if different_dsn:
        expect(page.locator("#ase-dsn")).to_have_value("TEST_ASE_DSN")
    assert not mock.endpoint_calls("disconnect")
    page.locator("#ase-database").select_option("db1")
    page.locator("#ase-search-button").click()
    select_table(page)
    assert not mock.violations, mock.violations


def validate_legacy_profile(page, url):
    """Old user-key profiles give a re-save path without asking for an encryption key."""
    mock = ASEMock(profiles=[PROFILES[0]], profiles_exist=True, legacy_profile=True, auto_save=True)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-password-field")).to_be_hidden()
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-feedback")).to_contain_text("직접 입력으로 접속해 다시 저장")
    assert mock.endpoint_calls("connect")[-1]["payload"] == {"profile_id": "main"}
    expect(page.locator("#ase-database")).to_be_disabled()
    page.locator("#ase-profile").select_option("")
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-profile")).to_have_value("saved-new")
    expect(page.locator("#ase-feedback")).to_contain_text("저장")
    page.locator("#ase-disconnect").click()
    mock.legacy_profile = False
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert mock.endpoint_calls("connect")[-1]["payload"] == {"profile_id": "saved-new"}
    assert page.locator("#ase-master-password, #ase-master-key-field").count() == 0
    assert FIXED_PROFILE_KEY not in page.locator("#ase-tool").inner_text()
    page.locator('a[data-tool="jasypt-tool"]').click()
    page.locator("#jasypt-password").fill("independent-custom-key")
    expect(page.locator("#jasypt-password")).to_have_value("independent-custom-key")
    assert not mock.violations, mock.violations


def validate_driver_status(page, url):
    """ODBC catalog access remains useful when the installed BCP tool is missing."""
    mock = ASEMock(bcp_available=False)
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-odbc-driver")).to_contain_text(ODBC["driver"])
    expect(page.locator("#ase-bcp-runtime")).to_contain_text("bcp.exe")
    expect(page.locator("#ase-connect")).to_be_enabled()
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    page.locator("#ase-database").select_option("db1")
    page.locator("#ase-search-button").click()
    select_table(page)
    expect(page.locator("#ase-column-body")).to_contain_text("varchar(255)")
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    expect(page.locator("#ase-preview")).to_be_disabled()
    expect(page.locator("#ase-start")).to_be_disabled()
    expect(page.locator("#ase-transfer-note")).to_contain_text("bcp.exe")
    assert not mock.endpoint_calls("pick-file")
    mock.runtime_available = False
    page.reload(wait_until="networkidle")
    expect(page.locator("#ase-connect")).to_be_disabled()
    expect(page.locator("#ase-runtime")).to_contain_text("ODBC 드라이버")
    assert not mock.violations, mock.violations


def validate_dsns(page, url):
    """Registered names, manual overrides, saved DSNs, and missing registration are explicit."""
    dsn = "한글 ASE 테스트"
    dsn_driver = "SAP ASE ODBC Driver"
    mock = ASEMock(auto_save=True, expected_dsn=dsn, data_sources=[{"name": "TEST_ASE_DSN", "driver": ODBC["driver"]}, {"name": dsn, "driver": dsn_driver}, {"name": HOSTILE, "driver": ODBC["driver"]}])
    page.add_init_script("window.__aseXss = false;")
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-dsn")).to_have_value("")
    expect(page.locator("#ase-dsn option")).to_have_count(4)
    expect(page.locator("#ase-dsn option[value='']")).to_have_text("직접 연결 (DSN 없음)")
    expect(page.locator("#ase-dsn-help")).to_contain_text("서버·포트·계정은 입력값")
    assert not mock.endpoint_calls("connect")
    page.locator("#ase-dsn").select_option("TEST_ASE_DSN")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-dsn").select_option(HOSTILE)
    expect(page.locator("#ase-password")).to_have_value("")
    assert page.locator("#ase-dsn option img").count() == 0
    page.locator("#ase-dsn").select_option(dsn)
    expect(page.locator("#ase-odbc-driver")).to_contain_text(dsn_driver)
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-profile")).to_have_value("saved-new")
    expect(page.locator("#ase-dsn")).to_have_value(dsn)
    expect(page.locator("#ase-dsn")).to_be_disabled()
    assert mock.endpoint_calls("connect")[-1]["payload"]["dsn"] == dsn
    assert not mock.endpoint_calls("disconnect"), "Saved DSN refresh must keep the active session"
    page.locator("#ase-disconnect").click()
    mock.data_sources = []
    mock.data_sources_detail = "DSN 목록을 읽지 못했습니다. ODBC 설정을 확인하세요."
    page.reload(wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-dsn")).to_have_value(dsn)
    expect(page.locator("#ase-dsn-help")).to_contain_text(mock.data_sources_detail)
    assert "등록된 64비트 SAP ASE DSN이 없습니다" not in page.locator("#ase-dsn-help").inner_text()
    expect(page.locator("#ase-dsn option[value='" + dsn + "']")).to_contain_text("등록 확인 필요")
    mock.data_sources_detail = ""
    page.reload(wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-dsn")).to_have_value(dsn)
    expect(page.locator("#ase-dsn option[value='" + dsn + "']")).to_contain_text("등록되지 않은 DSN")
    expect(page.locator("#ase-dsn-help")).to_contain_text("등록되어 있지 않습니다")
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert mock.endpoint_calls("connect")[-1]["payload"] == {"profile_id": "saved-new"}
    page.locator("#ase-profile").select_option("")
    expect(page.locator("#ase-dsn")).to_have_value("")
    expect(page.locator("#ase-dsn-help")).to_contain_text("등록된 64비트 SAP ASE DSN이 없습니다")
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE DSN overflow at {width}px"
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert page.evaluate("window.__aseXss") is False
    assert not mock.violations, mock.violations


def validate_dsn_case(page, url):
    """Saved DSN spelling is preserved while registration and driver lookup ignore case."""
    registered = "TEST_ASE_CASE_DSN"
    saved_name = registered.lower()
    driver = "SAP ASE ODBC Driver"
    profile = {**PROFILES[0], "dsn": saved_name}
    mock = ASEMock(profiles=[profile], profiles_exist=True, data_sources=[{"name": registered, "driver": driver}])
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    expect(page.locator("#ase-dsn")).to_have_value(saved_name)
    assert "등록되어 있지 않습니다" not in page.locator("#ase-dsn-help").inner_text()
    assert "등록되지 않은 DSN" not in page.locator("#ase-dsn option:checked").inner_text()
    expect(page.locator("#ase-odbc-driver")).to_contain_text(driver)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-dsn")).to_have_value(saved_name)
    assert mock.endpoint_calls("connect")[-1]["payload"] == {"profile_id": "main"}
    assert not mock.endpoint_calls("disconnect")
    assert not mock.violations, mock.violations


def validate_removed_owner(page, url):
    """A full owner refresh must invalidate a now-missing selected owner's review."""
    mock = ASEMock(owners_override=[{"id": 1, "name": "dbo"}])
    page.route("**/api/ase/**", mock.handle)
    page.goto(url, wait_until="networkidle")
    page.locator('a[data-tool="ase-tool"]').click()
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    page.locator("#ase-database").select_option("db1")
    page.locator("#ase-search-button").click()
    expect(page.locator("#ase-owner option[value='2']")).to_have_count(1)
    page.locator("#ase-owner").select_option("2")
    page.locator("#ase-search-button").click()
    select_table(page, owner="analytics")
    pick_and_preview(page, mock)
    focus_owner(page)
    expect(page.locator("#ase-owner")).to_have_value("")
    expect(page.locator("#ase-object-body button")).to_have_count(0)
    assert_invalidated(page)
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    assert not mock.violations, mock.violations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--screenshot")
    parser.add_argument("--mobile-screenshot")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        validate(context.new_page(), ASEMock(auto_save=True), args.url, args.screenshot, args.mobile_screenshot)
        validate_profiles(context.new_page(), args.url)
        validate_saved_connections(context.new_page(), args.url)
        validate_saved_profile_race(context.new_page(), args.url)
        validate_saved_profile_race(context.new_page(), args.url, retain_profile=True)
        validate_saved_profile_race(context.new_page(), args.url, different_dsn=True)
        validate_dsns(context.new_page(), args.url)
        validate_dsn_case(context.new_page(), args.url)
        validate_legacy_profile(context.new_page(), args.url)
        validate_driver_status(context.new_page(), args.url)
        validate_removed_owner(context.new_page(), args.url)
        browser.close()
    print("ASE browser: registered DSNs, saved spelling and unavailable choices, DSN discovery/driver diagnostics and session identity, explicit Search/Enter with IME guard, optional/lazy owners, server filters, duplicate names, stale responses, SAP ODBC driver/BCP paths and persistent-connection timings, saved connections, fixed-key profiles, job locks, OUT statistics, native file tokens, safe text, U/V/P modes, previews, polling/cancel, storage and responsive widths passed.")


if __name__ == "__main__":
    main()

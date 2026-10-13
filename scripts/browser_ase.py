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
FILE_PATH = r"C:\files\sample.txt"
HOSTILE = '<img src=x onerror="window.__aseXss=true">'
HOSTILE_ERROR = '<script>window.__aseXss=true</script> request failed'
HOSTILE_SAMPLE = '<svg onload="window.__aseXss=true">sample</svg>'
TABLE = {"database": "db1", "owner": "dbo", "owner_id": 1, "id": 10, "name": "customer_rows", "kind": "U"}
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
    pending_previews: list = field(default_factory=list)
    reviews: dict = field(default_factory=dict)

    def respond(self, route, payload, status=200):
        route.fulfill(status=status, json=payload, headers={"Cache-Control": "no-store"})

    def status(self, job, state="running"):
        return {
            "job": job, "state": state, "phase": "export", "message": f"mock job {state}",
            "rows": 3 if state == "succeeded" else 2,
            "events": [{"phase": "export", "message": HOSTILE_SAMPLE}],
            "result": {"rows": 3, "path": FILE_PATH} if state == "succeeded" else {},
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
            assert headers.get("cache-control") == "no-store", "ASE request must disable caching"
            assert headers.get("x-nice-tools") == "ase", "ASE request is missing its browser-origin guard"
            if endpoint == "status":
                assert request.method == "GET"
                return self.respond(route, {"available": True, "version": "16.0", "detail": "ready", "platform": "win32"})
            assert request.method == "POST", f"Unexpected {request.method} for {endpoint}"
            assert headers.get("content-type", "").startswith("application/json"), "ASE uses JSON, not uploads"
            assert isinstance(payload, dict)
            if endpoint == "connect":
                assert payload == {"host": "ase.example.internal", "port": 5000, "user": "tester", "password": PASSWORD}
                return self.respond(route, {"session": SESSION, "databases": [{"id": 1, "name": "db1"}, {"id": 2, "name": "db2"}]})
            assert payload.get("session") == SESSION
            assert "password" not in payload and "host" not in payload and "user" not in payload, "Credentials must only be sent to connect"
            assert PASSWORD not in str(payload), "Credentials leaked into a later API request"
            if endpoint == "owners":
                assert set(payload) == {"session", "database"}
                if self.delay_owners and payload["database"] == "db1":
                    self.delay_owners = False
                    self.pending_owners.append(route)
                    return
                owner = {"id": 1, "name": "dbo"} if payload["database"] == "db1" else {"id": 2, "name": "db2_owner"}
                return self.respond(route, {"owners": [owner]})
            if endpoint == "objects":
                assert set(payload) == {"session", "database", "owner_id"}
                assert isinstance(payload["owner_id"], int)
                objects = [TABLE, HOSTILE_TABLE, VIEW, PROCEDURE] if payload["database"] == "db1" else []
                return self.respond(route, {"objects": objects})
            if endpoint == "columns":
                assert set(payload) == {"session", "object"}
                if self.fail_columns:
                    self.fail_columns = False
                    return self.respond(route, {"detail": HOSTILE_ERROR}, status=500)
                return self.respond(route, {"object": payload["object"], "columns": COLUMNS})
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
                return self.respond(route, {"job": job})
            if endpoint == "job":
                assert set(payload) == {"session", "job"}
                job = payload["job"]
                self.status_polls[job] += 1
                state = "running" if self.hold_job or self.status_polls[job] < 2 else "succeeded"
                return self.respond(route, self.status(job, state))
            if endpoint == "cancel":
                assert set(payload) == {"session", "job"}
                return self.respond(route, self.status(payload["job"], "cancelled"))
            if endpoint == "disconnect":
                assert set(payload) == {"session"}
                return self.respond(route, {"ok": True})
            raise AssertionError(f"Unexpected ASE endpoint: {endpoint}")
        except AssertionError as error:
            self.violations.append(str(error))
            self.respond(route, {"detail": "Browser mock contract assertion failed"}, status=500)

    def endpoint_calls(self, name):
        return [call for call in self.calls if call["endpoint"] == name]


def select_table(page, name=TABLE["name"]):
    page.locator("#ase-object-body").get_by_role("button", name=name, exact=True).click()
    expect(page.locator("#ase-column-body")).to_contain_text(HOSTILE)
    assert page.locator("#ase-column-body img").count() == 0


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
    page.locator("#ase-host").fill("ase.example.internal")
    page.locator("#ase-port").fill("5000")
    page.locator("#ase-user").fill("tester")
    page.locator("#ase-password").fill(PASSWORD)
    page.locator("#ase-connect").click()
    expect(page.locator("#ase-database")).to_be_enabled()
    expect(page.locator("#ase-password")).to_have_value("")
    page.locator("#ase-database").select_option("db1")
    expect(page.locator("#ase-owner")).to_be_enabled()
    page.locator("#ase-owner").select_option("1")
    expect(page.locator("#ase-object-body button")).to_have_count(4)
    expect(page.locator("#ase-object-body")).to_contain_text(HOSTILE)
    assert page.locator("#ase-object-body img").count() == 0

    # Kind and search filtering stays local after the metadata request.
    object_calls = len(mock.endpoint_calls("objects"))
    page.locator("#ase-kind").select_option("U")
    expect(page.locator("#ase-object-body button")).to_have_count(2)
    page.locator("#ase-search").fill("customer_rows")
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    page.locator("#ase-search").fill("")
    page.locator("#ase-kind").select_option("V")
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    expect(page.locator("#ase-object-body")).to_contain_text(VIEW["name"])
    page.locator("#ase-kind").select_option("P")
    expect(page.locator("#ase-object-body button")).to_have_count(1)
    expect(page.locator("#ase-object-body")).to_contain_text(PROCEDURE["name"])
    page.locator("#ase-kind").select_option("all")
    expect(page.locator("#ase-object-body button")).to_have_count(4)
    assert len(mock.endpoint_calls("objects")) == object_calls

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
    pick_and_preview(page, mock)
    expect(page.locator("#ase-start")).to_be_enabled()
    page.locator("#ase-start").click()
    expect(page.locator("#ase-cancel")).to_be_enabled()
    expect(page.locator("#ase-database")).to_be_disabled()
    expect(page.locator("#ase-pick-file")).to_be_disabled()
    expect(page.locator("#ase-job-status")).to_contain_text("succeeded", timeout=10000)
    assert mock.status_polls["job-1"] >= 2
    expect(page.locator("#ase-cancel")).to_be_hidden()
    expect(page.locator("#ase-database")).to_be_enabled()
    assert page.locator("#ase-job-status svg, #ase-job-status script, #ase-job-status img").count() == 0

    # Cancel an active job, and make metadata errors literal display text.
    mock.hold_job = True
    page.locator("#ase-in").check()
    pick_and_preview(page, mock)
    page.locator("#ase-start").click()
    expect(page.locator("#ase-cancel")).to_be_enabled()
    page.locator("#ase-cancel").click()
    expect(page.locator("#ase-job-status")).to_contain_text("cancelled", timeout=10000)
    expect(page.locator("#ase-job-warning")).to_be_visible()
    expect(page.locator("#ase-job-warning")).to_contain_text("완료된 배치")
    expect(page.locator("#ase-job-warning")).to_contain_text("중복")
    expect(page.locator("#ase-database")).to_be_enabled()
    assert len(mock.endpoint_calls("cancel")) == 1
    mock.fail_columns = True
    page.locator("#ase-object-body").get_by_role("button", name=VIEW["name"], exact=True).click()
    expect(page.locator("#ase-feedback")).to_contain_text(HOSTILE_ERROR)
    assert page.locator("#ase-feedback script").count() == 0

    # Switching database while an older owner lookup is pending discards it.
    page.locator("#ase-database").select_option("db2")
    expect(page.locator("#ase-owner option[value='2']")).to_have_count(1)
    mock.delay_owners = True
    page.locator("#ase-database").select_option("db1")
    page.wait_for_timeout(30)
    assert mock.pending_owners
    page.locator("#ase-database").select_option("db2")
    expect(page.locator("#ase-owner option[value='2']")).to_have_count(1)
    mock.respond(mock.pending_owners.pop(), {"owners": [{"id": 1, "name": "stale_owner"}]})
    page.wait_for_timeout(100)
    expect(page.locator("#ase-database")).to_have_value("db2")
    expect(page.locator("#ase-owner")).to_contain_text("db2_owner")
    assert "stale_owner" not in page.locator("#ase-owner").inner_text()

    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"ASE overflow at {width}px"

    # Disconnect also cancels an active import and explains committed batches.
    page.locator("#ase-database").select_option("db1")
    expect(page.locator("#ase-owner")).to_be_enabled()
    page.locator("#ase-owner").select_option("1")
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
        validate(context.new_page(), ASEMock(), args.url, args.screenshot, args.mobile_screenshot)
        browser.close()
    print("ASE browser: navigation, strict API contract, native file tokens, safe metadata/errors/sample/events, filters, U/V/P modes, preview invalidation, stale responses, polling/cancel, disconnect, storage and responsive widths passed.")


if __name__ == "__main__":
    main()

"""Exercise the SAP IQ browser contract without an IQ driver or server.

Run against the source server: .venv/bin/python scripts/browser_iq.py --url URL
"""
import argparse
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

PASSWORD = "iq-browser-secret-9!"
SQL = "WITH source AS (SELECT '한글' AS name) SELECT * FROM source"
DIRECTORY = r"C:\nice-tools"
CHOSEN = r"C:\exports"
HOSTILE = '<img src=x onerror="window.__iqXss=true">'


class IQMock:
    def __init__(self, available=True):
        self.available = available
        self.calls = []
        self.pending = []
        self.delay = set()
        self.cancelled = False
        self.terminal = None
        self.number = 0
        self.picker_cancelled = False
        self.poll_error = False
        self.connect_error = False
        self.start_error = False
        self.cancel_pending = False

    def handle(self, route):
        request = route.request
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[1]
        payload = request.post_data_json if request.method == "POST" else None
        self.calls.append((endpoint, payload))
        if payload is not None:
            assert request.headers.get("x-nice-tools") == "iq"
            assert request.headers.get("cache-control") == "no-store"
        if endpoint == "status":
            response = {"available": self.available, "module_available": True,
                        "drivers": ["SQL Anywhere 17"] if self.available else [],
                        "driver": "SQL Anywhere 17" if self.available else "",
                        "detail": "" if self.available else "IQ driver missing", "export_directory": DIRECTORY}
        elif endpoint == "connect":
            if self.connect_error:
                route.fulfill(status=400, json={"detail": HOSTILE})
                return
            self.number += 1
            response = {"session": f"session-{self.number}", "driver": "SQL Anywhere 17", "version": "SAP IQ 16.1", "export_directory": DIRECTORY}
        elif endpoint == "pick-directory":
            response = {"cancelled": self.picker_cancelled, "path": CHOSEN}
        elif endpoint == "jobs":
            if self.start_error:
                route.fulfill(status=400, json={"detail": "SELECT 한 문장만 허용합니다."})
                return
            self.cancelled = False
            response = {"job": "job-1"}
        elif endpoint in ("job", "cancel"):
            if endpoint == "cancel":
                self.cancelled = True
            state = "running" if self.cancelled and self.cancel_pending and endpoint == "cancel" else "cancelled" if self.cancelled else self.terminal or "running"
            response = {"job": "job-1", "state": state, "phase": "writing" if state == "running" else "done",
                        "rows": 1234567, "bytes_written": 5432100, "elapsed_seconds": 2.5,
                        "rows_per_second": 493826.8, "path": CHOSEN + r"\iq-result_1.txt" if state == "succeeded" else "",
                        "message": HOSTILE if state == "failed" else "mock " + state}
            if state == "succeeded":
                response["result"] = {**response, "columns": [HOSTILE, "name"]}
            if self.poll_error and endpoint == "job":
                route.fulfill(status=503, json={"detail": HOSTILE})
                return
        elif endpoint == "disconnect":
            response = {"ok": True}
        else:
            raise AssertionError(endpoint)
        if endpoint in self.delay:
            self.pending.append((route, response))
        else:
            route.fulfill(json=response)

    def finish(self):
        route, response = self.pending.pop(0)
        route.fulfill(json=response)

    def payload(self, endpoint):
        return [payload for name, payload in self.calls if name == endpoint][-1]


def open_tool(page, url, mock):
    page.route("**/api/iq/**", mock.handle)
    page.goto(url + ("#iq-tool" if url.endswith(".html") else "/#iq-tool"), wait_until="networkidle")
    expect(page.locator("#iq-tool")).to_be_visible()
    expect(page.locator("#current-tool")).to_have_text("SAP IQ 내보내기")


def connect(page):
    page.locator("#iq-host").fill("iq.example.internal")
    page.locator("#iq-user").fill("reader")
    page.locator("#iq-password").fill(PASSWORD)
    page.locator("#iq-connect").click()
    expect(page.locator("#iq-sql")).to_be_enabled()
    expect(page.locator("#iq-password")).to_have_value("")


def validate(page, url):
    mock = IQMock()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_tool(page, url, mock)
    expect(page.locator("#iq-port")).to_have_value("2638")
    expect(page.locator("#iq-driver")).to_have_value("SQL Anywhere 17")
    connect(page)
    assert mock.payload("connect") == {"host": "iq.example.internal", "port": 2638, "user": "reader", "password": PASSWORD,
                                        "database": "", "server_name": "", "driver": "SQL Anywhere 17"}
    expect(page.locator("#iq-connection-status")).to_contain_text("검증")
    expect(page.locator("#iq-sql-note")).to_contain_text("SELECT")
    expect(page.locator("#iq-sql-note")).to_contain_text("CTE")
    expect(page.locator("#iq-encoding")).to_have_value("euc-kr")
    expect(page.locator("#iq-field-terminator")).to_have_value("|")
    expect(page.locator("#iq-row-terminator")).to_have_value(r"\n")
    expect(page.locator("#iq-header")).not_to_be_checked()
    expect(page.locator("#iq-filename")).to_have_value("iq-result.txt")
    page.locator("#iq-sql").fill(SQL)
    mock.picker_cancelled = True
    page.locator("#iq-pick-directory").click()
    expect(page.locator("#iq-path")).to_have_text(DIRECTORY)
    mock.picker_cancelled = False
    page.locator("#iq-pick-directory").click()
    expect(page.locator("#iq-path")).to_have_text(CHOSEN)
    page.locator("#iq-start").click()
    expect(page.locator("#iq-job-status")).to_contain_text("mock running")
    assert mock.payload("jobs") == {"session": "session-1", "sql": SQL, "filename": "iq-result.txt", "options": {
        "encoding": "euc-kr", "field_terminator": "|", "row_terminator": r"\n", "include_header": False}}
    expect(page.locator("#iq-stat-rows")).to_contain_text("1,234,567")
    for name in ("sql", "filename", "encoding", "field-terminator", "row-terminator", "header", "pick-directory"):
        expect(page.locator("#iq-" + name)).to_be_disabled()
    # Hiding the tool must retain the job without affecting other tools.
    before = len(mock.calls)
    page.locator('[data-tool="jasypt-tool"]').click()
    expect(page.locator("#jasypt-tool")).to_be_visible()
    page.wait_for_timeout(900)
    assert len(mock.calls) > before
    page.locator('[data-tool="iq-tool"]').click()
    page.locator("#iq-cancel").click()
    expect(page.locator("#iq-job-status")).to_contain_text("mock cancelled")
    expect(page.locator("#iq-start")).to_be_enabled()
    for state in ("failed", "succeeded"):
        mock.terminal = state
        page.locator("#iq-start").click()
        expect(page.locator("#iq-job-status")).to_contain_text(HOSTILE if state == "failed" else "mock succeeded")
        expect(page.locator("#iq-start")).to_be_enabled()
        if state == "succeeded":
            expect(page.locator("#iq-result-path")).to_have_text(CHOSEN + r"\iq-result_1.txt")
            expect(page.locator("#iq-result-details")).to_contain_text(HOSTILE)
    assert page.locator("#iq-tool img, #iq-tool script").count() == 0
    assert not page.evaluate("window.__iqXss")
    assert not page.evaluate("Object.keys(localStorage).length || Object.keys(sessionStorage).length")
    for width in (390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
    page.locator("#iq-disconnect").click()
    expect(page.locator("#iq-sql")).to_be_disabled()
    expect(page.locator("#iq-result-path")).to_have_text("")
    assert not errors, errors


def validate_missing(page, url):
    open_tool(page, url, IQMock(available=False))
    expect(page.locator("#iq-connect")).to_be_disabled()
    expect(page.locator("#iq-runtime")).to_contain_text("64비트")
    expect(page.locator("#iq-runtime")).to_contain_text("SQL Anywhere")
    page.locator('[data-tool="jasypt-tool"]').click()
    expect(page.locator("#jasypt-submit")).to_be_enabled()


def validate_stale(page, url):
    mock = IQMock()
    open_tool(page, url, mock)
    connect(page)
    mock.delay.add("pick-directory")
    page.locator("#iq-pick-directory").click()
    page.wait_for_timeout(100)
    expect(page.locator("#iq-sql")).to_be_disabled()
    page.locator("#iq-disconnect").click()
    mock.finish()
    expect(page.locator("#iq-path")).to_have_text(DIRECTORY)
    expect(page.locator("#iq-sql")).to_be_disabled()
    mock.delay.clear()
    mock.delay.add("connect")
    page.locator("#iq-password").fill(PASSWORD)
    page.locator("#iq-connect").click()
    page.wait_for_timeout(100)
    page.locator("#iq-disconnect").click()
    mock.finish()
    expect(page.locator("#iq-password")).to_have_value("")
    page.wait_for_timeout(100)
    assert mock.payload("disconnect") == {"session": "session-2"}
    expect(page.locator("#iq-sql")).to_be_disabled()



def validate_failures(page, url):
    mock = IQMock()
    open_tool(page, url, mock)
    mock.connect_error = True
    page.locator("#iq-host").fill("iq.example.internal")
    page.locator("#iq-user").fill("reader")
    page.locator("#iq-password").fill(PASSWORD)
    page.locator("#iq-connect").click()
    expect(page.locator("#iq-feedback")).to_have_text(HOSTILE)
    expect(page.locator("#iq-password")).to_have_value("")
    expect(page.locator("#iq-connect")).to_be_enabled()
    mock.connect_error = False
    connect(page)
    page.locator("#iq-sql").fill("SELECT 1")
    # Reject paths locally; retain server authority over collision naming.
    page.locator("#iq-filename").fill(r"..\escape.txt")
    page.locator("#iq-start").click()
    expect(page.locator("#iq-feedback")).to_contain_text("파일 이름만")
    assert not any(name == "jobs" for name, _ in mock.calls)
    page.locator("#iq-filename").fill("custom.txt")
    page.locator("#iq-encoding").select_option("utf-8")
    page.locator("#iq-field-terminator").fill(r"\t")
    page.locator("#iq-row-terminator").fill(r"\r\n")
    page.locator("#iq-header").check()
    mock.start_error = True
    page.locator("#iq-start").click()
    expect(page.locator("#iq-feedback")).to_contain_text("SELECT 한 문장")
    expect(page.locator("#iq-start")).to_be_enabled()
    mock.start_error = False
    mock.delay.add("jobs")
    page.locator("#iq-start").click()
    page.wait_for_timeout(100)
    for name in ("sql", "filename", "encoding", "header", "pick-directory", "start"):
        expect(page.locator("#iq-" + name)).to_be_disabled()
    assert mock.payload("jobs")["options"] == {"encoding": "utf-8", "field_terminator": r"\t", "row_terminator": r"\r\n", "include_header": True}
    mock.finish()
    expect(page.locator("#iq-job-status")).to_contain_text("mock running")
    mock.delay.clear()
    mock.poll_error = True
    expect(page.locator("#iq-feedback")).to_contain_text("자동으로 다시 확인")
    expect(page.locator("#iq-cancel")).to_be_enabled()
    mock.poll_error = False
    mock.cancel_pending = True
    page.locator("#iq-cancel").click()
    expect(page.locator("#iq-job-status")).to_contain_text("mock cancelled")
    expect(page.locator("#iq-start")).to_be_enabled()
    expect(page.locator("#iq-feedback")).to_be_hidden()
    # An old poll must not restore results after disconnect/reconnect.
    mock.cancel_pending = False
    mock.delay.add("job")
    page.locator("#iq-start").click()
    page.wait_for_timeout(100)
    page.locator("#iq-disconnect").click()
    mock.finish()
    connect(page)
    expect(page.locator("#iq-job")).to_be_hidden()
    expect(page.locator("#iq-result-path")).to_have_text("")
    expect(page.locator("#iq-path")).to_have_text(DIRECTORY)
    assert not page.evaluate("window.__iqXss")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
        for validation in (validate, validate_missing, validate_stale, validate_failures):
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            validation(context.new_page(), args.url.rstrip("/"))
            context.close()
        browser.close()
    print("IQ browser: connection/default payloads, driver guidance, folder cancel/stale responses, job progress/cancel/terminal/XSS, secret clearing/storage, route isolation and 390/768/1440 layouts passed.")


if __name__ == "__main__":
    main()

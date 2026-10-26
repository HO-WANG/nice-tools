"""Exercise the native-folder comparison UI with an isolated mocked API.

Run against a running source server; no native dialogs or user files are accessed.
"""
import argparse
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright


SUMMARY = {"total": 3, "same": 1, "different": 2, "left_only": 0, "right_only": 0, "error": 0}
RECORDS = [{"path": "<img onerror=alert(1)>.txt", "status": "different", "reports": {"compared": {"state": "ready"}, "original": {"state": "ready"}}}, {"path": "same.txt", "status": "same"}, {"path": "large.txt", "status": "different", "reports": {"compared": {"state": "ready"}, "original": {"state": "ready"}}}]
RECORDS.append({"path": "unreadable.txt", "status": "error", "message": "텍스트 인코딩·권한을 확인하고 다시 비교하세요."})
SUMMARY.update(total=4, error=1)


# Context and hunk headings do not consume the 1,000-change preview budget.
LARGE_ROWS = [{"kind": "hunk", "text": "@@ -1,2000 +1,2000 @@"}]
for index in range(1000):
    if index % 4 == 0:
        LARGE_ROWS.append({"kind": "context", "old_line": index + 1, "new_line": index + 1, "text": f"shared {index}"})
    LARGE_ROWS.append({"kind": "add" if index % 2 else "delete", "new_line": index + 1, "old_line": index + 1, "text": f"large {index}"})
LARGE_PAGES = (len(LARGE_ROWS) + 199) // 200


def read_preview(page):
    """Count changes separately from headings/context over every cached page."""
    changes = rows = 0
    while True:
        expect(page.locator("#diff-panel")).to_have_attribute("aria-busy", "false")
        rows += page.locator("#diff-code tr").count()
        changes += page.locator("#diff-code tr.add, #diff-code tr.delete").count()
        if page.locator("#diff-next").is_disabled():
            break
        label = page.locator("#diff-page-label").inner_text()
        page.locator("#diff-next").click()
        expect(page.locator("#diff-page-label")).not_to_have_text(label)
    return changes, rows


def check_live_upload(browser, url):
    """Exercise real multipart-to-owned-worker reports on a non-Windows server."""
    with tempfile.TemporaryDirectory(prefix="nice-tools-browser-upload-") as directory:
        root = Path(directory)
        left, right = root / "before", root / "after"
        for folder, prefix in ((left, "old"), (right, "new")):
            folder.mkdir()
            (folder / "small.txt").write_text(prefix + " 한글\n", encoding="utf-8")
            for name, count in (("large.txt", 600), ("threshold.txt", 500)):
                (folder / name).write_text("\n".join(line for number in range(count) for line in (f"{prefix} {number}", f"shared {number} a", f"shared {number} b")), encoding="utf-8")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        requests, responses, errors = [], [], []
        page.on("request", lambda request: requests.append(request))
        page.on("response", lambda response: responses.append((response.url, response.json())) if "/api/comparison/" in response.url and response.ok else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="networkidle")
        expect(page.locator("#comparison-runtime")).to_contain_text("개발 환경")
        page.locator("#left-folder").set_input_files(str(left))
        page.locator("#right-folder").set_input_files(str(right))
        page.locator("#compare-button").click()
        expect(page.locator("#result-content")).to_be_visible(timeout=30000)
        expect(page.locator('[data-stat="different"]')).to_have_text("3")
        jobs = [data for response_url, data in responses if response_url.endswith("/job") and data.get("state") == "succeeded"]
        assert jobs
        for record in jobs[-1]["results"]:
            assert all(report["state"] == "ready" for report in record["reports"].values()), record
        expect(page.locator("#diff-analyze")).to_have_count(0)
        page.get_by_role("button", name="small.txt", exact=True).click()
        expect(page.locator("#diff-added")).to_have_text("+1 추가")
        expect(page.locator("#diff-deleted")).to_have_text("−1 삭제")
        page.get_by_role("button", name="large.txt", exact=True).click()
        expect(page.locator("#diff-added")).to_have_text("+600 추가")
        expect(page.locator("#diff-deleted")).to_have_text("−600 삭제")
        expect(page.locator("#diff-limit-note")).to_contain_text("전체 상세 보고서 기록 완료")
        expect(page.locator("#diff-limit-note")).to_contain_text("앞 1,000개 변경")
        expect(page.locator("#diff-limit-note")).to_contain_text("문맥 별도")
        assert read_preview(page)[0] == 1000
        large_pages = [data for response_url, data in responses if response_url.endswith("/diff") and data.get("path") == "large.txt"]
        assert len(large_pages) > 5
        assert sum(len(data["lines"]) for data in large_pages) > 1000
        for data in large_pages:
            assert data.get("state", "ready") == "ready" and data["truncated"] is True
            assert data["change_limit"] == data["displayed_changes"] == 1000
            assert data["full_total_lines"] > data["total_lines"]
            assert data["full_pages"] > data["pages"]
        page.get_by_role("button", name="threshold.txt", exact=True).click()
        expect(page.locator("#diff-added")).to_have_text("+500 추가")
        expect(page.locator("#diff-deleted")).to_have_text("−500 삭제")
        expect(page.locator("#diff-limit-note")).to_be_hidden()
        changes, rows = read_preview(page)
        assert changes == 1000 and rows > 1000
        threshold_pages = [data for response_url, data in responses if response_url.endswith("/diff") and data.get("path") == "threshold.txt"]
        assert all(data.get("state", "ready") == "ready" and data["truncated"] is False and data["displayed_changes"] == 1000 for data in threshold_pages)
        # Reopening and switching view both read the already-ready report.
        page.get_by_role("button", name="large.txt", exact=True).click()
        expect(page.locator("#diff-page-label")).to_contain_text("1 /")
        assert read_preview(page)[0] == 1000
        page.locator("#diff-mode").select_option("original")
        expect(page.locator("#diff-page-label")).to_contain_text("1 /")
        assert read_preview(page)[0] == 1000
        diff_requests = [request.post_data_json for request in requests if "/api/comparison/diff" in request.url]
        assert all(set(data) == {"session", "job", "path", "mode", "page"} for data in diff_requests)
        uploads = [request for request in requests if "/api/comparison/upload?" in request.url]
        assert len(uploads) == 1
        assert "multipart/form-data" in uploads[0].headers["content-type"]
        assert uploads[0].headers["x-nice-tools"] == "comparison"
        assert not any(request.url.endswith(("/api/compare", "/api/diff")) for request in requests)
        with page.expect_response("**/api/comparison/close"):
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        assert not errors, errors
        page.close()
    print("Live upload comparison: one upload, initially ready complete reports, exact full counts, 1,000-change previews with extra context pages, threshold boundary, reopen/view cache reads and cleanup passed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--live-upload", action="store_true", help="also test actual non-Windows upload/worker APIs")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        calls, errors, held = [], [], {}
        state = {"status_fail": True, "pick_cancel": False, "hold": None, "job_state": "succeeded", "diff_fail": False, "job_fail": False, "sequence": 0, "expired": False, "start_fail": False, "pick_network_fail": False}
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.comparisonFetches = [];
          const originalFetch = window.fetch;
          window.fetch = (url, options) => {
            if (String(url).startsWith('/api/comparison/')) window.comparisonFetches.push({url, ...options});
            return originalFetch(url, options);
          };""")

        def api(route):
            request = route.request
            endpoint = request.url.split("/api/comparison/")[1]
            data = request.post_data_json if request.method == "POST" else {}
            calls.append((endpoint, data, request.headers))
            assert request.headers.get("x-nice-tools") == "comparison"
            if request.method == "POST":
                assert "application/json" in request.headers.get("content-type", "")
            if state["pick_network_fail"] and endpoint == "pick-directory":
                state["pick_network_fail"] = False
                route.abort("failed")
                return
            if state["expired"] and endpoint == "pick-directory":
                state["expired"] = False
                route.fulfill(status=404, json={"detail": "세션이 만료되었습니다. 폴더를 다시 선택하세요.", "code": "session_expired"})
                return
            if state["start_fail"] and endpoint == "start":
                state["start_fail"] = False
                route.fulfill(status=503, json={"detail": "시작 응답 오류"})
                return
            if state["hold"] == endpoint:
                state["hold"] = None
                held[endpoint] = route
                return
            if endpoint == "status":
                if state["status_fail"]:
                    state["status_fail"] = False
                    route.fulfill(status=503, json={"detail": "실행창을 확인하세요"})
                    return
                result = {"native_available": True, "file_limit": 1000, "diff_page_size": 200}
            elif endpoint == "session":
                state["sequence"] += 1
                result = {"session": f"session-{state['sequence']}"}
            elif endpoint == "pick-directory":
                if state["pick_cancel"]:
                    state["pick_cancel"] = False
                    result = {"cancelled": True}
                else:
                    side = data["side"]
                    result = {"cancelled": False, "selection": {"token": side + "-token", "path": "C:\\private\\" + side, "name": side, "file_count": 2, "total_bytes": 1024}}
            elif endpoint == "start":
                assert set(data) == {"session", "left", "right", "options"}
                assert data["left"] == "left-token" and data["right"] == "right-token"
                result = {"job": "job-1"}
            elif endpoint in ("job", "cancel"):
                if endpoint == "job" and state["job_fail"]:
                    state["job_fail"] = False
                    route.fulfill(status=503, json={"detail": "잠시 후 다시 시도"})
                    return
                job_state = "cancelled" if endpoint == "cancel" else state["job_state"]
                result = {"job": data["job"], "state": job_state, "phase": "preparing-reports", "completed": 1, "total": 2, "path": "<img onerror=alert(1)>.txt", "elapsed_seconds": 1.25, "message": "실패 후 다시 시도" if job_state == "failed" else "", "summary": SUMMARY, "results": RECORDS}
                if job_state == "failed":
                    result.update(phase="done", failure_phase="preparing-reports", code="worker_exit", exit_code=3221225477)
            elif endpoint == "diff":
                assert set(data) == {"session", "job", "path", "mode", "page"}
                if state["diff_fail"]:
                    state["diff_fail"] = False
                    route.fulfill(status=400, json={"detail": "diff 재시도", "code": "report_missing"})
                    return
                number = data["page"]
                lines = [{"kind": "add", "new_line": number * 200 + i + 1, "text": f"<img onerror=alert(1)> {number * 200 + i}"} for i in range(200 if number < 2 else 1)]
                result = {"path": data["path"], "old_exists": True, "new_exists": True, "identical": data["path"] == "same.txt" and data["mode"] == "compared", "additions": 401, "deletions": 2, "total_lines": 401, "page": number, "page_size": 200, "pages": 3, "lines": lines}
                if result["identical"]:
                    result.update(lines=[], total_lines=0, pages=0)
                if data["path"] == "large.txt":
                    result.update(state="ready", additions=2000, deletions=1500, total_lines=len(LARGE_ROWS), pages=LARGE_PAGES,
                                  change_limit=1000, displayed_changes=1000, truncated=True, full_total_lines=4501, full_pages=23,
                                  lines=LARGE_ROWS[number * 200:(number + 1) * 200])
            elif endpoint == "close":
                result = {"ok": True}
            else:
                raise AssertionError(endpoint)
            route.fulfill(json=result)

        page.route("**/api/comparison/**", api)
        page.route("**/api/compare", lambda route: (_ for _ in ()).throw(AssertionError("native mode uploaded files")))
        page.route("**/api/diff", lambda route: (_ for _ in ()).throw(AssertionError("native mode uploaded diff")))
        page.goto(args.url, wait_until="networkidle")
        expect(page.locator("#comparison-runtime-retry")).to_be_visible()
        expect(page.locator("#left-pick-directory")).to_be_disabled()
        assert not any(name == "pick-directory" for name, _, _ in calls)
        page.locator("#comparison-runtime-retry").click()
        expect(page.locator("#left-pick-directory")).to_be_enabled()
        for side in ("left", "right"):
            page.locator(f"#{side}-pick-directory").click()
            expect(page.locator(f"#{side}-name")).to_have_text(side)
            expect(page.locator(f"#{side}-detail")).to_contain_text("C:")
        state["pick_cancel"] = True
        page.locator("#left-pick-directory").click()
        expect(page.locator("#left-pick-directory")).to_be_enabled()
        expect(page.locator("#left-name")).to_have_text("left")
        page.locator("#sort-lines").check()
        page.locator("#ignore-whitespace").check()
        page.locator("#ignore-start").fill("1")
        page.locator("#ignore-end").fill("2")
        page.locator("#compare-button").click()
        expect(page.locator("#result-content")).to_be_visible()
        assert [data for name, data, _ in calls if name == "start"][-1]["options"] == {"sort_lines": True, "ignore_whitespace": True, "ignore_start": 1, "ignore_end": 2}
        expect(page.locator("#result-body tr")).to_have_count(4)
        before_error_click = len([name for name, _, _ in calls if name == 'diff'])
        page.get_by_role('button', name='unreadable.txt', exact=True).click()
        expect(page.locator('#diff-message')).to_contain_text('텍스트 인코딩·권한')
        expect(page.locator('#diff-mode')).to_be_disabled()
        assert len([name for name, _, _ in calls if name == 'diff']) == before_error_click
        assert page.locator("#directory-tool img").count() == 0
        page.get_by_role("button", name=RECORDS[0]["path"], exact=True).click()
        expect(page.locator("#diff-code tr")).to_have_count(200)
        expect(page.locator("#diff-page-label")).to_contain_text("1 / 3")
        page.locator("#diff-next").click()
        expect(page.locator("#diff-page-label")).to_contain_text("2 / 3")
        page.locator("#diff-next").click()
        expect(page.locator("#diff-code tr")).to_have_count(1)
        expect(page.locator("#diff-page-label")).to_contain_text("3 / 3")
        page.locator("#diff-prev").click()
        expect(page.locator("#diff-page-label")).to_contain_text("2 / 3")
        page.locator("#diff-mode").select_option("original")
        expect(page.locator("#diff-page-label")).to_contain_text("1 / 3")
        assert [data for name, data, _ in calls if name == "diff"][-1]["mode"] == "original"
        expect(page.locator("#diff-note")).to_contain_text("원본")
        assert page.locator("#directory-tool img").count() == 0
        state["diff_fail"] = True
        page.get_by_role("button", name="same.txt", exact=True).click()
        expect(page.locator("#diff-retry")).to_be_visible()
        expect(page.locator("#diff-message")).to_contain_text('[report_missing]')
        page.locator("#diff-retry").click()
        expect(page.locator("#diff-code tr")).to_have_count(200)
        page.locator("#diff-mode").select_option("compared")
        expect(page.locator("#diff-message")).to_contain_text("차이가 없습니다")
        # Large reports are ready on the first read; only changes consume the cap.
        expect(page.locator("#diff-analyze")).to_have_count(0)
        page.get_by_role("button", name="large.txt", exact=True).click()
        expect(page.locator("#diff-page-label")).to_contain_text(f"1 / {LARGE_PAGES}")
        expect(page.locator("#diff-limit-note")).to_contain_text("전체 상세 보고서 기록 완료")
        expect(page.locator("#diff-limit-note")).to_contain_text("앞 1,000개 변경")
        expect(page.locator("#diff-limit-note")).to_contain_text("문맥 별도")
        expect(page.locator("#diff-added")).to_have_text("+2,000 추가")
        expect(page.locator("#diff-deleted")).to_have_text("−1,500 삭제")
        changes, rows = read_preview(page)
        assert changes == 1000 and rows == len(LARGE_ROWS) > 1000
        expect(page.locator("#diff-next")).to_be_disabled()
        page.get_by_role("button", name="same.txt", exact=True).click()
        expect(page.locator("#diff-limit-note")).to_be_hidden()
        page.get_by_role("button", name="large.txt", exact=True).click()
        expect(page.locator("#diff-page-label")).to_contain_text(f"1 / {LARGE_PAGES}")
        assert read_preview(page)[0] == 1000
        # Retry preserves the requested cached page and view.
        state["diff_fail"] = True
        page.locator("#diff-prev").click()
        expect(page.locator("#diff-retry")).to_be_visible()
        page.locator("#diff-retry").click()
        expect(page.locator("#diff-page-label")).to_contain_text(f"{LARGE_PAGES - 1} / {LARGE_PAGES}")
        retries = [data for name, data, _ in calls if name == "diff"][-2:]
        assert retries[0] == retries[1] and retries[0]["page"] == LARGE_PAGES - 2
        state["diff_fail"] = True
        page.locator("#diff-mode").select_option("original")
        expect(page.locator("#diff-retry")).to_be_visible()
        page.locator("#diff-retry").click()
        expect(page.locator("#diff-page-label")).to_contain_text(f"1 / {LARGE_PAGES}")
        assert [data for name, data, _ in calls if name == "diff"][-1]["mode"] == "original"
        # Switching modes ignores a delayed ready response from the previous view.
        state["hold"] = "diff"
        page.locator("#diff-mode").select_option("compared")
        expect(page.locator("#diff-panel")).to_have_attribute("aria-busy", "true")
        page.locator("#diff-mode").select_option("original")
        expect(page.locator("#diff-page-label")).to_contain_text(f"1 / {LARGE_PAGES}")
        held.pop("diff").fulfill(json={"state": "ready", "lines": [], "additions": 99, "deletions": 99, "total_lines": 0, "pages": 0})
        expect(page.locator("#diff-added")).to_have_text("+2,000 추가")
        expect(page.locator("#diff-note")).to_contain_text("원본")
        state["diff_fail"] = True
        page.get_by_role("button", name="same.txt", exact=True).click()
        expect(page.locator("#diff-retry")).to_be_visible()
        page.locator("#diff-mode").select_option("compared")
        expect(page.locator("#diff-message")).to_contain_text("차이가 없습니다")
        assert all(set(data) == {"session", "job", "path", "mode", "page"} for name, data, _ in calls if name == "diff")
        state["hold"] = "diff"
        page.get_by_role("button", name=RECORDS[0]["path"], exact=True).click()
        expect(page.locator("#diff-panel")).to_have_attribute("aria-busy", "true")
        page.locator("#ignore-start").fill("3")
        held.pop("diff").fulfill(json={"lines": [], "identical": True})
        expect(page.locator("#result-content")).to_be_hidden()
        expect(page.locator("#diff-path")).to_be_hidden()
        state["job_state"] = "running"
        state["job_fail"] = True
        page.locator("#compare-button").click()
        expect(page.locator("#comparison-job-retry")).to_be_visible()
        expect(page.locator("#ignore-start")).to_be_disabled()
        page.locator("#comparison-job-retry").click()
        expect(page.locator("#comparison-job-status")).to_contain_text("1 / 2")
        expect(page.locator("#comparison-job-status")).to_contain_text("상세 내용 준비")
        expect(page.locator("#left-pick-directory")).to_be_disabled()
        page.locator("#comparison-cancel").click()
        expect(page.locator("#feedback")).to_contain_text("취소")
        expect(page.locator("#compare-button")).to_be_enabled()
        state["job_state"] = "failed"
        page.locator("#compare-button").click()
        expect(page.locator("#feedback")).to_contain_text("실패 후 다시 시도")
        expect(page.locator("#feedback")).to_contain_text("[worker_exit]")
        expect(page.locator("#feedback")).to_contain_text("exit_code=3221225477")
        expect(page.locator("#comparison-job-status")).to_contain_text("상세 내용 준비")
        expect(page.locator("#ignore-start")).to_be_enabled()
        # A delayed job status must not resurrect results after cancellation.
        state["job_state"] = "running"
        state["hold"] = "job"
        page.locator("#compare-button").click()
        expect(page.locator("#comparison-cancel")).to_be_visible()
        page.locator("#comparison-cancel").click()
        expect(page.locator("#compare-button")).to_be_enabled()
        held.pop("job").fulfill(json={"job": "job-1", "state": "succeeded", "summary": SUMMARY, "results": RECORDS})
        expect(page.locator("#result-content")).to_be_hidden()
        for width in (390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        page.locator("#sidebar-toggle").click()
        expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "false")
        # Expired sessions discard both stale directory tokens and allow a fresh picker.
        state["expired"] = True
        page.locator("#left-pick-directory").click()
        expect(page.locator("#feedback")).to_contain_text("만료")
        expect(page.locator("#compare-button")).to_be_disabled()
        expect(page.locator("#left-name")).to_have_text("폴더를 선택하세요")
        for side in ("left", "right"):
            page.locator(f"#{side}-pick-directory").click()
            expect(page.locator(f"#{side}-name")).to_have_text(side)
        # A disconnected picker is closed before allowing another native dialog.
        state["pick_network_fail"] = True
        with page.expect_response("**/api/comparison/close"):
            page.locator("#left-pick-directory").click()
        expect(page.locator("#compare-button")).to_be_disabled()
        expect(page.locator("#left-name")).to_have_text("폴더를 선택하세요")
        for side in ("left", "right"):
            page.locator(f"#{side}-pick-directory").click()
            expect(page.locator(f"#{side}-name")).to_have_text(side)
        # A lost start response may have created a job: close owns server cleanup.
        state["start_fail"] = True
        with page.expect_response("**/api/comparison/close"):
            page.locator("#compare-button").click()
        expect(page.locator("#feedback")).to_contain_text("폴더를 다시 선택")
        expect(page.locator("#compare-button")).to_be_disabled()
        for side in ("left", "right"):
            page.locator(f"#{side}-pick-directory").click()
            expect(page.locator(f"#{side}-name")).to_have_text(side)
        # Closing while a picker is in flight retires its session and ignores its reply.
        state["hold"] = "pick-directory"
        page.locator("#left-pick-directory").click()
        expect(page.locator("#sort-lines")).to_be_disabled()
        with page.expect_response("**/api/comparison/close"):
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#left-pick-directory")).to_be_enabled()
        page.locator("#left-pick-directory").click()
        expect(page.locator("#left-name")).to_have_text("left")
        with page.expect_response("**/api/comparison/close"):
            held.pop("pick-directory").fulfill(json={"cancelled": False, "selection": {"token": "stale", "name": "stale", "path": "C:/stale", "file_count": 0, "total_bytes": 0}})
        expect(page.locator("#left-name")).to_have_text("left")
        page.locator("#right-pick-directory").click()
        expect(page.locator("#right-name")).to_have_text("right")
        # A late start reply is explicitly cancelled and closed after navigation.
        state["hold"] = "start"
        page.locator("#compare-button").click()
        expect(page.locator("#comparison-job-status")).to_contain_text("준비")
        with page.expect_response("**/api/comparison/close"):
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        with page.expect_response("**/api/comparison/cancel"):
            held.pop("start").fulfill(json={"job": "stale-job"})
        expect(page.locator("#result-content")).to_be_hidden()
        # A late session creation reply must also be closed, even without a picker.
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#left-pick-directory")).to_be_enabled()
        state["hold"] = "session"
        page.locator("#left-pick-directory").click()
        expect(page.locator("#sort-lines")).to_be_disabled()
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        with page.expect_response("**/api/comparison/close"):
            held.pop("session").fulfill(json={"session": "stale-session"})
        assert any(name == "close" and data["session"] == "stale-session" for name, data, _ in calls)
        for request in page.evaluate("window.comparisonFetches"):
            assert request["credentials"] == "same-origin"
            assert request["cache"] == "no-store"
        assert not errors, errors
        assert all("multipart" not in headers.get("content-type", "") for _, _, headers in calls)
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        if args.live_upload:
            check_live_upload(browser, args.url)
        browser.close()
    print("Native comparison UI: initially ready large reports, context-aware 1,000-change previews, full counts, cached page/view retry, stale ready responses, runtime retry, native tokens, options, safe rendering, server pagination, stale diff/job/picker/start/session, progress, cancellation, failures, layout and cleanup passed.")


if __name__ == "__main__":
    main()

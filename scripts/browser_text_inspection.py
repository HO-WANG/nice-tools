"""Mocked browser checks for the read-only hidden-character inspection UI.

Run against a source server. Native dialogs and real user files are never opened.
"""
import argparse
from playwright.sync_api import expect, sync_playwright


SUMMARY = {
    "encoding": "utf-8", "bom": "UTF-8", "file_size": 1024,
    "characters": 900, "lines": 4, "findings": 201,
    "kinds": {"zero_width": 200, "bidi": 1},
    "common_whitespace": {"space": 10, "tab": 2, "crlf": 1, "lf": 2, "cr": 0},
}
ROW = {"line": 2, "column": 3, "codepoint": "U+200B", "name": "ZERO WIDTH SPACE",
       "kind": "zero_width", "label": "폭 없는 공백", "before": '<img src=x onerror="alert(1)">\u202eabc',
       "display": "⟦U+200B⟧", "after": "def<script>alert(2)</script>\u2066"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        calls, errors, held = [], [], {}
        state = {"native": True, "status_fail": True, "pick_cancel": False, "hold": None,
                 "job_state": "succeeded", "page_fail": False, "poll_fail": False, "sequence": 0,
                 "expired": False, "start_fail": False, "pick_network_fail": False, "empty": False, "strict": False}
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.inspectionFetches = [];
          const originalFetch = window.fetch;
          window.fetch = (url, options) => {
            if (String(url).startsWith('/api/text-inspection/')) window.inspectionFetches.push({url, credentials: options.credentials, cache: options.cache});
            return originalFetch(url, options);
          };""")

        def api(route):
            request = route.request
            endpoint = request.url.split("/api/text-inspection/")[1]
            upload = endpoint == "upload"
            data = request.post_data_json if request.method == "POST" and not upload else {}
            calls.append((endpoint, data, request.headers))
            assert request.headers.get("x-nice-tools") == "text-inspection"
            if request.method == "POST" and not upload:
                assert "application/json" in request.headers.get("content-type", "")
            if state["pick_network_fail"] and endpoint == "pick-file":
                state["pick_network_fail"] = False
                route.abort("failed")
                return
            if state["expired"] and endpoint == "pick-file":
                state["expired"] = False
                route.fulfill(status=404, json={"detail": "세션이 만료되었습니다. 파일을 다시 선택하세요.", "code": "session_expired"})
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
                result = {"native_available": state["native"], "page_size": 200}
            elif endpoint == "session":
                state["sequence"] += 1
                result = {"session": f"session-{state['sequence']}"}
            elif endpoint in ("pick-file", "upload"):
                if upload:
                    assert not state["native"], "native mode must never upload"
                    assert "multipart/form-data" in request.headers.get("content-type", "")
                    assert b'name="session"' in request.post_data_buffer
                    assert b'name="file"' in request.post_data_buffer
                    assert b"uploaded text" in request.post_data_buffer
                if state["pick_cancel"]:
                    state["pick_cancel"] = False
                    result = {"cancelled": True}
                else:
                    result = {"cancelled": False, "selection": {"token": "file-token", "path": "C:\\private\\sample.txt", "name": "sample.txt", "size": 1024}}
            elif endpoint == "start":
                assert set(data) == {"session", "file", "options"}
                assert data["file"] == "file-token"
                assert type(data["options"]["strict_mode"]) is bool
                if data["options"]["encoding"] == "unknown_codec_private_marker":
                    route.fulfill(status=400, json={"detail": "지원하지 않는 인코딩입니다. 인코딩 이름을 확인하세요.", "code": "encoding"})
                    return
                state["strict"] = data["options"]["strict_mode"]
                result = {"job": "job-1"}
            elif endpoint in ("job", "cancel"):
                if endpoint == "job" and state["poll_fail"]:
                    state["poll_fail"] = False
                    route.fulfill(status=503, json={"detail": "상태 조회 재시도"})
                    return
                job_state = "cancelled" if endpoint == "cancel" else state["job_state"]
                result = {"job": data["job"], "state": job_state, "phase": "done" if job_state == "succeeded" else "scanning", "completed": 512, "total": 1024, "elapsed_seconds": 1.25,
                          "message": "인코딩을 확인하세요" if job_state == "failed" else "", "summary": SUMMARY}
            elif endpoint == "page":
                if state["page_fail"]:
                    state["page_fail"] = False
                    route.fulfill(status=503, json={"detail": "결과 조회 재시도"})
                    return
                number = data["page"]
                summary = dict(SUMMARY)
                if state["strict"]:
                    summary["kinds"] = {"strict": 201}
                if state["empty"]:
                    summary.update(findings=0, kinds={})
                template = dict(ROW, kind="strict", codepoint="U+4E2D", name="CJK UNIFIED IDEOGRAPH-4E2D", display="⟦U+4E2D⟧") if state["strict"] else ROW
                result = {"summary": summary, "page": number, "page_size": 200,
                          "pages": 1 if state["empty"] else 2, "total": summary["findings"],
                          "results": [] if state["empty"] else [dict(template, line=number * 200 + i + 1) for i in range(200 if number == 0 else 1)]}
            elif endpoint == "close":
                result = {"ok": True}
            else:
                raise AssertionError(endpoint)
            route.fulfill(json=result)

        page.route("**/api/text-inspection/**", api)
        page.goto(args.url, wait_until="networkidle")
        # Loading and navigating existing tools does not initialize this API.
        assert not calls
        for tool in ("jasypt-tool", "ase-tool", "iq-tool", "directory-tool"):
            page.locator(f'[data-tool="{tool}"]').click()
            expect(page.locator(f"#{tool}")).to_be_visible()
        assert not calls
        page.locator('[data-tool="text-inspection-tool"]').click()
        expect(page.locator("#text-inspection-runtime-retry")).to_be_visible()
        expect(page.locator("#text-inspection-pick")).to_be_disabled()
        assert not any(name in ("pick-file", "upload") for name, _, _ in calls)
        page.locator("#text-inspection-runtime-retry").click()
        expect(page.locator("#text-inspection-pick")).to_be_enabled()
        expect(page.locator("#text-inspection-auto")).to_be_checked()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        expect(page.locator("#text-inspection-encoding")).to_have_attribute("maxlength", "128")
        expect(page.locator("#text-inspection-common")).not_to_be_checked()
        expect(page.locator("#text-inspection-strict")).not_to_be_checked()
        strict_label = page.locator("label", has=page.locator("#text-inspection-strict"))
        strict_label.hover()
        tooltip = strict_label.get_attribute("title")
        for text in ("ASCII", "일반 공백", "한글 음절·자모", "탭", "CR/LF", "한자·이모지·전각", "채움 문자"):
            assert text in tooltip, text
        expect(page.locator("#text-inspection-strict")).to_have_attribute("aria-describedby", "text-inspection-strict-note")
        expect(page.locator("#text-inspection-strict-note")).to_contain_text("반드시 오류는 아닙니다")
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        expect(page.locator("#text-inspection-detail")).to_contain_text("C:")
        state["pick_cancel"] = True
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-pick")).to_be_enabled()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(200)
        assert [data for name, data, _ in calls if name == "start"][-1]["options"] == {"encoding": "auto", "include_common_whitespace": False, "strict_mode": False}
        expect(page.locator("#text-inspection-page-label")).to_contain_text("1 / 2")
        expect(page.locator("#text-inspection-summary")).to_contain_text("UTF-8")
        expect(page.locator("#text-inspection-summary")).to_contain_text("선두 BOM")
        expect(page.locator("#text-inspection-whitespace")).to_contain_text("CRLF 1")
        expect(page.locator("#text-inspection-kinds")).to_contain_text("폭 없는 문자 200")
        assert page.locator("#text-inspection-tool img, #text-inspection-tool script").count() == 0
        context = page.locator(".text-inspection-context").first
        expect(context).to_contain_text("<img")
        expect(context).to_contain_text("[U+202E]")
        expect(context).to_contain_text("[U+2066]")
        expect(context.locator("mark")).to_have_text("⟦U+200B⟧")
        assert "\u202e" not in context.text_content()
        assert context.evaluate("e => getComputedStyle(e).unicodeBidi") == "isolate"
        page.locator("#text-inspection-next").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(1)
        expect(page.locator("#text-inspection-page-label")).to_contain_text("2 / 2")
        page.locator("#text-inspection-prev").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(200)
        state["page_fail"] = True
        page.locator("#text-inspection-next").click()
        expect(page.locator("#text-inspection-page-retry")).to_be_visible()
        page.locator("#text-inspection-page-retry").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(1)
        # Input changes discard results and ignore a delayed page response.
        state["hold"] = "page"
        page.locator("#text-inspection-prev").click()
        expect(page.locator("#text-inspection-results")).to_have_attribute("aria-busy", "true")
        page.locator("#text-inspection-auto").uncheck()
        expect(page.locator("#text-inspection-encoding")).to_be_enabled()
        page.locator("#text-inspection-encoding").fill(" CP1251 ")
        held.pop("page").fulfill(json={"summary": SUMMARY, "page": 0, "pages": 2, "results": [ROW], "total": 201})
        expect(page.locator("#text-inspection-results")).to_be_hidden()
        # Free codec names and aliases are editable, blank input gets a static local error,
        # and an unknown codec gets a static server error without losing the file.
        start_count = sum(name == "start" for name, _, _ in calls)
        page.locator("#text-inspection-encoding").fill("   ")
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("인코딩 이름을 확인")
        assert sum(name == "start" for name, _, _ in calls) == start_count
        page.locator("#text-inspection-encoding").fill("unknown_codec_private_marker")
        close_count = sum(name == "close" for name, _, _ in calls)
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("지원하지 않는 인코딩")
        expect(page.locator("#text-inspection-feedback")).not_to_contain_text("unknown_codec_private_marker")
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        expect(page.locator("#text-inspection-encoding")).to_be_enabled()
        assert sum(name == "close" for name, _, _ in calls) == close_count
        page.locator("#text-inspection-encoding").fill(" CP1251 ")
        page.locator("#text-inspection-strict").check()
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(200)
        expect(page.locator("#text-inspection-kinds")).to_contain_text("엄격 모드 허용 외 문자 201")
        expect(page.locator("#text-inspection-rows tr").first).to_contain_text("엄격 모드 허용 외 문자")
        assert [data for name, data, _ in calls if name == "start"][-1]["options"] == {"encoding": "CP1251", "include_common_whitespace": False, "strict_mode": True}
        # Strict changes and auto/manual mode changes invalidate prior results.
        page.locator("#text-inspection-strict").uncheck()
        expect(page.locator("#text-inspection-results")).to_be_hidden()
        page.locator("#text-inspection-strict").check()
        page.locator("#text-inspection-common").check()
        state["empty"] = True
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-empty")).to_contain_text("확인 대상 문자가 없습니다")
        expect(page.locator("#text-inspection-next")).to_be_disabled()
        assert [data for name, data, _ in calls if name == "start"][-1]["options"] == {"encoding": "CP1251", "include_common_whitespace": True, "strict_mode": True}
        state["empty"] = False
        state["job_state"] = "running"
        state["poll_fail"] = True
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-job-retry")).to_be_visible()
        expect(page.locator("#text-inspection-pick")).to_be_disabled()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        expect(page.locator("#text-inspection-common")).to_be_disabled()
        expect(page.locator("#text-inspection-auto")).to_be_disabled()
        expect(page.locator("#text-inspection-strict")).to_be_disabled()
        page.locator("#text-inspection-job-retry").click()
        expect(page.locator("#text-inspection-job-status")).to_contain_text("512 / 1,024 bytes")
        page.locator("#text-inspection-cancel").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("취소")
        expect(page.locator("#text-inspection-scan")).to_be_enabled()
        state["job_state"] = "failed"
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("인코딩을 확인")
        expect(page.locator("#text-inspection-encoding")).to_be_enabled()
        # Cancellation supersedes a job response that arrives afterwards.
        state["job_state"] = "running"
        state["hold"] = "job"
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-cancel")).to_be_visible()
        page.locator("#text-inspection-cancel").click()
        expect(page.locator("#text-inspection-scan")).to_be_enabled()
        held.pop("job").fulfill(json={"job": "job-1", "state": "succeeded", "phase": "done", "summary": SUMMARY})
        expect(page.locator("#text-inspection-results")).to_be_hidden()
        for width in (390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        state["expired"] = True
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("만료")
        expect(page.locator("#text-inspection-scan")).to_be_disabled()
        expect(page.locator("#text-inspection-name")).to_have_text("파일을 선택하세요")
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        state["pick_network_fail"] = True
        with page.expect_response("**/api/text-inspection/close"):
            page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-scan")).to_be_disabled()
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        state["start_fail"] = True
        with page.expect_response("**/api/text-inspection/close"):
            page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-feedback")).to_contain_text("파일을 다시 선택")
        expect(page.locator("#text-inspection-scan")).to_be_disabled()
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        # Closing while a picker is open retires its session; late replies are ignored.
        state["hold"] = "pick-file"
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        with page.expect_response("**/api/text-inspection/close"):
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#text-inspection-pick")).to_be_enabled()
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        with page.expect_response("**/api/text-inspection/close"):
            held.pop("pick-file").fulfill(json={"cancelled": False, "selection": {"token": "stale-token", "name": "stale"}})
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        state["hold"] = "start"
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        with page.expect_response("**/api/text-inspection/cancel"):
            held.pop("start").fulfill(json={"job": "stale-job"})
        assert any(name == "cancel" and data["job"] == "stale-job" for name, data, _ in calls)
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#text-inspection-pick")).to_be_enabled()
        state["hold"] = "session"
        page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        with page.expect_response("**/api/text-inspection/close"):
            held.pop("session").fulfill(json={"session": "stale-session"})
        assert any(name == "close" and data["session"] == "stale-session" for name, data, _ in calls)
        # Native requests are JSON only, without file content, multipart, or filesystem paths.
        assert all("multipart" not in headers.get("content-type", "") for _, _, headers in calls)
        for name, data, _ in calls:
            assert "C:" not in str(data), name
        assert not any(name == "upload" for name, _, _ in calls)
        # Only explicit status=false enables the development single-file upload fallback.
        state["native"] = False
        state["job_state"] = "succeeded"
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator("#text-inspection-pick")).to_be_enabled()
        with page.expect_file_chooser() as chooser:
            page.locator("#text-inspection-pick").click()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        chooser.value.set_files({"name": "fallback.txt", "mimeType": "text/plain", "buffer": b"uploaded text"})
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        assert any(name == "upload" for name, _, _ in calls)
        page.locator("#text-inspection-auto").check()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        page.locator("#text-inspection-scan").click()
        expect(page.locator("#text-inspection-rows tr")).to_have_count(200)
        assert [data for name, data, _ in calls if name == "start"][-1]["options"]["encoding"] == "auto"
        for width in (390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert page.locator(".text-inspection-scroll").evaluate("e => e.scrollWidth >= e.clientWidth")
        # Browser cancellation retains the prior selection and releases the options.
        with page.expect_file_chooser():
            page.locator("#text-inspection-pick").click()
        page.locator("#text-inspection-file").dispatch_event("cancel")
        expect(page.locator("#text-inspection-name")).to_have_text("sample.txt")
        expect(page.locator("#text-inspection-auto")).to_be_enabled()
        expect(page.locator("#text-inspection-strict")).to_be_enabled()
        expect(page.locator("#text-inspection-encoding")).to_be_disabled()
        for request in page.evaluate("window.inspectionFetches"):
            assert request["credentials"] == "same-origin"
            assert request["cache"] == "no-store"
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert not errors, errors
        browser.close()
    print("Text inspection UI passed: isolated routing, native tokens/no uploads, explicit upload fallback, strict tooltip/payload/kinds, auto/free codec input and static errors, options, BOM/whitespace summary, safe XSS/bidi context, pagination, empty results, retry, cancel, stale job/page/picker/start/session, cleanup and 390/768/1440 layouts.")


if __name__ == "__main__":
    main()

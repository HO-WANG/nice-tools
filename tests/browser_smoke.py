"""실행 중인 서버의 실제 폴더 선택과 화면 동작을 Chromium으로 확인."""

import argparse
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="nice-tools-browser-") as directory:
        root = Path(directory)
        left, right, large, too_many = root / "before", root / "after", root / "large-files", root / "many-files"
        for folder in (left, right, large, too_many):
            folder.mkdir()
        for folder in (left, right):
            (folder / "sub").mkdir()
            (folder / "sub/same.txt").write_text("같은 내용\n", encoding="utf-8")
            (folder / "<img onerror=alert(1)>.txt").write_text("plain text", encoding="utf-8")
        (left / "changed.txt").write_text("Xa bY\nX c dY", encoding="utf-8")
        (right / "changed.txt").write_text("Qc dR\nQabR", encoding="utf-8")
        (left / "left-only.txt").write_text('<img onerror=alert(1)>', encoding="utf-8")
        (right / "right-only.txt").write_text("right", encoding="utf-8")
        (large / "large.txt").write_bytes(b"a" * (10 * 1024 * 1024 + 1))
        for i in range(1000):
            (too_many / f"file{i}.txt").write_bytes(b"a")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(args.url, wait_until="networkidle")
            expect(page.get_by_role("heading", name="디렉토리 비교", exact=True)).to_be_visible()
            page.locator("#left-folder").set_input_files(str(left))
            page.locator("#right-folder").set_input_files(str(right))
            page.locator("#compare-button").click()
            expect(page.locator('[data-stat="total"]')).to_have_text("5")
            expect(page.locator('[data-stat="same"]')).to_have_text("2")
            expect(page.locator('[data-stat="different"]')).to_have_text("1")
            expect(page.locator("#result-body tr")).to_have_count(5)
            assert page.locator("#result-body img").count() == 0
            expect(page.locator("#result-body")).to_contain_text("<img onerror=alert(1)>.txt")
            page.locator("#status-filter").select_option("different")
            expect(page.locator("#result-body tr")).to_have_count(1)
            page.get_by_role("button", name="changed.txt", exact=True).click()
            expect(page.locator("#diff-path")).to_have_text("changed.txt")
            expect(page.locator("#diff-code .delete")).to_have_count(2)
            expect(page.locator("#diff-code .add")).to_have_count(2)
            expect(page.locator("#diff-code")).to_contain_text("Xa bY")
            expect(page.get_by_role("button", name="changed.txt", exact=True)).to_be_focused()
            page.locator("#status-filter").select_option("all")
            page.get_by_role("button", name="left-only.txt", exact=True).click()
            expect(page.locator("#diff-code .delete")).to_have_count(1)
            expect(page.locator("#diff-code")).to_contain_text('<img onerror=alert(1)>')
            assert page.locator("#diff-code img").count() == 0
            page.get_by_role("button", name="<img onerror=alert(1)>.txt", exact=True).click()
            expect(page.locator("#diff-message")).to_contain_text("차이가 없습니다")
            assert page.locator("#diff-panel img").count() == 0
            page.locator("#sort-lines").check()
            page.locator("#ignore-whitespace").check()
            page.locator("#ignore-start").fill("1")
            page.locator("#ignore-end").fill("1")
            page.locator("#compare-button").click()
            expect(page.locator('[data-stat="same"]')).to_have_text("3")
            expect(page.locator('[data-stat="different"]')).to_have_text("0")
            expect(page.locator("#result-body tr")).to_have_count(5)
            page.get_by_role("button", name="changed.txt", exact=True).click()
            expect(page.locator("#diff-message")).to_contain_text("차이가 없습니다")
            page.locator("#diff-mode").select_option("original")
            expect(page.locator("#diff-code .delete")).to_have_count(2)
            expect(page.locator("#diff-note")).to_contain_text("원본")
            page.locator("#diff-mode").select_option("compared")
            expect(page.locator("#diff-message")).to_contain_text("차이가 없습니다")
            page.locator("#sidebar-toggle").click()
            expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "false")
            page.locator("#sidebar-toggle").click()
            expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "true")
            (right / "large.txt").write_bytes((large / "large.txt").read_bytes())
            page.locator("#left-folder").set_input_files(str(large))
            page.locator("#right-folder").set_input_files(str(right))
            page.locator("#compare-button").click()
            expect(page.locator("#result-content")).to_be_visible(timeout=20000)
            expect(page.locator('[data-stat="same"]')).to_have_text("1")
            expect(page.locator('[data-stat="right_only"]')).to_have_text("4")
            (right / "large.txt").unlink()
            page.locator("#right-folder").set_input_files(str(right))
            page.locator("#left-folder").set_input_files(str(too_many))
            page.locator("#compare-button").click()
            expect(page.locator("#feedback")).to_contain_text("1,000")
            page.locator("#left-folder").set_input_files(str(left))
            page.locator("#compare-button").click()
            expect(page.locator("#result-content")).to_be_visible()
            expect(page.locator('[data-stat="same"]')).to_have_text("3")
            page.get_by_role("button", name="changed.txt", exact=True).click()
            page.locator("#diff-mode").select_option("original")
            expect(page.locator("#diff-code .delete")).to_have_count(2)
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#compare-button")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1000})
            # A long diff remains complete while the browser renders one page at a time.
            long_left, long_right = root / "long-before", root / "long-after"
            for folder, prefix in ((long_left, "old"), (long_right, "new")):
                folder.mkdir()
                (folder / "long.txt").write_text("\n".join(f"{prefix} {i}" for i in range(225)), encoding="utf-8")
            page.locator("#reset-options").click()
            page.locator("#left-folder").set_input_files(str(long_left))
            page.locator("#right-folder").set_input_files(str(long_right))
            page.locator("#compare-button").click()
            expect(page.locator('[data-stat="different"]')).to_have_text("1")
            page.get_by_role("button", name="long.txt", exact=True).click()
            expect(page.locator("#diff-code tr")).to_have_count(200)
            expect(page.locator("#diff-page-label")).to_contain_text("1 / 3")
            page.locator("#diff-next").click()
            expect(page.locator("#diff-page-label")).to_contain_text("2 / 3")
            page.locator("#diff-next").click()
            expect(page.locator("#diff-code")).to_contain_text("new 224")
            expect(page.locator("#diff-next")).to_be_disabled()
            page.locator("#diff-prev").click()
            expect(page.locator("#diff-page-label")).to_contain_text("2 / 3")
            # Request errors can be retried without losing the file selection.
            page.route("**/api/diff", lambda route: route.fulfill(status=500, json={"detail": "검증용 요청 오류"}), times=1)
            page.get_by_role("button", name="long.txt", exact=True).click()
            expect(page.locator("#diff-message")).to_have_text("검증용 요청 오류")
            page.locator("#diff-retry").click()
            expect(page.locator("#diff-code tr")).to_have_count(200)
            # Discard a response if options were edited while it was in flight.
            delayed = []
            page.route("**/api/diff", lambda route: delayed.append(route), times=1)
            page.get_by_role("button", name="long.txt", exact=True).click()
            expect(page.locator("#diff-panel")).to_have_attribute("aria-busy", "true")
            page.locator("#ignore-start").fill("1")
            assert delayed
            delayed[0].fulfill(status=200, json={"lines": [], "additions": 0, "deletions": 0, "identical": True})
            expect(page.locator("#result-content")).to_be_hidden()
            expect(page.locator("#diff-path")).to_be_hidden()
            assert not errors, errors
            assert all(urlsplit(url).netloc == urlsplit(args.url).netloc for url in requests), requests
            browser.close()
    print("Browser smoke: folders, options, large files, diff modes, pagination, retry, stale response, focus, safe content, filter, sidebar, mobile and offline assets passed.")


if __name__ == "__main__":
    main()

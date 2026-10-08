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
        left, right, oversized = root / "before", root / "after", root / "too-large"
        for folder in (left, right, oversized):
            folder.mkdir()
        for folder in (left, right):
            (folder / "sub").mkdir()
            (folder / "sub/same.txt").write_text("같은 내용\n", encoding="utf-8")
            (folder / "<img onerror=alert(1)>.txt").write_text("plain text", encoding="utf-8")
        (left / "changed.txt").write_text("Xa bY\nX c dY", encoding="utf-8")
        (right / "changed.txt").write_text("Qc dR\nQabR", encoding="utf-8")
        (left / "left-only.txt").write_text("left", encoding="utf-8")
        (right / "right-only.txt").write_text("right", encoding="utf-8")
        (oversized / "large.txt").write_bytes(b"a" * (10 * 1024 * 1024 + 1))
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
            page.locator("#sort-lines").check()
            page.locator("#ignore-whitespace").check()
            page.locator("#ignore-start").fill("1")
            page.locator("#ignore-end").fill("1")
            page.locator("#compare-button").click()
            expect(page.locator('[data-stat="same"]')).to_have_text("3")
            expect(page.locator('[data-stat="different"]')).to_have_text("0")
            expect(page.locator("#result-body tr")).to_have_count(5)
            page.locator("#sidebar-toggle").click()
            expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "false")
            page.locator("#sidebar-toggle").click()
            expect(page.locator("#sidebar-toggle")).to_have_attribute("aria-expanded", "true")
            page.locator("#left-folder").set_input_files(str(oversized))
            page.locator("#compare-button").click()
            expect(page.locator("#feedback")).to_contain_text("10 MiB")
            page.locator("#left-folder").set_input_files(str(left))
            page.locator("#compare-button").click()
            expect(page.locator("#result-content")).to_be_visible()
            expect(page.locator('[data-stat="same"]')).to_have_text("3")
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#compare-button")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors, errors
            assert all(urlsplit(url).netloc == urlsplit(args.url).netloc for url in requests), requests
            browser.close()
    print("Browser smoke: folder selection, 4 options, results, filter, sidebar, retry, filename escaping, mobile and offline assets passed.")


if __name__ == "__main__":
    main()

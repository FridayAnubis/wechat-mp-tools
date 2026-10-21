"""Run inside a disposable container/volume; never against a user's data.

Only the upstream WeRead article-list response is mocked. Article HTTP download,
browser rendering, PDF/Markdown export, RSS storage, API and feed links are real.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

BASE = "http://127.0.0.1:5200"
ACCOUNT = "Docker测试公众号"
FAKEID = "docker-smoke-test"


def get(path):
    response = requests.get(BASE + path, timeout=15)
    response.raise_for_status()
    return response


def verify():
    assert get("/api/health").json() == {"status": "ok", "rss_scheduler": True}
    assert "Media Tools" in get("/").text
    assert get("/api/transcode/check-ffmpeg").json()["available"]
    subs = get("/api/accounts/rss-subscriptions").json()["subscriptions"]
    assert any(s["fakeid"] == FAKEID and s["total_articles"] == 1 for s in subs)
    for path in ("/api/articles/rss", "/api/articles/rss/" + quote(ACCOUNT)):
        feed = ET.fromstring(get(path).content)
        items = feed.findall("./channel/item")
        assert len(items) == 1, "RSS must deduplicate downloaded and scheduled articles"
        link = items[0].findtext("link")
        assert link.startswith("https://rss.example.test/api/articles/serve-file/")
        assert "容器正文" in get(urlsplit(link).path).text
    from backend.config import DOWNLOAD_HISTORY_FILE, load_json
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    assert len(history) == 1 and history[0]["success"]
    article_dir = Path(history[0]["path"])
    assert list(article_dir.glob("*.md"))
    pdf = next(article_dir.glob("*.pdf"))
    assert pdf.read_bytes().startswith(b"%PDF") and pdf.stat().st_size > 1000
    print("PASS: UI, health, FFmpeg, subscription, RSS links, HTML/Markdown/PDF and persistence")


def seed():
    from backend.config import ACCOUNTS_FILE, DATA_DIR, DOWNLOAD_HISTORY_FILE, save_json
    if DOWNLOAD_HISTORY_FILE.exists() or ACCOUNTS_FILE.exists():
        raise SystemExit("Smoke test requires a fresh disposable data volume")

    class Article(BaseHTTPRequestHandler):
        def do_GET(self):
            content = ('<html><head><meta charset="utf-8"><title>容器文章</title></head>'
                       '<body><div id="js_content"><p>容器正文，中文 PDF 测试。</p></div></body></html>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Article)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/article"
    try:
        from playwright.sync_api import sync_playwright
        from backend.runtime import launch_chromium, launch_persistent_context
        with sync_playwright() as p:
            browser = launch_chromium(p.chromium, headless=True)
            page = browser.new_page()
            # Optional external fonts/captcha scripts must not make this local
            # readiness test depend on Internet availability.
            page.route("**/*", lambda route: route.continue_()
                       if route.request.url.startswith(BASE + "/") else route.abort())
            page.goto(BASE)
            assert "Media Tools" in page.locator("body").inner_text()
            browser.close()
            profile = DATA_DIR / "weread_profiles" / "smoke"
            context = launch_persistent_context(p.chromium, profile, headless=True)
            context.add_cookies([{"name": "smoke", "value": "persisted", "url": url,
                                 "expires": 2147483647}])
            context.close()
            context = launch_persistent_context(p.chromium, profile, headless=True)
            assert any(c["name"] == "smoke" for c in context.cookies())
            context.close()

        save_json(ACCOUNTS_FILE, [{"fakeid": FAKEID, "nickname": ACCOUNT}])
        response = requests.post(BASE + f"/api/accounts/{FAKEID}/rss-subscribe",
                                 json={"interval_minutes": 60}, timeout=10)
        response.raise_for_status()
        from backend.rss_scheduler import RssScheduler
        scheduler = RssScheduler()
        try:
            article = {"title": "容器文章", "link": url, "update_time": 1700000000}
            with patch("backend.articles._fetch_articles_page", return_value=([article], 1)), \
                    patch("backend.rss_scheduler.time.sleep"):
                scheduler._fetch_for_account(scheduler.get_subscription(FAKEID))
                scheduler._fetch_for_account(scheduler.get_subscription(FAKEID))
        finally:
            scheduler.stop()
        verify()
    finally:
        server.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify"]:
        verify()
    else:
        seed()

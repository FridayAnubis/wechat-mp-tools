"""Avatar ownership regressions; all files and network responses are isolated."""

import base64
import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask
from backend import avatar_cache, downloader, rss_feed


ID_A = "MP_WXS_1234567890"
ID_B = "MP_WXS_2345678901"
BIZ_A = base64.b64encode(b"1234567890").decode("ascii")
BIZ_B = base64.b64encode(b"2345678901").decode("ascii")
AVATAR_A = "https://mmbiz.qpic.cn/avatar-a/0"
AVATAR_B = "https://mmbiz.qpic.cn/avatar-b/0"
SHORT_URL = "https://mp.weixin.qq.com/s/test-article-123456789"


def article_page(biz=BIZ_A, avatar=AVATAR_A):
    return ('<html><head><title>测试文章</title></head><body>'
            f'<script>var biz = "{biz}"; var round_head_img = "{avatar}";</script>'
            '<div id="js_content"><p>正文</p></div></body></html>')


class RssAvatarTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="rss-avatar-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.accounts = self.root / "accounts.json"
        self.history = self.root / "download_history.json"
        self.articles = self.root / "rss_articles.json"
        for name, path in (("ACCOUNTS_FILE", self.accounts),
                           ("DOWNLOAD_HISTORY_FILE", self.history),
                           ("RSS_ARTICLES_FILE", self.articles)):
            patcher = patch.object(rss_feed, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module, name in ((rss_feed, "_cache"), (avatar_cache, "_article_avatars"),
                             (avatar_cache, "_failures")):
            patcher = patch.object(module, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write(self.accounts, [{"nickname": "账号A", "fakeid": BIZ_A, "bookId": ID_A}])
        self.write(self.history, [])
        self.write(self.articles, {})
        # No test may accidentally make a live request.
        fetch = patch.object(avatar_cache, "_fetch", side_effect=AssertionError("Unexpected network call"))
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def local_item(self, **overrides):
        metadata = {"source": "账号A", "url": SHORT_URL,
                    "avatar_account_id": ID_A, "round_head_img": AVATAR_A}
        metadata.update(overrides)
        path = self.root / "article"
        self.write(path / "metadata.json", metadata)
        return {"account": "账号A", "link": SHORT_URL, "path": str(path)}

    def test_distinct_homonymous_accounts_never_pick_the_first_avatar(self):
        records = [{"nickname": "同名", "fakeid": BIZ_A, "round_head_img": AVATAR_A},
                   {"nickname": "同名", "fakeid": BIZ_B, "round_head_img": AVATAR_B}]
        for accounts in (records, list(reversed(records))):
            self.write(self.accounts, accounts)
            self.assertEqual(rss_feed.get_account_avatar("同名"), "")

    def test_equivalent_fakeid_and_bookid_are_one_account(self):
        self.write(self.accounts, [
            {"nickname": "账号A", "fakeid": BIZ_A, "round_head_img": AVATAR_A},
            {"nickname": "账号A", "bookId": ID_A, "round_head_img": AVATAR_A},
        ])
        self.assertEqual(rss_feed.get_account_avatar("账号A"), AVATAR_A)
        self.assertEqual(rss_feed.get_account_avatar("账号A", from_article=False), AVATAR_A)

    def test_conflicting_saved_identity_cannot_supply_avatar(self):
        self.write(self.accounts, [{"nickname": "账号A", "fakeid": BIZ_A,
                                   "bookId": ID_B, "round_head_img": AVATAR_B}])
        self.assertEqual(rss_feed.get_account_avatar("账号A"), "")

    def test_matching_metadata_can_supply_avatar(self):
        self.assertEqual(rss_feed.get_account_avatar("账号A", [self.local_item()]), AVATAR_A)
        self.fetch.assert_not_called()

    def test_legacy_metadata_with_matching_biz_url_can_supply_avatar(self):
        url = f"https://mp.weixin.qq.com/s?__biz={BIZ_A}&mid=1&idx=1"
        item = self.local_item(avatar_account_id="", url=url)
        item["link"] = url
        self.assertEqual(rss_feed.get_account_avatar("账号A", [item]), AVATAR_A)

    def test_metadata_with_mismatching_source_or_identity_is_rejected(self):
        for changes in ({"source": "账号B"}, {"avatar_account_id": ID_B},
                        {"avatar_account_id": ""},
                        {"url": f"https://mp.weixin.qq.com/s?__biz={BIZ_B}&mid=1&idx=1"},
                        {"url": "https://mp.weixin.qq.com/s/different-article-123456789"}):
            with self.subTest(changes=changes):
                item = self.local_item(**changes)
                with patch.object(rss_feed, "article_avatar", return_value=""):
                    self.assertEqual(rss_feed.get_account_avatar("账号A", [item]), "")

    def test_item_from_another_account_is_not_a_fallback_candidate(self):
        item = self.local_item()
        item["account"] = "账号B"
        self.assertEqual(rss_feed.get_account_avatar("账号A", [item]), "")
        self.fetch.assert_not_called()

    def test_missing_account_history_cannot_supply_another_accounts_avatar(self):
        item = self.local_item(source="账号B", avatar_account_id=ID_B, round_head_img=AVATAR_B)
        self.write(self.history, [{"title": "其他账号文章", "success": True,
                                   "link": SHORT_URL, "path": item["path"]}])
        self.fetch.side_effect = None
        self.fetch.return_value = (article_page(BIZ_B, AVATAR_B).encode(), "text/html")
        items = rss_feed.collect_items("账号A")
        self.assertEqual(rss_feed.get_account_avatar("账号A", items), "")

    def test_missing_target_identity_does_not_enable_article_fallback(self):
        for accounts in ([], [{"nickname": "账号A"}]):
            with self.subTest(accounts=accounts):
                self.write(self.accounts, accounts)
                self.assertEqual(rss_feed.get_account_avatar("账号A", [self.local_item()]), "")
        self.fetch.assert_not_called()

    def test_disabled_article_fallback_does_not_read_article_avatar(self):
        self.assertEqual(rss_feed.get_account_avatar("账号A", [self.local_item()], from_article=False), "")
        self.fetch.assert_not_called()

    def test_remote_page_identity_must_match_even_on_a_shared_short_url(self):
        self.fetch.side_effect = None
        self.fetch.return_value = (article_page().encode(), "text/html")
        self.assertEqual(avatar_cache.article_avatar(SHORT_URL, BIZ_A), AVATAR_A)
        self.assertEqual(avatar_cache.article_avatar(SHORT_URL, ID_A), AVATAR_A)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(avatar_cache.article_avatar(SHORT_URL, ID_B), "")
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(avatar_cache.article_avatar(SHORT_URL, ID_B), "")
        self.assertEqual(self.fetch.call_count, 2)  # Failed ownership check is throttled too.

    def test_remote_page_without_identity_is_rejected(self):
        self.fetch.side_effect = None
        self.fetch.return_value = (article_page().replace(f'var biz = "{BIZ_A}";', "").encode(), "text/html")
        self.assertEqual(avatar_cache.article_avatar(SHORT_URL, ID_A), "")

    def test_conflicting_link_identity_is_rejected_before_fetch(self):
        url = f"https://mp.weixin.qq.com/s?__biz={BIZ_B}&mid=1&idx=1"
        self.assertEqual(avatar_cache.article_avatar(url, ID_A), "")
        self.fetch.assert_not_called()

    def test_page_identity_extraction_handles_escaping_and_conflicts(self):
        self.assertEqual(avatar_cache.extract_avatar_account_id(f'var biz = "{BIZ_A}";'), ID_A)
        self.assertEqual(avatar_cache.extract_avatar_account_id(
            f'window.__biz = "{BIZ_A.replace("=", chr(92) + "x3d")}";'), ID_A)
        self.assertEqual(avatar_cache.extract_avatar_account_id(
            f'var biz = "{BIZ_A}"; var __biz = "{BIZ_B}";'), "")
        self.assertEqual(avatar_cache.extract_avatar_account_id(
            f'<a href="https://mp.weixin.qq.com/s?__biz={BIZ_B}">其他文章</a>'), "")

    def test_feeds_keep_different_local_avatar_urls_and_existing_etags(self):
        self.write(self.accounts, [
            {"nickname": "账号A", "fakeid": BIZ_A, "round_head_img": AVATAR_A},
            {"nickname": "账号B", "fakeid": BIZ_B, "round_head_img": AVATAR_B},
        ])
        def cached_filename(url):
            return hashlib.sha256(url.encode()).hexdigest() + ".jpg"
        app = Flask(__name__)
        urls = []
        with patch.object(rss_feed, "cache_avatar", side_effect=cached_filename), \
                patch.object(rss_feed, "get_settings", return_value={"rss_cache_ttl": 300}):
            for name, avatar in (("账号A", AVATAR_A), ("账号B", AVATAR_B)):
                with app.test_request_context(f"/api/articles/rss/{name}?full=0"):
                    from flask import request
                    response = rss_feed.rss_response(request, name)
                self.assertEqual(response.status_code, 200)
                root = ET.fromstring(response.get_data(as_text=True))
                self.assertEqual(root.findtext("channel/title"), name)
                url = root.findtext("channel/image/url")
                self.assertEqual(url, "http://localhost/api/articles/avatars/" + cached_filename(avatar))
                urls.append(url)
                with app.test_request_context(f"/api/articles/rss/{name}?full=0",
                                              headers={"If-None-Match": response.headers["ETag"]}):
                    self.assertEqual(rss_feed.rss_response(request, name).status_code, 304)
        self.assertNotEqual(urls[0], urls[1])

    def test_download_persists_avatar_ownership_without_changing_result_contract(self):
        with patch.object(downloader, "fetch_wechat_article_html",
                          return_value=(article_page(), SHORT_URL, "")), \
                patch.object(downloader, "get_settings", return_value={
                    "auto_save_images": False, "auto_save_videos": False,
                    "save_markdown": False, "save_pdf": False,
                }), patch.object(downloader, "get_proxies_dict", return_value=None), \
                patch.object(downloader, "report_proxy_status"):
            result = downloader.download_single_article(SHORT_URL, self.root / "downloads")
        self.assertTrue(result["success"])
        metadata = json.loads((Path(result["path"]) / "metadata.json").read_text())
        self.assertEqual(metadata["avatar_account_id"], ID_A)
        self.assertEqual(metadata["round_head_img"], AVATAR_A)


if __name__ == "__main__":
    unittest.main()

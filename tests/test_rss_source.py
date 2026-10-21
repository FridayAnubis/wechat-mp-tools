"""Regression coverage for source grouping after the RSS module extraction."""

import json
import xml.etree.ElementTree as ET

import pytest
from flask import Flask

from backend import downloader, rss_feed


@pytest.mark.parametrize("author", ["署名作者", ""])
@pytest.mark.parametrize("directory", ["url_download", "测试公众号"])
def test_download_keeps_source_separate_from_author(tmp_path, monkeypatch, author, directory):
    url = "https://mp.weixin.qq.com/s/source-test"
    page = (
        '<html><head><meta property="og:title" content="测试文章"></head>'
        '<body><script>var nickname = "测试公众号";'
        f'var author = "{author}";</script>'
        '<div id="js_content"><p>正文</p></div></body></html>'
    )
    monkeypatch.setattr(downloader, "fetch_wechat_article_html", lambda *a, **kw: (page, url, ""))
    monkeypatch.setattr(downloader, "get_settings", lambda: {
        "auto_save_images": False, "auto_save_videos": False,
        "save_markdown": False, "save_pdf": False,
    })
    result = downloader.download_single_article(url, tmp_path / directory)
    assert result["success"]
    assert result["source"] == "测试公众号"
    assert result["author"] == author


@pytest.fixture
def rss_data(tmp_path, monkeypatch):
    history = tmp_path / "download_history.json"
    articles = tmp_path / "rss_articles.json"
    monkeypatch.setattr(rss_feed, "DOWNLOAD_HISTORY_FILE", history)
    monkeypatch.setattr(rss_feed, "RSS_ARTICLES_FILE", articles)
    monkeypatch.setattr(rss_feed, "get_settings", lambda: {})
    rss_feed._cache.clear()
    for name in ("RSS_TOKEN", "RSS_BASE_URL", "RSS_MAX_ITEMS", "RSS_CACHE_TTL"):
        monkeypatch.delenv(name, raising=False)
    yield history, articles
    rss_feed._cache.clear()


def test_subscription_grouping_dedup_and_metadata_author(rss_data, tmp_path):
    history, articles = rss_data
    article_dir = tmp_path / "article"
    article_dir.mkdir()
    (article_dir / "metadata.json").write_text(json.dumps({"author": "作者 & 合著者"}))
    link = "https://mp.weixin.qq.com/s/shared"
    articles.write_text(json.dumps({"公众号": [{"title": "文章", "link": link, "update_time": 1}]}))
    history.write_text(json.dumps([{
        "title": "文章", "link": link, "account": "公众号", "success": True,
        "path": str(article_dir),
    }]))

    items = rss_feed.collect_items("公众号")
    assert len(items) == 1
    assert items[0]["account"] == "公众号"
    assert items[0]["author"] == "作者 & 合著者"
    assert rss_feed.collect_items("作者 & 合著者") == []
    root = ET.fromstring(rss_feed.render_feed(items, "标题", "摘要", "https://rss.example/"))
    assert root.findtext("channel/item/{http://purl.org/dc/elements/1.1/}creator") == "作者 & 合著者"


def test_rss_route_supports_proxy_fulltext_and_etag(rss_data, tmp_path):
    from backend.articles import articles_bp

    history, articles = rss_data
    article_dir = tmp_path / "公众号" / "文章"
    article_dir.mkdir(parents=True)
    (article_dir / "文章.html").write_text('<div id="js_content"><p><strong>正文</strong></p></div>')
    history.write_text("[]")
    articles.write_text(json.dumps({"公众号": [{
        "title": "文章", "link": "https://mp.weixin.qq.com/s/rss-test",
        "author": "作者", "path": str(article_dir), "update_time": 1,
    }]}))
    app = Flask(__name__)
    app.register_blueprint(articles_bp)
    client = app.test_client()
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "rss.example"}

    response = client.get("/api/articles/rss/公众号", headers=headers)
    assert response.status_code == 200
    item = ET.fromstring(response.data).find("channel/item")
    assert item.findtext("link").startswith("https://rss.example/api/articles/serve-file/")
    assert "<strong>正文</strong>" in item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
    assert item.findtext("{http://purl.org/dc/elements/1.1/}creator") == "作者"
    cached = client.get("/api/articles/rss/公众号", headers={**headers, "If-None-Match": response.headers["ETag"]})
    assert cached.status_code == 304

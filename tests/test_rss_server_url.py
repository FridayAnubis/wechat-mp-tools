from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from flask import Flask
import pytest

from backend import articles
from backend.rss_scheduler import rss_scheduler


@pytest.mark.parametrize("public_url,expected", [
    ("", "http://nas.local:15200/"),
    ("https://rss.example.com", "https://rss.example.com/"),
    ("https://rss.example.com/", "https://rss.example.com/"),
])
def test_rss_links_reach_persisted_article(monkeypatch, tmp_path, public_url, expected):
    article_dir = tmp_path / "公众号" / "测试文章"
    article_dir.mkdir(parents=True)
    (article_dir / "测试文章.html").write_text("<p>离线正文</p>", encoding="utf-8")
    (article_dir / "content.txt").write_text("离线正文", encoding="utf-8")
    item = {"success": True, "title": "测试文章 & 正文", "account": "公众号",
            "path": str(article_dir), "link": "https://mp.weixin.qq.com/s/example", "time": 1700000000}
    monkeypatch.setattr(articles, "load_json", lambda *args: [item])
    monkeypatch.setattr(articles, "get_settings", lambda: {"download_dir": str(tmp_path)})
    monkeypatch.setattr(articles, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(articles, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_scheduler, "get_articles", lambda account: [item])
    monkeypatch.setenv("WECHAT_MP_PUBLIC_URL", public_url)
    app = Flask(__name__)
    app.register_blueprint(articles.articles_bp)
    client = app.test_client()
    response = client.get("/api/articles/rss/公众号", base_url="http://nas.local:15200")
    assert response.status_code == 200
    feed = ET.fromstring(response.data)
    assert feed.findtext("./channel/link") == expected
    items = feed.findall("./channel/item")
    assert len(items) == 1
    assert items[0].findtext("title") == "测试文章 & 正文"
    link = items[0].findtext("link")
    assert link.startswith(expected)
    response = client.get(urlsplit(link).path)
    assert response.status_code == 200
    assert "离线正文" in response.get_data(as_text=True)

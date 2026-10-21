import json
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from flask import Flask
import pytest

from backend import articles, downloader
from backend.rss_scheduler import rss_scheduler

DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

ARTICLE_HTML = """<!DOCTYPE html>
<html><head><style>body { color: #333; }</style></head>
<body>
<h1>测试文章</h1>
<div id="js_content">
<section style="color: rgb(255, 0, 0); font-size: 18px;"><p><strong>加粗段落</strong></p></section>
<img src="media/img_001.jpg" onclick="alert(1)">
<section style="background-image: url(&quot;media/bg_001.png&quot;);">背景</section>
<a href="https://mp.weixin.qq.com/s/other">外链</a>
<script>alert(2)</script>
</div>
</body>
</html>"""


def _feed(monkeypatch, tmp_path, history, account="建筑档案", subs=None, cached=None):
    monkeypatch.setattr(articles, "load_json",
                        lambda path, default=None: history if path == articles.DOWNLOAD_HISTORY_FILE
                        else json.loads(path.read_text(encoding="utf-8")) if path.exists() else default)
    monkeypatch.setattr(articles, "get_settings", lambda: {"download_dir": str(tmp_path)})
    monkeypatch.setattr(articles, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(articles, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_scheduler, "get_subscriptions", lambda: subs or [])
    monkeypatch.setattr(rss_scheduler, "get_articles", lambda nick: (cached or {}).get(nick, []))
    monkeypatch.setenv("WECHAT_MP_PUBLIC_URL", "https://rss.example.com")
    app = Flask(__name__)
    app.register_blueprint(articles.articles_bp)
    client = app.test_client()
    url = f"/api/articles/rss/{account}" if account else "/api/articles/rss"
    response = client.get(url)
    assert response.status_code == 200
    return client, ET.fromstring(response.data).findall("./channel/item")


def _make_article(tmp_path, account, dir_name, file_stem, author):
    art_dir = tmp_path / account / dir_name
    (art_dir / "media").mkdir(parents=True)
    (art_dir / f"{file_stem}.html").write_text(ARTICLE_HTML, encoding="utf-8")
    (art_dir / f"{file_stem}_raw.html").write_text("<p>raw</p>", encoding="utf-8")
    (art_dir / "media" / "img_001.jpg").write_bytes(b"jpg")
    (art_dir / "content.txt").write_text("加粗段落", encoding="utf-8")
    (art_dir / "metadata.json").write_text(
        json.dumps({"source": account, "author": author}, ensure_ascii=False), encoding="utf-8")
    return art_dir


def test_rss_content_keeps_rich_html_with_absolute_media(monkeypatch, tmp_path):
    art_dir = _make_article(tmp_path, "建筑档案", "测试文章_1", "测试文章", "张三")
    history = [{"success": True, "title": "测试文章", "account": "建筑档案", "path": str(art_dir),
                "link": "https://mp.weixin.qq.com/s/a", "time": 1700000000}]
    client, items = _feed(monkeypatch, tmp_path, history)
    assert len(items) == 1
    body = items[0].findtext(f"{CONTENT}encoded")
    assert "<strong>加粗段落</strong>" in body
    assert 'style="color: rgb(255, 0, 0); font-size: 18px;"' in body
    base = "https://rss.example.com/api/articles/serve-file/%E5%BB%BA%E7%AD%91%E6%A1%A3%E6%A1%88/%E6%B5%8B%E8%AF%95%E6%96%87%E7%AB%A0_1/"
    assert f'src="{base}media/img_001.jpg"' in body
    assert f"url(&quot;{base}media/bg_001.png&quot;)" in body
    assert 'href="https://mp.weixin.qq.com/s/other"' in body
    assert "<script" not in body and "onclick" not in body and "<h1>" not in body
    assert items[0].findtext(f"{DC}creator") == "张三"

    # 同名文章目录 `标题_1` 中的文件仍叫 `标题.html`：链接应指向真实文件
    link = items[0].findtext("link")
    assert link == f"{base}%E6%B5%8B%E8%AF%95%E6%96%87%E7%AB%A0.html"
    assert "加粗段落" in client.get(urlsplit(link).path).get_data(as_text=True)
    assert client.get(urlsplit(f"{base}media/img_001.jpg").path).data == b"jpg"


def test_rss_falls_back_to_escaped_text_paragraphs(monkeypatch, tmp_path):
    art_dir = tmp_path / "建筑档案" / "纯文本"
    art_dir.mkdir(parents=True)
    (art_dir / "content.txt").write_text("第一段 <b>\n\n第二段", encoding="utf-8")
    history = [{"success": True, "title": "纯文本", "account": "建筑档案", "path": str(art_dir),
                "link": "https://mp.weixin.qq.com/s/t"}]
    _, items = _feed(monkeypatch, tmp_path, history)
    assert items[0].findtext(f"{CONTENT}encoded") == "<p>第一段 &lt;b&gt;</p>\n<p>第二段</p>"


def test_auto_fetched_articles_group_by_subscription_not_author(monkeypatch, tmp_path):
    subs = [{"nickname": "建筑档案"}]
    cached = {"建筑档案": [
        {"title": "甲", "link": "https://mp.weixin.qq.com/s/1", "author": "张三"},
        {"title": "乙", "link": "https://mp.weixin.qq.com/s/2", "author": "李四"},
    ]}
    _, items = _feed(monkeypatch, tmp_path, [], account=None, subs=subs, cached=cached)
    assert {i.findtext("title") for i in items} == {"甲", "乙"}
    _, items = _feed(monkeypatch, tmp_path, [], account="建筑档案", subs=subs, cached=cached)
    assert len(items) == 2


@pytest.mark.parametrize("account_name,result_source,expected", [
    ("建筑档案", "建筑档案", "建筑档案"),
    ("url_download", "某公众号", "某公众号"),
    ("url_download", "url_download", "链接下载"),
])
def test_batch_download_records_publisher_not_author(monkeypatch, tmp_path, account_name, result_source, expected):
    saved = {}
    monkeypatch.setattr(articles, "get_settings", lambda: {"download_dir": str(tmp_path), "request_delay": 0})
    monkeypatch.setattr(articles, "load_json", lambda *a, **k: [])
    monkeypatch.setattr(articles, "save_json", lambda path, data: saved.setdefault("history", data))
    monkeypatch.setattr(downloader, "download_single_article", lambda *a, **k: {
        "success": True, "title": "文章", "source": result_source, "author": "张三", "path": str(tmp_path)})
    articles._download_tasks["t"] = {"completed": 0, "failed": 0, "results": [], "status": "running"}
    articles._do_batch_download("t", [{"link": "https://mp.weixin.qq.com/s/x", "title": "文章"}], account_name)
    record = saved["history"][0]
    assert record["account"] == expected
    assert record["author"] == "张三"


def test_fix_history_accounts_only_touches_author_misattribution(monkeypatch, tmp_path):
    import scripts.fix_history_accounts as fix

    wrong = _make_article(tmp_path, "建筑档案", "a", "a", "张三")
    right = _make_article(tmp_path, "建筑档案", "b", "b", "李四")
    history = [
        {"title": "a", "account": "张三", "path": str(wrong)},
        {"title": "b", "account": "建筑档案", "path": str(right)},
        {"title": "c", "account": "抖音", "path": str(tmp_path / "missing")},
    ]
    history_file = tmp_path / "download_history.json"
    history_file.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fix, "DOWNLOAD_HISTORY_FILE", history_file)
    monkeypatch.setattr("sys.argv", ["fix", "--apply"])
    assert fix.main() == 0
    fixed = json.loads(history_file.read_text(encoding="utf-8"))
    assert [h["account"] for h in fixed] == ["建筑档案", "建筑档案", "抖音"]
    assert fixed[0]["author"] == "张三"
    assert list(tmp_path.glob("download_history.json.bak-*"))

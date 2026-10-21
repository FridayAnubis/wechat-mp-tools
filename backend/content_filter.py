"""文章内容过滤：按关键词 / 特征屏蔽干扰元素

针对「部分公众号用图片充当小节标题」这类排版问题。核心是横幅图识别：
微信编辑器会给正文图片写入 ``data-ratio``（高/宽）和 ``data-w``（原始宽度），
被当作小节标题的横幅图通常「很宽很扁」——ratio 很小、宽度很大，正文配图不会这样。

规则保存在 ``data/content_filter.json``，文件变化后自动生效（按 mtime 缓存）。
所有规则都可以关掉；默认只处理明显是装饰/广告的元素，避免误删正文。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

RULES_FILE = DATA_DIR / "content_filter.json"

DEFAULT_RULES = {
    "enabled": True,
    # 用 CSS 选择器精确摘除的元素（微信固定的广告/二维码/赞赏区块）
    "remove_selectors": [
        "#js_pc_qr_code",
        ".qr_code_pc",
        ".reward_area",
        ".js_ad_link",
        ".rich_media_tool",
    ],
    # 短文本块命中关键词就整块删除（广告语、引导关注）
    "remove_text_keywords": [
        "点击上方蓝字",
        "长按识别二维码",
        "扫码关注",
        "点击蓝字关注",
    ],
    # 图片的 alt / title / 文件名命中关键词就删（装饰图、二维码）
    "remove_image_keywords": [
        "分割线",
        "小节标题",
        "二维码",
        "扫码",
    ],
    # 横幅图识别：宽高比 <= max_ratio 且宽度 >= min_width
    "banner_images": {
        "enabled": True,
        "max_ratio": 0.22,
        "min_width": 500,
    },
    # 删掉过滤后变空、且本来就没有图片/视频的段落
    "remove_empty_blocks": True,
    # 安全阀：过滤后正文文字减少超过这个比例就整体放弃，返回原文
    "max_text_loss": 0.2,
}

_cache: dict = {"mtime": None, "rules": None}
_lock = threading.Lock()
_last_stats: dict = {}


# ---------------------------------------------------------------- 规则读写


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_rules(force: bool = False) -> dict:
    """读取规则；文件不存在时返回默认值，不自动落盘。"""
    try:
        mtime = RULES_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None

    with _lock:
        if not force and _cache["rules"] is not None and _cache["mtime"] == mtime:
            return _cache["rules"]

    rules = dict(DEFAULT_RULES)
    if mtime is not None:
        try:
            data = json.loads(RULES_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                rules = _merge(DEFAULT_RULES, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("content_filter.json 解析失败，使用默认规则: %s", exc)

    with _lock:
        _cache["mtime"] = mtime
        _cache["rules"] = rules
    return rules


def save_rules(rules: dict) -> dict:
    merged = _merge(DEFAULT_RULES, rules or {})
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    load_rules(force=True)
    return merged


def last_stats() -> dict:
    return dict(_last_stats)


# ---------------------------------------------------------------- 特征识别


def _image_width(img) -> int:
    for key in ("data-w", "width"):
        raw = img.get(key)
        if not raw:
            continue
        try:
            return int(float(str(raw).replace("px", "").strip()))
        except (TypeError, ValueError):
            continue
    return 0


def _image_ratio(img):
    """返回 高/宽。优先用微信编辑器的 data-ratio，其次用 width/height 属性。"""
    raw = img.get("data-ratio")
    if raw:
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            pass

    width, height = _image_width(img), 0
    for key in ("data-h", "height"):
        val = img.get(key)
        if val:
            try:
                height = int(float(str(val).replace("px", "").strip()))
                break
            except (TypeError, ValueError):
                continue
    if width and height:
        return height / width
    return None


def _is_leaf_block(node) -> bool:
    """只对「叶级」块做关键词删除，避免把整篇文章一起干掉。"""
    for child in node.find_all(True):
        if child.name in ("p", "div", "section", "article", "ul", "ol", "table"):
            return False
    return True


# ---------------------------------------------------------------- 主流程


def filter_article_html(html: str, rules: dict | None = None) -> str:
    """按规则清洗正文 HTML，返回新的 HTML 字符串。"""
    global _last_stats

    if not html:
        return html

    rules = rules or load_rules()
    if not rules.get("enabled", True):
        _last_stats = {"enabled": False}
        return html

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("未安装 beautifulsoup4，跳过内容过滤")
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001
        logger.warning("内容过滤解析失败，返回原文: %s", exc)
        return html

    stats = {"selectors": 0, "images": 0, "banners": 0, "text_blocks": 0, "empty": 0}

    # 过滤前的正文字数，用于最后的安全阀对比
    before_len = len(soup.get_text("", strip=True))

    # 1) CSS 选择器
    for selector in rules.get("remove_selectors") or []:
        try:
            for node in soup.select(selector):
                node.decompose()
                stats["selectors"] += 1
        except Exception:  # noqa: BLE001
            continue

    # 2) 图片：关键词 + 横幅特征
    banner = rules.get("banner_images") or {}
    banner_on = bool(banner.get("enabled", True))
    max_ratio = float(banner.get("max_ratio", 0.22) or 0.22)
    min_width = int(banner.get("min_width", 500) or 500)
    img_keywords = [str(k).lower() for k in (rules.get("remove_image_keywords") or []) if k]

    for img in soup.find_all("img"):
        haystack = " ".join(
            str(img.get(attr) or "") for attr in ("alt", "title", "src", "data-src")
        ).lower()

        if img_keywords and any(kw in haystack for kw in img_keywords):
            img.decompose()
            stats["images"] += 1
            continue

        if banner_on:
            ratio = _image_ratio(img)
            width = _image_width(img)
            if ratio is not None and width >= min_width and 0 < ratio <= max_ratio:
                img.decompose()
                stats["banners"] += 1

    # 3) 短文本块关键词
    text_keywords = [str(k) for k in (rules.get("remove_text_keywords") or []) if k]
    if text_keywords:
        targets = []
        for node in soup.find_all(["p", "section", "blockquote", "li", "h1", "h2", "h3", "h4"]):
            text = node.get_text(" ", strip=True)
            if not text or len(text) > 200:
                continue
            if not _is_leaf_block(node):
                continue
            if any(kw in text for kw in text_keywords):
                targets.append(node)
        for node in targets:
            node.decompose()
            stats["text_blocks"] += 1

    # 4) 清掉过滤后变成空壳的段落
    if rules.get("remove_empty_blocks", True):
        empties = []
        for node in soup.find_all(["p", "section"]):
            if node.find(["img", "video", "iframe", "table", "audio"]):
                continue
            if node.get_text(" ", strip=True):
                continue
            empties.append(node)
        for node in empties:
            node.decompose()
            stats["empty"] += 1

    # 安全阀：正文文字掉得太多说明规则误伤，或者 HTML 太畸形被 bs4 重组掉了。
    # 宁可不过滤，也不能把用户文章删没。
    max_loss = float(rules.get("max_text_loss", 0.2) or 0)
    after_len = len(soup.get_text("", strip=True))
    if before_len and after_len < before_len * (1 - max_loss):
        logger.warning(
            "内容过滤后正文过短（%d -> %d 字），已放弃本次过滤", before_len, after_len
        )
        _last_stats = {
            "aborted": True, "before_chars": before_len, "after_chars": after_len,
        }
        return html

    _last_stats = stats
    if any(stats.values()):
        logger.info(
            "内容过滤：选择器 %d，图片 %d，横幅 %d，文本块 %d，空段 %d",
            stats["selectors"], stats["images"], stats["banners"],
            stats["text_blocks"], stats["empty"],
        )

    return str(soup)


# ---------------------------------------------------------------- 存量重跑


def refilter_existing(download_dir, dry_run: bool = True, force: bool = False) -> dict:
    """对已下载的文章重新跑一遍过滤。

    只处理「清洗后」的 ``<标题>.html``，改写前留一份 ``.html.bak``；
    ``content.txt`` 同步重算（RSS 读的就是它）。``_raw.html`` 不动。
    """
    from backend.downloader import clean_html_to_text

    root = Path(download_dir)
    result = {"scanned": 0, "changed": 0, "content_updated": 0,
              "skipped": 0, "dry_run": dry_run, "errors": []}
    if not root.exists():
        return result

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        result["errors"].append("缺少 beautifulsoup4，无法重跑")
        return result

    for account_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for art_dir in sorted(p for p in account_dir.iterdir() if p.is_dir()):
            html_path = art_dir / f"{art_dir.name}.html"
            if not html_path.is_file():
                # 兼容带有序号后缀（如 _1）或目录名与文件名不完全一致的情况
                candidates = [
                    f for f in art_dir.glob("*.html")
                    if not f.name.endswith(("_raw.html", ".bak"))
                ]
                if candidates:
                    html_path = candidates[0]
                else:
                    result["skipped"] += 1
                    continue

            result["scanned"] += 1
            try:
                original = html_path.read_text(encoding="utf-8", errors="replace")
                filtered = filter_article_html(original)
                html_changed = filtered != original

                # html 没变化但要求 force 时，仍然重算 content.txt
                if not html_changed and not force:
                    continue
                if html_changed:
                    result["changed"] += 1
                if dry_run:
                    continue

                if html_changed:
                    backup = html_path.with_suffix(".html.bak")
                    if not backup.exists():
                        backup.write_text(original, encoding="utf-8")
                    html_path.write_text(filtered, encoding="utf-8")

                # content.txt 由过滤后的正文重算，RSS 立即受益。
                # 用 bs4 精确定位 #js_content，避免把 <title>/<h1> 也算进正文。
                body_html = filtered
                try:
                    node = BeautifulSoup(filtered, "html.parser").find(id="js_content")
                    if node is not None:
                        body_html = str(node)
                except Exception:  # noqa: BLE001
                    pass
                (art_dir / "content.txt").write_text(
                    clean_html_to_text(body_html), encoding="utf-8"
                )
                result["content_updated"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{art_dir.name}: {exc}")

    return result

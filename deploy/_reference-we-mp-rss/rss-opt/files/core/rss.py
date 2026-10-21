"""RSS 生成与缓存（优化版）

相对上游 core/rss.py 的改动（详见 rss-opt/README.md）：
1. 时间格式化不再依赖 locale，避免容器 LANG 为 zh_CN 时 %a/%b 输出中文导致 RSS 非法
2. CDATA 通过占位符后处理实现，修掉「CDATA 被转义成 &lt;![CDATA[...]]&gt;」的问题
3. Atom 的 updated 改为 RFC3339，content 用 <content type="html">，author 补 <name>
4. 缓存写入改为原子替换（先写 .tmp 再 os.replace），避免读到半截文件
5. 新增缓存新鲜度判断 cache_mtime / is_cache_fresh / get_cache(max_age)
6. 新增 cache_content_if_needed，避免每次生成都重写全部正文缓存
7. 路径拼接改用 commonpath 校验，比 startswith 更严
8. 新增 md / txt 的真实文本输出（上游把 .md 也返回 Atom XML）
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from core.content_format import format_content

CST = timezone(timedelta(hours=8))

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_CDATA_TOKEN = "@@WERSS_CDATA_%d@@"


def _safe_join(base: str, name: str) -> str:
    """在 base 目录下安全拼接文件名，阻止 ../ 越权。"""
    base_abs = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base_abs, name))
    try:
        inside = os.path.commonpath([base_abs, target]) == base_abs
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("Path traversal detected: %s" % name)
    return target


def _as_datetime(value) -> datetime:
    """把 datetime / ISO 字符串 / 时间戳字符串统一成带时区的 datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.now(CST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def to_rfc822(value) -> str:
    """RFC 822（RSS 2.0 的 pubDate 要求），显式英文星期/月份。"""
    dt = _as_datetime(value).astimezone(CST)
    return "%s, %02d %s %04d %02d:%02d:%02d %s" % (
        _WEEKDAYS[dt.weekday()],
        dt.day, _MONTHS[dt.month - 1], dt.year,
        dt.hour, dt.minute, dt.second,
        dt.strftime("%z"),
    )


def to_rfc3339(value) -> str:
    """RFC 3339（Atom 的 updated 要求）。"""
    return _as_datetime(value).astimezone(CST).isoformat()


class RSS:
    cache_dir = os.path.normpath("data/cache/rss")
    content_cache_dir = os.path.normpath("data/cache/content")
    rss_file = "all"

    def __init__(self, name: str = "all", cache_dir: str = None, ext: str = "rss"):
        if cache_dir is not None:
            self.cache_dir = cache_dir
        self.ext = ext
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.content_cache_dir, exist_ok=True)
        self.rss_file = _safe_join(self.cache_dir, "%s.%s" % (name, ext))
        self.content_type = None

    # ---------- 类型 ----------

    def get_type(self) -> str:
        if self.ext in ("rss", "xml", "atom"):
            return "application/xml; charset=utf-8"
        if self.ext == "json":
            return "application/json; charset=utf-8"
        if self.ext in ("md", "jmd"):
            return "text/markdown; charset=utf-8"
        return "text/plain; charset=utf-8"

    def set_content_type(self, type: str = None):
        self.content_type = type

    def get_content_type(self) -> str:
        ext = self.ext
        if ext in ("atom", "xml", "json", "markdown"):
            return "html"
        if ext in ("md", "jmd"):
            return "markdown"
        if ext == "txt":
            return "text"
        return "html"

    # ---------- 缓存 ----------

    def cache_mtime(self):
        try:
            return os.path.getmtime(self.rss_file)
        except OSError:
            return None

    def is_cache_fresh(self, max_age=None) -> bool:
        if not max_age or max_age <= 0:
            return True
        mtime = self.cache_mtime()
        return mtime is not None and (time.time() - mtime) < max_age

    def get_cache(self, max_age=None):
        if not self.rss_file:
            return None
        if max_age is not None and not self.is_cache_fresh(max_age):
            return None
        try:
            with open(self.rss_file, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _write(self, payload: str):
        if not self.rss_file:
            return
        try:
            tmp = self.rss_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.rss_file)
        except OSError as e:
            print("RSS 缓存写入失败: %s" % e)

    # ---------- 正文缓存 ----------

    def cache_content(self, content_id: str, content: dict):
        content = dict(content)
        content["content"] = self.add_logo_prefix_to_urls(content.get("content") or "")
        path = _safe_join(self.content_cache_dir, "%s.json" % content_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)

    def cache_content_if_needed(self, content_id: str, content: dict) -> bool:
        """只在「没有缓存」或「缓存正文为空」时落盘，减少重复 IO。"""
        existing = self.get_cached_content(content_id)
        if existing and existing.get("content"):
            return False
        self.cache_content(content_id, content)
        return True

    def get_cached_content(self, content_id: str):
        try:
            path = _safe_join(self.content_cache_dir, "%s.json" % content_id)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    # ---------- 工具 ----------

    def serialize_datetime(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    def datetime_to_rfc822(self, dt) -> str:
        return to_rfc822(dt)

    def add_logo_prefix_to_urls(self, text: str) -> str:
        return text

    @staticmethod
    def _serialize(root, cdata_map) -> str:
        xml = ET.tostring(
            root, encoding="utf-8", method="xml", short_empty_elements=False
        ).decode("utf-8")
        for token, payload in cdata_map.items():
            xml = xml.replace(token, "<![CDATA[%s]]>" % payload)
        return '<?xml version="1.0" encoding="utf-8"?>\r\n' + xml

    @staticmethod
    def _append_text(parent, tag, text, use_cdata, cdata_map):
        node = ET.SubElement(parent, tag)
        text = text or ""
        if use_cdata and text:
            token = _CDATA_TOKEN % len(cdata_map)
            node.text = token
            cdata_map[token] = text.replace("]]>", "]]]]><![CDATA[>")
        else:
            node.text = text
        return node

    # ---------- 生成 ----------

    def generate_rss(self, rss_list, title: str = "Mp-We-Rss",
                     link: str = "https://github.com/rachelos/we-mp-rss",
                     description: str = "RSS频道", language: str = "zh-CN",
                     image_url: str = "") -> str:
        from core.config import cfg
        full_context = bool(cfg.get("rss.full_context", False))
        use_cdata = bool(cfg.get("rss.cdata", False))
        add_cover = bool(cfg.get("rss.add_cover", False))
        cdata_map = {}

        root = ET.Element("rss", version="2.0")
        if full_context:
            root.attrib["xmlns:content"] = "http://purl.org/rss/1.0/modules/content/"

        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text = title
        ET.SubElement(channel, "link").text = link
        ET.SubElement(channel, "description").text = description or ""
        ET.SubElement(channel, "language").text = language
        ET.SubElement(channel, "generator").text = "Mp-We-Rss"
        ET.SubElement(channel, "lastBuildDate").text = to_rfc822(datetime.now(CST))

        if add_cover and image_url:
            image = ET.SubElement(channel, "image")
            ET.SubElement(image, "url").text = image_url
            ET.SubElement(image, "title").text = title
            ET.SubElement(image, "link").text = link

        for data in rss_list:
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = data.get("title") or ""
            ET.SubElement(item, "description").text = data.get("description") or ""
            ET.SubElement(item, "link").text = data.get("link") or ""
            ET.SubElement(item, "guid").text = data.get("link") or ""

            if add_cover and data.get("image"):
                enclosure = ET.SubElement(item, "enclosure")
                enclosure.set("url", str(data["image"]))
                enclosure.set("length", "0")
                enclosure.set("type", "image/jpeg")

            if full_context:
                self._append_text(item, "content:encoded",
                                  str(data.get("content") or ""), use_cdata, cdata_map)

            ET.SubElement(item, "pubDate").text = to_rfc822(data.get("updated"))

        xml = self._serialize(root, cdata_map)
        self._write(xml)
        return xml

    def generate_atom(self, rss_list, title: str = "Mp-We-Rss",
                      link: str = "https://github.com/rachelos/we-mp-rss",
                      description: str = "RSS频道", language: str = "zh-CN",
                      image_url: str = "") -> str:
        from core.config import cfg
        full_context = bool(cfg.get("rss.full_context", False))
        use_cdata = bool(cfg.get("rss.cdata", False))
        add_cover = bool(cfg.get("rss.add_cover", False))
        cdata_map = {}

        feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")
        if full_context:
            feed.attrib["xmlns:content"] = "http://purl.org/rss/1.0/modules/content/"

        ET.SubElement(feed, "title").text = title
        ET.SubElement(feed, "link", rel="alternate", href=link)
        if image_url:
            ET.SubElement(feed, "link", rel="icon", href=image_url)
            ET.SubElement(feed, "logo").text = str(image_url)
            ET.SubElement(feed, "icon").text = str(image_url)
        ET.SubElement(feed, "updated").text = to_rfc3339(datetime.now(CST))
        ET.SubElement(feed, "id").text = str(link)
        ET.SubElement(feed, "subtitle").text = description or ""

        for data in rss_list:
            entry = ET.SubElement(feed, "entry")
            ET.SubElement(entry, "id").text = str(data.get("id") or "")
            ET.SubElement(entry, "title").text = str(data.get("title") or "")
            ET.SubElement(entry, "link", href=str(data.get("link") or ""))
            ET.SubElement(entry, "updated").text = to_rfc3339(data.get("updated"))
            ET.SubElement(entry, "summary").text = str(data.get("description") or "")

            author = ET.SubElement(entry, "author")
            ET.SubElement(author, "name").text = str(data.get("mp_name") or "")

            if add_cover and data.get("image"):
                enclosure = ET.SubElement(entry, "enclosure")
                enclosure.set("url", str(data["image"]))
                enclosure.set("length", "0")
                enclosure.set("type", "image/jpeg")

            if full_context:
                node = ET.SubElement(entry, "content", type="html")
                content = format_content(data.get("content") or "", "html")
                if use_cdata and content:
                    token = _CDATA_TOKEN % len(cdata_map)
                    node.text = token
                    cdata_map[token] = content.replace("]]>", "]]]]><![CDATA[>")
                else:
                    node.text = content

        xml = self._serialize(feed, cdata_map)
        self._write(xml)
        return xml

    def generate_json(self, rss_list, title: str = "Mp-We-Rss",
                      link: str = "https://github.com/rachelos/we-mp-rss",
                      description: str = "RSS频道", language: str = "zh-CN",
                      image_url: str = "") -> str:
        type_ = self.get_content_type()
        result = {
            "name": title,
            "link": link,
            "description": description,
            "language": language,
            "cover": image_url,
            "items": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "description": item["description"],
                    "link": item["link"],
                    "updated": item["updated"].isoformat()
                    if isinstance(item["updated"], datetime) else item["updated"],
                    "content": format_content(item.get("content") or "", type_),
                    "channel_name": item.get("mp_name", ""),
                    "feed": item.get("feed"),
                }
                for item in rss_list
            ],
        }
        payload = json.dumps(result, ensure_ascii=False, indent=2,
                             default=self.serialize_datetime)
        self._write(payload)
        return payload

    def generate_markdown(self, rss_list, title: str = "Mp-We-Rss",
                          link: str = "https://github.com/rachelos/we-mp-rss",
                          description: str = "RSS频道", language: str = "zh-CN",
                          image_url: str = "") -> str:
        lines = ["# %s" % title, "", description or "", ""]
        if link:
            lines.append("- 订阅地址：%s" % link)
        lines.append("")
        for data in rss_list:
            lines.append("## [%s](%s)" % (data.get("title") or "", data.get("link") or ""))
            lines.append("")
            lines.append("- 公众号：%s" % (data.get("mp_name") or ""))
            lines.append("- 发布时间：%s" % to_rfc3339(data.get("updated")))
            lines.append("")
            body = format_content(data.get("content") or "", "markdown")
            if body:
                lines.append(body.strip())
                lines.append("")
        payload = "\n".join(lines)
        self._write(payload)
        return payload

    def generate_text(self, rss_list, title: str = "Mp-We-Rss",
                      link: str = "https://github.com/rachelos/we-mp-rss",
                      description: str = "RSS频道", language: str = "zh-CN",
                      image_url: str = "") -> str:
        lines = [title, "=" * 20, description or "", ""]
        for data in rss_list:
            lines.append(data.get("title") or "")
            lines.append(data.get("link") or "")
            lines.append(to_rfc3339(data.get("updated")))
            body = format_content(data.get("content") or "", "text")
            if body:
                lines.append(body.strip())
            lines.append("-" * 40)
        payload = "\n".join(lines)
        self._write(payload)
        return payload

    def generate_by_template(self, rss_list, template: str, title: str = "Mp-We-Rss",
                             link: str = "https://github.com/rachelos/we-mp-rss",
                             description: str = "RSS频道", language: str = "zh-CN",
                             image_url: str = ""):
        from core.lax import TemplateParser
        parser = TemplateParser(template)
        return parser.render({
            "articles": rss_list,
            "title": title,
            "link": link,
            "description": description,
            "language": language,
            "image_url": image_url,
        })

    def generate(self, rss_list, ext: str = "rss", title: str = "Mp-We-Rss",
                 link: str = "https://github.com/rachelos/we-mp-rss",
                 description: str = "RSS频道", language: str = "zh-CN",
                 image_url: str = "", template: str = None) -> str:
        ext = (ext or "rss").lower().strip(".")
        self.ext = ext
        kwargs = dict(title=title, link=link, description=description,
                      language=language, image_url=image_url)
        if ext in ("rss", "xml"):
            return self.generate_rss(rss_list, **kwargs)
        if ext == "atom":
            return self.generate_atom(rss_list, **kwargs)
        if ext in ("json", "jmd"):
            return self.generate_json(rss_list, **kwargs)
        if ext == "md":
            return self.generate_markdown(rss_list, **kwargs)
        if ext == "txt":
            return self.generate_text(rss_list, **kwargs)
        if template is not None:
            return self.generate_by_template(rss_list, template, **kwargs)
        raise ValueError("Unsupported extension: %s" % ext)

    # ---------- 失效 ----------

    def clear_cache(self, mp_id: str = ""):
        if os.path.exists(self.cache_dir):
            for filename in os.listdir(self.cache_dir):
                if "%s_" % mp_id not in filename:
                    continue
                try:
                    path = _safe_join(self.cache_dir, filename)
                    if os.path.isfile(path):
                        os.unlink(path)
                except Exception as e:
                    print("Error deleting %s: %s" % (filename, e))

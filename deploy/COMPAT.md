# 桌面端兼容性核对

这份改动最初是为「Docker 部署」做的，但有一部分代码是**桌面版和容器版共用**的。
这里逐条列出改动、对桌面端的影响、判断依据和验证方式，确保桌面功能没有被改坏。

验证环境：macOS arm64，`venv312`，直接起 `app.py`（完整桌面入口）。

## 一、结论

- 桌面端 **7 个导航分组、4 行登录状态、品牌名、账号池页面**全部保持原样（截图比对）。
- 所有新增能力都有「桌面分支 / 容器分支」的判定，桌面走的是**原来那段代码**。
- 只发现 **1 处真实的行为变化**（RSS 默认条数），已改回原状。
- 内容过滤是新功能，会改变下载产物，但可通过 `enabled: false` 关闭，且原文 `_raw.html` 始终保留。

## 二、逐项核对

| # | 改动 | 桌面端影响 | 判断依据 | 验证 |
|---|---|---|---|---|
| 1 | `app.py` 新增 `/healthz`、`/api/capabilities` | 无 | 纯新增路由，不覆盖任何已有路径 | 200 + `mode=full` |
| 2 | `config.py` 新增 4 个 `rss_*` 默认值 | 无 | 只往 `DEFAULT_SETTINGS` 加键，`get_settings()` 仍合并旧值 | 设置读取正常 |
| 3 | `config.py` `DOWNLOAD_DIR` 兜底 | 无 | 只有环境变量存在时才生效；桌面不设该变量 | 桌面 `download_dir` 未变 |
| 4 | `runtime.py` 新增 `desktop_available()` / `host_path_hint()` | 无 | 纯新增函数 | 桌面返回 `True`，路径原样 |
| 5 | `articles.py` `get_rss()` 改为委托 `rss_feed` | **有变化**（见第三节） | 路由、参数、XML 结构不变 | 383 条、可解析、带 ETag |
| 6 | `articles.py` `open-folder` 加容器分支 | 无 | `desktop_available()` 为真时跳过新分支 | 仍调用 `open`，返回 200 |
| 7 | `articles.py` `open-file` 加容器分支 | 无 | 同上 | 仍调用 `open`，不返回 `url` |
| 8 | `articles.py` `open-parent` 加容器分支 | 无 | 同上 | 同上 |
| 9 | `articles.py` history 增加 `display_path` + 路径重定位 | 无 | `_resolve_history_path` 只在原路径不存在时才动 | `display_path == path` |
| 10 | `articles.py` 新增 3 个 content-filter 接口 | 无 | 纯新增 | 200 |
| 11 | `downloader.py` PDF 加 `--no-sandbox` | 无 | 仅 `geteuid() == 0` 时加；macOS 普通用户是 501 | `euid=501` → 不加 |
| 12 | `downloader.py` 接入内容过滤 | **有变化**（见第四节） | 只影响清洗产物，`_raw.html` 不动 | 见下 |
| 13 | `frontend/index.html` 引入 `mp-mode.js` | 无 | 脚本在 `mode=full` 时直接 return | 导航 7 组完整 |
| 14 | `frontend/router.js` 加 `MP_MODE` 拦截 | 无 | `window.MP_MODE` 未定义时跳过 | 直接访问各页面正常 |
| 15 | `frontend/history.js` 用 `display_path`、支持 `url` | 无 | 两者都有 `||` 兜底 | 显示与原来一致 |
| 16 | `requirements.txt` 加 `beautifulsoup4` | 无 | 纯新增依赖；`articles.py` 里本来就 `import bs4` | 已装 4.15.0 |
| 17 | `wechat_mp_tools.spec` 加 4 个 hiddenimports | 无 | 只影响打包收集 | 见第五节 |

## 三、唯一的行为变化：RSS 条数

改动过程中我一度把 `rss_max_items` 默认设成 50，**这会让桌面版订阅源从「全部」变成「最近 50 条」**，属于功能退化，已改回：

- `rss_max_items` 默认 **0 = 不限制**（桌面版保持原行为）
- 环境变量 `RSS_MAX_ITEMS` 优先级高于设置文件（Docker 里设 50）

实测：

| 场景 | 条数 |
|---|---|
| 桌面，无参数 | 383（全部） |
| 桌面，`?limit=3` | 3 |
| 桌面，单账号无参数 | 130（全部） |
| 容器，`RSS_MAX_ITEMS=50` | 49（50 条里 1 条无 link 被跳过） |

其余 RSS 差异：按发布时间倒序（原来是写入顺序）、带 ETag/304、反代下链接正确。
这些是修复，不改变可用性；`guid`、`link`、`pubDate` 字段格式保持不变。

## 四、内容过滤对下载产物的影响

这是**新功能**，默认开启。它会让新下载的文章：

- `.html` / `content.txt` / `.md` / PDF 里少了广告块、二维码、横幅装饰图
- `_raw.html` 和 `data.json` **完全保留原始正文**

如果觉得默认规则不合适：

```json
// data/content_filter.json
{ "enabled": false }
```

或只调松横幅阈值：`"banner_images": { "max_ratio": 0.15 }`。

**安全阀**：过滤后正文字数减少超过 20%（`max_text_loss`）会整体放弃，返回原文。
本地 13 篇文章实测：0 篇文本损失、1 张图被删（`data-ratio=0.185` 的横幅）。

## 五、打包（PyInstaller）注意

`wechat_mp_tools.spec` 的 `hiddenimports` 是显式列表，已补：

- `backend.rss_feed`、`backend.content_filter`
- `bs4`、`soupsieve`（bs4 的 CSS 选择器依赖）

不补的话打包版会在运行时报 `ImportError`，被 `except` 吞掉后**静默跳过过滤**——功能失效但不崩。
CI 用的是 `pip install -r requirements.txt`，bs4 会自动带上。

## 六、怎么自己复核

```bash
# 桌面接口回归（不启动 GUI，直接用 Flask test client）
./venv312/bin/python - <<'PY'
import xml.etree.ElementTree as ET, app
c = app.app.test_client()
print("capabilities:", c.get("/api/capabilities").get_json())
print("RSS 条数:", len(ET.fromstring(c.get("/api/articles/rss").data).findall("./channel/item")))
print("history display_path == path:",
      all((i.get("display_path") == i.get("path")) for i in
          c.get("/api/articles/history").get_json()["history"] if i.get("path")))
PY

# 起桌面版看界面（不会弹浏览器）
./venv312/bin/python app.py --no-browser --port 5400
```

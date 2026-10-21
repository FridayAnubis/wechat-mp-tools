"""修复下载历史中被误记为文章署名（author）的公众号归属（account）。

旧版批量下载把 `author or source` 写入了 account，导致 RSS 按作者而非公众号分组。
本脚本只修正特征明确的条目：account == metadata.author 且 != metadata.source。

默认只预览；加 --apply 才写入（写入前自动备份为 download_history.json.bak-<时间戳>）。
Docker:  docker compose exec -T wechat-mp-tools python scripts/fix_history_accounts.py [--apply]
"""
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import DOWNLOAD_HISTORY_FILE, load_json, save_json


def find_fixes(history: list) -> list:
    fixes = []
    for item in history:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        meta = load_json(Path(item["path"]) / "metadata.json", {})
        if not isinstance(meta, dict):
            continue
        source, author = meta.get("source") or "", meta.get("author") or ""
        if not source or source == "url_download" or not author:
            continue
        if item.get("account") == author and author != source:
            fixes.append((item, source, author))
    return fixes


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    fixes = find_fixes(history)
    for item, source, author in fixes:
        print(f"{item.get('title')!r}: account {author!r} -> {source!r}")
    print(f"共 {len(fixes)} 条需要修正" + ("" if apply or not fixes else "（预览模式，加 --apply 写入）"))
    if apply and fixes:
        backup = DOWNLOAD_HISTORY_FILE.with_name(f"{DOWNLOAD_HISTORY_FILE.name}.bak-{int(time.time())}")
        shutil.copy2(DOWNLOAD_HISTORY_FILE, backup)
        for item, source, author in fixes:
            item["account"] = source
            item.setdefault("author", author)
        save_json(DOWNLOAD_HISTORY_FILE, history)
        print(f"已写入，备份：{backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

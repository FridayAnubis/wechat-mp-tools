#!/usr/bin/env python3
"""低频验证真实历史分页，输出脱敏 JSON；不下载正文、不启动后台任务。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def probe(fetch_page, fakeid, count, offsets, rounds, interval, sleep=time.sleep):
    records = []
    passed = False
    for round_no in range(1, rounds + 1):
        first_keys, first_oldest = set(), None
        positions = [0, None] + sorted(set(offsets) - {0})
        for index, offset in enumerate(positions):
            if records:
                sleep(interval)
            if offset is None:
                offset = records[-1]["next_offset"]
            started = time.monotonic()
            page = fetch_page(fakeid, offset, count)
            keys = [fingerprint(a.get("id") or a.get("link")) for a in page.articles]
            times = [a.get("update_time") for a in page.articles]
            times = [t for t in times if isinstance(t, (int, float)) and t > 0]
            records.append({
                "round": round_no, "offset": offset, "count": count,
                "next_offset": page.next_offset, "state": page.state,
                "http_status": page.http_status, "error_code": page.error_code,
                "reason": page.reason, "raw_count": page.raw_count,
                "unique_count": len(keys), "has_more": page.has_more,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "article_fingerprints": keys,
                "oldest_timestamp": min(times) if times else None,
                "newest_timestamp": max(times) if times else None,
            })
            if page.state not in ("ok", "end"):
                return {"g0_passed": passed, "stopped_reason": page.reason, "pages": records}
            if index == 0:
                first_keys = set(keys)
                first_oldest = min(times) if times else None
            elif first_oldest is not None:
                # 必须有第一页以外的、不同标识且时间更早的真实文章作为证据。
                passed |= any(
                    fingerprint(a.get("id") or a.get("link")) not in first_keys
                    and isinstance(a.get("update_time"), (int, float))
                    and 0 < a["update_time"] < first_oldest
                    for a in page.articles
                )
            if page.state == "end":
                break
    return {"g0_passed": passed, "stopped_reason": "", "pages": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fakeid", help="已授权公众号的 fakeid 或 bookId")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--count", type=int, choices=range(5, 31), default=10)
    parser.add_argument("--offsets", type=int, nargs="*", default=[], help="额外检查的位置，如 50 200 500")
    parser.add_argument("--rounds", type=int, choices=(1, 2), default=2)
    parser.add_argument("--interval", type=float, default=10, help="请求间隔秒数，至少 10 秒")
    parser.add_argument("--output", type=Path, help="默认写入所选数据目录")
    args = parser.parse_args()
    if args.interval < 10 or not args.interval < float("inf") or any(o < 0 for o in args.offsets):
        parser.error("interval 必须为至少 10 秒的有限数值，offset 必须非负")

    # 在导入账号池前设置路径，复用目标环境的设置、账号及请求头。
    from backend import config
    for key, value in list(vars(config).items()):
        if key.endswith("_FILE") and isinstance(value, Path) and value.parent == config.DATA_DIR:
            setattr(config, key, args.data_dir / value.name)
    config.DATA_DIR = args.data_dir
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "target_fingerprint": fingerprint(args.fakeid) if args.fakeid else None,
        "g0_passed": False, "pages": [],
    }
    # 无凭证时只记录前置条件，避免尝试无意义的上游请求。
    accounts = config.load_json(config.ACCOUNT_POOL_FILE, [])
    if not accounts:
        report["stopped_reason"] = "missing_account_pool"
    elif not args.fakeid:
        report["stopped_reason"] = "missing_authorized_target"
    else:
        from backend.history_paging import fetch_history_page
        report.update(probe(fetch_history_page, args.fakeid, args.count, args.offsets,
                            args.rounds, max(args.interval, float(config.get_settings()["request_delay"]))))
    output = args.output or args.data_dir / "history_pagination_probe.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"g0_passed": report["g0_passed"], "stopped_reason": report["stopped_reason"],
                      "pages": len(report["pages"]), "report": str(output)}, ensure_ascii=False))
    return 0 if report["g0_passed"] and not report["stopped_reason"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

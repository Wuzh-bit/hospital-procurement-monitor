#!/usr/bin/env python3
"""Preview or remove only aged raw collection caches under one data directory."""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from monitor_common import ensure_data_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely clean aged raw monitoring caches.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Actually delete; otherwise preview only.")
    args = parser.parse_args()
    if args.older_than_days < 1:
        parser.error("--older-than-days 至少为 1")
    raw_dir = ensure_data_dir(args.data_dir) / "raw"
    if not raw_dir.exists():
        print("没有 raw 缓存目录。")
        return 0
    cutoff = time.time() - args.older_than_days * 86400
    targets = [item for item in raw_dir.iterdir() if item.is_dir() and item.stat().st_mtime < cutoff]
    action = "将删除" if args.apply else "将删除（预览，未执行）"
    for target in targets:
        print(f"{action}: {target}")
    if args.apply:
        for target in targets:
            # Target is an immediate child of <data-dir>/raw, resolved before deletion.
            shutil.rmtree(target)
    print(f"匹配 {len(targets)} 个缓存批次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

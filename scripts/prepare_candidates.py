#!/usr/bin/env python3
"""Deterministically filter, classify, and de-duplicate collected notices.

v1.0 enhancements:
- B3: candidates already judged irrelevant are persisted in skipped_notices and
  are not sent for AI review again.
- B4: notices are de-duplicated by canonical URL across sources and pagination
  pages; the first occurrence wins.
- W6: pure-ASCII keywords match with word boundaries, so "CT" no longer hits
  inside words like "doctor".
- W4: titles that look truncated (ending with ... / ...) are flagged in the
  evidence so reviewers know to open the original link.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from monitor_common import canonical_url, create_tables, ensure_data_dir, load_json, utc_now, write_json

NOTICE_TYPES = [
    "需求调查", "市场调研", "参数征集", "采购意向", "需求征集", "方案征集",
    "招标公告", "竞争性磋商", "竞争性谈判", "询价", "单一来源", "中标", "成交", "更正", "延期",
]

# 标题疑似被列表页截断的后缀（W4）：提示审阅者打开原链接确认完整标题。
TRUNCATED_SUFFIXES = ("…", "...", "......")


def make_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    create_tables(connection)
    return connection


def term_match(term: str, lowered_text: str) -> bool:
    lowered = term.lower()
    if lowered.isascii():
        # W6: 纯 ASCII 词按词边界匹配，避免 "CT" 命中 "doctor" 之类的误报。
        return re.search(r"(?<![a-z0-9])" + re.escape(lowered) + r"(?![a-z0-9])", lowered_text) is not None
    return lowered in lowered_text


def matches(text: str, terms) -> list[str]:
    lowered = text.lower()
    return [term for term in (terms or [])
            if str(term).strip() and term_match(str(term).strip(), lowered)]


def classify(notice: dict, rules: dict) -> tuple[str, dict]:
    title = notice.get("title", "")
    text = " ".join([title, notice.get("published_text", "")])
    required = matches(text, rules.get("required", []))
    synonyms = matches(text, rules.get("synonyms", []))
    excluded = matches(text, rules.get("exclude", []))
    notice_types = matches(text, NOTICE_TYPES)
    priority = matches(text, rules.get("priority_notice_types", []))
    evidence = {"required_keywords": required, "synonym_keywords": synonyms,
                "exclude_keywords": excluded, "notice_types": notice_types, "priority_notice_types": priority}
    if title.endswith(TRUNCATED_SUFFIXES):
        evidence["title_truncated"] = True
    if excluded:
        return "excluded", evidence
    if required or synonyms:
        return "candidate", evidence
    return "irrelevant", evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare explainable candidates without using an LLM.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", help="Default: <data-dir>/work/candidates-<run-id>.json")
    args = parser.parse_args()
    config, data_dir = load_json(args.config), ensure_data_dir(args.data_dir)
    run_dir = data_dir / "raw" / args.run_id
    if not run_dir.is_dir():
        parser.error(f"找不到采集批次：{run_dir}")
    hospitals = {item["id"]: item for item in config.get("hospitals", [])}
    connection = make_database(data_dir / "monitor.sqlite3")
    seen_ids = {row[0] for row in connection.execute("SELECT notice_id FROM leads")}
    # B3: 已判不相关的候选记录在 skipped_notices，不再重复送审。
    skipped_ids = {row[0] for row in connection.execute("SELECT notice_id FROM skipped_notices")}
    # B4: 已入库/已跳过线索的 canonical URL 作为去重基线。
    url_seen = set()
    for table in ("leads", "skipped_notices"):
        for (url,) in connection.execute(f"SELECT url FROM {table} WHERE url != ''"):
            url_seen.add(canonical_url(url))
    connection.close()

    candidate_rows, excluded_rows = [], []
    counters = {"already_recorded": 0, "already_skipped": 0, "duplicate_url": 0}
    batch_urls: set[str] = set()
    for source_file in sorted(run_dir.glob("*.json")):
        if source_file.name == "manifest.json":
            continue
        for notice in load_json(source_file).get("notices", []):
            if notice["notice_id"] in seen_ids:
                counters["already_recorded"] += 1
                continue
            if notice["notice_id"] in skipped_ids:
                counters["already_skipped"] += 1
                continue
            url_key = canonical_url(notice.get("url", ""))
            if url_key and (url_key in url_seen or url_key in batch_urls):
                counters["duplicate_url"] += 1
                continue
            gate, evidence = classify(notice, config.get("keywords", {}))
            hospital = hospitals.get(notice.get("hospital_id"), {})
            notice["hospital_name"] = hospital.get("official_name", "")
            notice["evidence"] = evidence
            if gate == "candidate":
                if url_key:
                    batch_urls.add(url_key)
                candidate_rows.append(notice)
            else:
                notice["exclusion_reason"] = "命中排除词" if gate == "excluded" else "未命中用户确认的项目关键词或同义词"
                excluded_rows.append(notice)
    payload = {"run_id": args.run_id, "created_at": utc_now(),
               "candidate_count": len(candidate_rows), "excluded_count": len(excluded_rows),
               "already_recorded_count": counters["already_recorded"],
               "already_skipped_count": counters["already_skipped"],
               "duplicate_url_count": counters["duplicate_url"],
               "candidates": candidate_rows, "excluded": excluded_rows}
    output = Path(args.output) if args.output else data_dir / "work" / f"candidates-{args.run_id}.json"
    write_json(output, payload)
    print(output)
    if len(candidate_rows) > 50:
        print(f"[warn] 本轮候选 {len(candidate_rows)} 条较多：请先检查关键词/排除词是否过宽，"
              f"或按批（每批 20~30 条）审阅", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fetch candidate detail pages and extract compact summaries for AI review.

AI 审阅的成本主要来自“为每个候选打开详情页，正文进入 LLM 上下文”。
本脚本用 Python 批量抓取候选详情页，提取标题、发布时间、预算、截止日期和
正文摘要（前 300 字），输出 work/details-<RUN_ID>.json。AI 审阅时只读压缩
摘要，网络请求和页面正文都不进入 LLM 上下文，从而显著降低 token 与耗时。

提取规则是“尽力而为”：各站点格式差异大，识别不到预算/截止日期时留空，
由正文摘要兜底供 AI 判断。单条失败不中断，错误记录在结果中。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

from monitor_common import ensure_data_dir, fetch_public_url, load_json, utc_now, write_json

# 正文提取时跳过脚本/样式/导航区块
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside"}

BUDGET_RE = re.compile(r"预算(?:金额|上限|价)?[:：]?\s*[¥￥]?\s*\d[\d,]*\.?\d*\s*(?:万元|元|万)")
DEADLINE_RE = re.compile(
    r"(?:截止(?:时间|日期|日)?|递交(?:响应)?文件截止时间|提交投标(?:文件)?截止时间|磋商时间)"
    r"[:：]?\s*(?:至)?\s*((?:20\d{2}|19\d{2})[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}(?:日)?)"
)
PUBLISHED_RE = re.compile(r"发布时间[:：]\s*((?:20\d{2}|19\d{2})[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}(?:日)?)")


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in SKIP_TAGS:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract(body: str, title_hint: str = "", max_summary: int = 300) -> dict:
    parser = TextExtractor()
    parser.feed(body)
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    published_match = PUBLISHED_RE.search(text)
    budget_match = BUDGET_RE.search(text)
    deadline_match = DEADLINE_RE.search(text)
    # 以候选标题为锚点截取摘要：跳过页面导航菜单，正文从公告标题开始。
    start = 0
    if title_hint:
        anchor = title_hint[:20]
        index = text.find(anchor)
        if index >= 0:
            start = index
    return {
        "title": re.sub(r"\s+", " ", title_match.group(1)).strip()[:120] if title_match else "",
        "published": published_match.group(1) if published_match else "",
        "budget": budget_match.group(0) if budget_match else "",
        "deadline": deadline_match.group(1) if deadline_match else "",
        "summary": text[start:start + max_summary],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch candidate details into compact summaries for AI review.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidates", help="Default: <data-dir>/work/candidates-<run-id>.json")
    parser.add_argument("--output", help="Default: <data-dir>/work/details-<run-id>.json")
    parser.add_argument("--delay-seconds", type=float, default=1.0, help="Minimum delay between fetches; do not set below 1.")
    parser.add_argument("--max-summary", type=int, default=300, help="Characters of body text kept in the summary.")
    args = parser.parse_args()
    if args.delay_seconds < 1:
        parser.error("--delay-seconds 不得低于 1 秒")
    data_dir = ensure_data_dir(args.data_dir)
    candidates_path = Path(args.candidates) if args.candidates else data_dir / "work" / f"candidates-{args.run_id}.json"
    payload = load_json(candidates_path)
    candidates = payload.get("candidates", [])
    if not candidates:
        print("没有候选，无需抓取详情。")
        return 0

    details, errors = [], []
    for index, candidate in enumerate(candidates):
        url = candidate.get("url", "")
        entry = {"notice_id": candidate.get("notice_id", ""), "url": url, "fetch_ok": False}
        if not url:
            entry["error"] = "候选缺少 url"
            details.append(entry)
            continue
        try:
            final, ctype, body = fetch_public_url(url, timeout=20)
            entry.update(extract(body, candidate.get("title", ""), args.max_summary))
            entry["final_url"] = final
            entry["fetch_ok"] = True
        except Exception as error:  # 单条失败不中断，记录后继续。
            entry["error"] = f"{type(error).__name__}: {error}"
            errors.append({"notice_id": entry["notice_id"], "url": url, "error": entry["error"]})
        details.append(entry)
        if index < len(candidates) - 1:
            time.sleep(max(1.0, args.delay_seconds))

    output = Path(args.output) if args.output else data_dir / "work" / f"details-{args.run_id}.json"
    write_json(output, {"run_id": args.run_id, "created_at": utc_now(),
                        "fetched_count": sum(1 for d in details if d["fetch_ok"]),
                        "failed_count": len(errors), "candidates": details})
    print(output)
    print(f"详情抓取完成：成功 {len(details) - len(errors)}/{len(details)} 条")
    if errors:
        print(f"{len(errors)} 条抓取失败（详见 details 文件 error 字段）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

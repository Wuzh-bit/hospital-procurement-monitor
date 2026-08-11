#!/usr/bin/env python3
"""Collect links from confirmed public HTML, RSS, or Atom notice sources.

This collector intentionally does not bypass logins, CAPTCHAs, robots controls,
or access protections. It stores a small JSON observation for each source.

v1.0 enhancements:
- Source-level pagination traversal (source.pagination) with --since early stop.
- Structural-noise filter: short titles pointing at list/column URLs are dropped.
- Per-source quality warnings surfaced in manifest.json (JS-rendered / invalid pages).
- Exit code 3 means the run finished but some sources failed (data still written).
"""
from __future__ import annotations

import argparse
import email.utils
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from monitor_common import (
    canonical_url, ensure_data_dir, fetch_public_url, load_json, make_notice_id,
    render_public_url, utc_now, write_json,
)

# 兼容两种日期形态：YYYY 年/月/日（常规）与 MM-DDYYYY 无分隔（如 "05-292026"）。
# 后者常见于复旦儿科等站点的标题日期，旧版无法解析导致 --since 过滤失效。
DATE_RE = re.compile(
    r"(?:20\d{2}|19\d{2})[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}(?:日)?"
    r"|\d{1,2}[./-]\d{1,2}(?:20\d{2}|19\d{2})"
)

# 结构识别：这些区域内的链接视为导航/页脚，不作为公告候选。
SKIP_TAGS = {"nav", "header", "footer", "aside"}

# 列表/栏目 URL 形态识别（N1）：短标题链接指向这类 URL 时，几乎可以确定是栏目导航
# 而非真实公告，采集阶段直接过滤，避免占用送审配额。
LIST_URL_RE = re.compile(
    r"(?:list[-_]?\d+|/channels?/|/cat/\d+|/node[_/]?\d+|/col\d+/|general/list"
    r"|index(?:_\d+)?\.s?html?$)",
    re.I,
)

# 导航/板块链接文本黑名单（兜底）。命中即跳过采集，避免把栏目名当公告送审。
# 可通过配置 keywords.noise_link_texts（推荐）或顶层 noise_link_texts 追加，两处均生效。
NOISE_LINK_TEXTS_BUILTIN = {
    "首页", "网站首页", "本站首页", "网站地图", "联系我们", "人才招聘", "招生信息",
    "院长信箱", "友情链接", "相关链接", "科室导航", "专家介绍", "预约挂号", "出诊信息",
    "患者服务", "门诊服务", "住院服务", "医院概况", "医院简介", "领导团队", "组织架构",
    "历史荣誉", "交通指引", "来访路线", "下载中心", "资料下载", "常用下载", "视频中心",
    "图片新闻", "媒体聚焦", "英文版", "EN", "医疗工作", "科研平台", "科研管理",
    "信息公开", "医疗新闻", "综合信息", "医院新闻", "通知公告", "党建工作", "工会",
    "团委", "后勤保障", "图书馆", "期刊", "学会", "协会", "个人中心", "登录", "注册",
    # 首轮真实回测中出现的栏目/板块链接（非真实采购公告），作为兜底过滤
    "医疗动态", "研究平台", "历史招聘信息", "检验结果", "医疗机构营业许可", "医疗成果",
    "特色医疗技术", "特需医疗", "特色医疗模式", "特色医疗",
}


def is_structural_noise(title: str, url: str) -> bool:
    """短标题 + 列表形态 URL ≈ 栏目导航链接，直接过滤（N1）。"""
    return len(title) < 8 and bool(LIST_URL_RE.search(url))


class NoticeLinkParser(HTMLParser):
    def __init__(self, base_url: str, same_host_only: bool):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.same_host_only = same_host_only
        self.links: list[dict] = []
        self._href: str | None = None
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        low = tag.lower()
        if low in SKIP_TAGS:
            self._skip_depth += 1
        if low == "a" and self._skip_depth == 0:
            href = dict(attrs).get("href")
            if href:
                self._href, self._parts = href, []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag):
        low = tag.lower()
        if low in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if low != "a" or not self._href:
            return
        url = canonical_url(urljoin(self.base_url, self._href))
        base_host = urlparse(self.base_url).hostname
        if urlparse(url).scheme in {"http", "https"} and (not self.same_host_only or urlparse(url).hostname == base_host):
            title = re.sub(r"\s+", " ", "".join(self._parts)).strip()
            if title:
                self.links.append({"url": url, "title": title})
        self._href, self._parts = None, []


def text_date(text: str) -> str:
    match = DATE_RE.search(text)
    return match.group(0) if match else ""


def date_to_iso(value: str) -> str:
    match = re.search(r"((?:20\d{2}|19\d{2}))[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return ""
    # MM-DDYYYY 无分隔形态（如 "05-292026"）。
    match = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})((?:20\d{2}|19\d{2}))(?!\d)", value)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def feed_date_to_iso(value: str) -> str:
    """B2: 兼容 RSS(RFC-822)、Atom(ISO-8601) 以及中文/数字日期。"""
    value = (value or "").strip()
    if not value:
        return ""
    iso = date_to_iso(value)
    if iso:
        return iso
    try:
        return email.utils.parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def parse_html(body: str, base_url: str, max_items: int, same_host_only: bool,
               noise_link_texts=None) -> tuple[list[dict], dict]:
    noise = {str(t).strip() for t in (noise_link_texts or set())}
    parser = NoticeLinkParser(base_url, same_host_only)
    parser.feed(body)
    unique, seen = [], set()
    stats = {"links_seen": len(parser.links), "noise_filtered": 0, "structural_filtered": 0}
    for item in parser.links:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        if item["title"] in noise or len(item["title"]) < 4:
            stats["noise_filtered"] += 1
            continue
        if is_structural_noise(item["title"], item["url"]):
            stats["structural_filtered"] += 1
            continue
        item["published_text"] = text_date(item["title"])
        item["published_iso"] = date_to_iso(item["published_text"])
        unique.append(item)
        if len(unique) >= max_items:
            break
    return unique, stats


def parse_feed(body: str, max_items: int) -> tuple[list[dict], dict]:
    root = ET.fromstring(body)
    nodes = root.findall(".//{*}item") + root.findall(".//{*}entry")
    items = []
    for node in nodes:
        title = (node.findtext("{*}title") or "").strip()
        link_node = node.find("{*}link")
        url = ""
        if link_node is not None:
            url = link_node.get("href") or (link_node.text or "").strip()
        published = (node.findtext("{*}pubDate") or node.findtext("{*}published")
                     or node.findtext("{*}updated") or node.findtext("{*}date") or "").strip()
        if title and url:
            items.append({"url": canonical_url(url), "title": title,
                          "published_text": published, "published_iso": feed_date_to_iso(published)})
        if len(items) >= max_items:
            break
    stats = {"links_seen": len(nodes), "noise_filtered": 0, "structural_filtered": 0}
    return items, stats


def quality_warnings(items: list[dict], stats: dict) -> list[str]:
    """N2: 让“采到了但无效/低质”的信号显式可见，而不是静默产出空结果。"""
    warnings: list[str] = []
    if stats.get("links_seen", 0) == 0:
        warnings.append("未解析到任何条目：页面可能是 JS 动态渲染、需要登录，或 URL 已失效")
        return warnings
    if not items:
        warnings.append(
            f"解析到 {stats['links_seen']} 个链接但全部被噪声过滤；如确属公告栏目，请检查 noise_link_texts 是否过严"
        )
        return warnings
    dated = sum(1 for item in items if item.get("published_iso"))
    long_titles = sum(1 for item in items if len(item["title"]) >= 12)
    # 只要存在长标题公告就不判为导航页（页面底部/侧栏常混入大量短标题导航链接，
    # 避免真实公告列表被误报；纯导航页则一条长标题都没有，仍会告警）。
    if len(items) >= 10 and dated == 0 and long_titles == 0:
        warnings.append("疑似导航页/JS 列表：链接较多但均无日期且标题普遍偏短，建议更换为公告列表直达页")
    return warnings


def parse_source_page(source: dict, body: str, content_type: str, final_url: str,
                      noise_link_texts=None) -> tuple[list[dict], dict]:
    max_items = int(source.get("max_items", 40))
    if source.get("mode") in {"rss", "atom", "feed"} or "xml" in (content_type or ""):
        return parse_feed(body, max_items)
    return parse_html(body, final_url, max_items, bool(source.get("same_host_only", True)),
                      noise_link_texts)


def hits_keyword(item: dict, keywords: list[str]) -> bool:
    """关键词定向（来源级）：标题命中任一关键词才保留；纯 ASCII 词按词边界匹配。"""
    if not keywords:
        return True
    lowered = (item.get("title") or "").lower()
    for keyword in keywords:
        term = str(keyword).strip().lower()
        if not term:
            continue
        if term.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lowered):
                return True
        elif term in lowered:
            return True
    return False


def fetch_source_page(source: dict, url: str) -> tuple[str, str, str]:
    """按来源 mode 选择抓取后端：mode=js 用系统 Edge 无头渲染（公开 JS 页面），
    其余走标准静态抓取。"""
    if source.get("mode") == "js":
        return render_public_url(url)
    return fetch_public_url(url)


def collect_source(source: dict, noise_link_texts=None, delay_seconds: float = 1.0,
                   since: str | None = None, keyword_filter: list[str] | None = None,
                   early_stop_pages: int = 0) -> dict:
    final_url, content_type, body = fetch_source_page(source, source["url"])
    items, stats = parse_source_page(source, body, content_type, final_url, noise_link_texts)
    keyword_filter = [str(k) for k in (keyword_filter or [])]
    if keyword_filter:
        kept = [item for item in items if hits_keyword(item, keyword_filter)]
        stats["keyword_filtered"] = stats.get("keyword_filtered", 0) + (len(items) - len(kept))
        items = kept
    total_stats = dict(stats)
    all_items: list[dict] = list(items)
    seen_urls = {item["url"] for item in items}

    # N3: 原生分页遍历。source.url 视为第 1 页，pagination.url_template 中的 {page}
    # 从 first_page 开始按 step 递增（默认 1）；descending 时递减（适配 offset/旧页在前
    # 的站点）；页间强制限速；命中 --since 提前停止；配置 keyword_early_stop_pages 时
    # 连续 N 页零命中关键词即提前停止（减少抓取范围，详见配置字段说明）。
    pagination = source.get("pagination") or {}
    url_template = str(pagination.get("url_template") or "")
    max_pages = max(0, int(pagination.get("max_pages", 0)))
    first_page = int(pagination.get("first_page", 2))
    step = max(1, int(pagination.get("step", 1)))
    descending = bool(pagination.get("descending", False))
    pages_fetched, early_stopped = 1, False
    page_errors: list[dict] = []
    pages_since_hit = 0
    if url_template and "{page}" in url_template and max_pages > 0:
        for index in range(max_pages):
            page = first_page - index * step if descending else first_page + index * step
            if descending and page < 1:
                break
            time.sleep(max(1.0, delay_seconds))
            page_url = url_template.replace("{page}", str(page))
            try:
                page_final, page_type, page_body = fetch_source_page(source, page_url)
            except Exception as error:  # 分页失败不丢弃已采数据；记录并停止翻页。
                page_errors.append({"page": page, "url": page_url, "error": str(error)})
                break
            pages_fetched += 1
            page_items, page_stats = parse_source_page(source, page_body, page_type,
                                                       page_final, noise_link_texts)
            for key in total_stats:
                total_stats[key] = total_stats.get(key, 0) + page_stats.get(key, 0)
            if keyword_filter:
                page_raw_count = len(page_items)
                page_items = [item for item in page_items if hits_keyword(item, keyword_filter)]
                total_stats["keyword_filtered"] = total_stats.get("keyword_filtered", 0) + (
                    page_raw_count - len(page_items))
                # 关键词定向早停：连续 N 页零命中即停（N=early_stop_pages，未配置时按 1 处理），
                # 减少对公告量巨大来源的抓取范围。
                threshold = early_stop_pages if early_stop_pages > 0 else 1
                if not page_items:
                    pages_since_hit += 1
                    if pages_since_hit >= threshold:
                        early_stopped = True
                        break
                    continue
                pages_since_hit = 0
            if not page_items:
                early_stopped = True
                break
            new_count = 0
            for item in page_items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_items.append(item)
                    new_count += 1
            if new_count == 0:
                early_stopped = True  # 该页与已有内容完全重复，视为越界。
                break
            dated = [i["published_iso"] for i in page_items if i.get("published_iso")]
            if since and dated and all(d < since for d in dated):
                early_stopped = True  # 整页已早于回看窗口，提前停止。
                break

    notices = []
    for item in all_items:
        item.update({
            "notice_id": make_notice_id(source["id"], item["url"], item["title"], item.get("published_text", "")),
            "source_id": source["id"], "source_name": source["name"], "source_type": source["type"],
            "hospital_id": source.get("hospital_id", ""), "collected_at": utc_now(),
        })
        notices.append(item)

    result = {"source_id": source["id"], "source_url": source["url"], "final_url": final_url,
              "collected_at": utc_now(), "notice_count": len(notices), "notices": notices,
              "stats": total_stats}
    if keyword_filter:
        result["keyword_filter"] = {"terms": keyword_filter,
                                    "filtered_count": total_stats.get("keyword_filtered", 0),
                                    "early_stop_pages": early_stop_pages}
    if keyword_filter and not all_items:
        # 关键词定向过滤后无命中是正常结果，不要误报“链接全被噪声过滤”。
        warnings = ["关键词定向（keyword_filter）过滤后无命中公告；如长时间无命中请检查关键词是否过窄"]
    else:
        warnings = quality_warnings(all_items, total_stats)
    if warnings:
        result["warnings"] = warnings
    if url_template and "{page}" in url_template and max_pages > 0:
        result["pagination"] = {"pages_fetched": pages_fetched, "early_stopped": early_stopped,
                                "page_errors": page_errors}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public notices from confirmed sources.")
    parser.add_argument("--config", required=True, help="Confirmed monitor JSON configuration.")
    parser.add_argument("--data-dir", required=True, help="User-owned monitor data directory.")
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"), help="Stable ID shared by one monitoring run.")
    parser.add_argument("--delay-seconds", type=float, default=1.0, help="Minimum delay between sources and pages; do not set below 1.")
    parser.add_argument("--since", help="Only keep items dated on/after YYYY-MM-DD; undated items are retained for review.")
    args = parser.parse_args()
    if args.delay_seconds < 1:
        parser.error("--delay-seconds 不得低于 1 秒")
    if args.since:
        try:
            date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since 必须为 YYYY-MM-DD")
    config, data_dir = load_json(args.config), ensure_data_dir(args.data_dir)
    keywords_cfg = config.get("keywords") or {}
    # B1: 顶层与 keywords.noise_link_texts 两处配置均生效并合并，不再静默忽略。
    noise = (set(config.get("noise_link_texts", []))
             | set(keywords_cfg.get("noise_link_texts", []))
             | NOISE_LINK_TEXTS_BUILTIN)
    sources = [s for s in config.get("sources", []) if s.get("enabled") and s.get("confirmed")]
    if not sources:
        parser.error("没有已确认且已启用的信息源；请先完成来源确认。")
    run_dir = data_dir / "raw" / args.run_id
    results, errors = [], []
    for index, source in enumerate(sources):
        try:
            # 来源级关键词定向（可选）：keyword_filter 命中标题才保留；
            # keyword_early_stop_pages 为连续零命中页数阈值（>0 生效，未配置按 1 处理）。
            keyword_filter = source.get("keyword_filter") or []
            early_stop_pages = max(0, int(source.get("keyword_early_stop_pages", 0) or 0))
            result = collect_source(source, noise, delay_seconds=args.delay_seconds, since=args.since,
                                    keyword_filter=keyword_filter, early_stop_pages=early_stop_pages)
            if args.since:
                kept = [n for n in result["notices"]
                        if not n.get("published_iso") or n["published_iso"] >= args.since]
                result["filtered_before_since"] = result["notice_count"] - len(kept)
                result["notices"], result["notice_count"] = kept, len(kept)
            write_json(run_dir / f"{source['id']}.json", result)
            entry = {"source_id": source["id"], "notice_count": result["notice_count"],
                     "dated_count": sum(1 for n in result["notices"] if n.get("published_iso")),
                     "filtered_before_since": result.get("filtered_before_since", 0),
                     "keyword_filtered": (result.get("keyword_filter") or {}).get("filtered_count", 0),
                     "warnings": result.get("warnings", [])}
            if result.get("pagination"):
                entry["pagination"] = result["pagination"]
            if result.get("keyword_filter"):
                entry["keyword_filter"] = result["keyword_filter"]
            results.append(entry)
            for warning in result.get("warnings", []):
                print(f"[warn] {source['id']}: {warning}", file=sys.stderr)
        except Exception as error:  # Preserve failure details for an agent to report; continue other sources.
            errors.append({"source_id": source.get("id", "unknown"), "error": str(error)})
        if index < len(sources) - 1:
            time.sleep(max(1.0, args.delay_seconds))
    manifest = {"run_id": args.run_id, "collected_at": utc_now(), "since": args.since or "",
                "results": results, "errors": errors}
    write_json(run_dir / "manifest.json", manifest)
    print(run_dir)
    if errors:
        print(f"完成但有 {len(errors)} 个来源失败（退出码 3）；详见 manifest.json", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

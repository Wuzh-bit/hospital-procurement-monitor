#!/usr/bin/env python3
"""Discover and live-verify hospital notice sources with plain Python.

首次建立监测时，信息源的“发现 + 实测验证”由本脚本完成，AI 只负责两件轻量的事：
（1）每家医院至多 1 次搜索官网主页（用户已提供 homepage 则跳过）；
（2）读本脚本输出的紧凑评估 JSON，转成可用性评估表请用户批量确认。
这样把过去“AI 逐页搜索 + 逐页实测推理”的高 Token、易死循环环节替换为确定性脚本。

职责：
1. 医院官网：从 hospitals[].homepage 出发，抓主页、下钻 1 层找“公告/通知/招标/
   采购/公示/信息公开”等栏目链接（每家至多 --max-columns 个）；对每个候选 URL
   实测：可达性、静态可解析、列表条目数、标题日期可读性、分页线索。
2. 省/市政采与公共资源平台：按 references/gov-platforms.json 预置表取候选 URL，抓平台
   首页并下钻 1 层找静态公告栏目（老式平台常见），连同栏目一起实测；预置表未覆盖的省份
   回退到国家级平台（ccgp.gov.cn / ggzy.gov.cn）。另做轻量探测：若站点自带 GET 搜索
   表单（?keyword=xx），在评估输出中标出，供配置关键词搜索页来源时参考。
3. 不搜索、不推理、不翻全站；每家的请求数 ≤ 1(主页) + 4(官网栏目) + 2(平台) + 2(平台栏目) ≈ 9。

用法：
  python scripts/discover_sources.py --hospitals <hospitals-input.json> \
      --data-dir <data-dir> [--platforms references/gov-platforms.json] \
      [--max-columns 3] [--delay-seconds 0.5]

输入 hospitals-input.json（见 templates/hospitals-input.example.json）：
  [{"id": "...", "official_name": "杭州市中医院", "aliases": [], "province": "浙江省",
    "city": "杭州市", "homepage": "https://www.hztcm.cn"}]
  homepage 为可选：用户直接提供官网主页（或公告直达页）地址时填入，可完全跳过搜索。

输出：<data-dir>/work/source-assessment-<时间戳>.json（每家医院官网候选 ≤ 4 条 +
省平台候选 ≤ 2 条，逐条一行摘要，供 AI 转成表格请用户确认）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from monitor_common import canonical_url, ensure_data_dir, fetch_public_url, load_json, render_public_url, utc_now, write_json
from collect_notices import NoticeLinkParser, NOISE_LINK_TEXTS_BUILTIN, date_to_iso, parse_html, quality_warnings, text_date


class SearchFormParser(HTMLParser):
    """轻量探测页面中的 GET 搜索表单（如 ?keyword=xx），JS/POST 表单忽略。"""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.found: dict | None = None
        self._form: dict | None = None
        self._form_depth = 0

    def handle_starttag(self, tag, attrs):
        low = tag.lower()
        attrs = dict(attrs)
        if low == "form":
            method = (attrs.get("method") or "get").lower()
            if self._form is None:
                self._form = {"method": method, "action": attrs.get("action") or "",
                              "inputs": []}
                self._form_depth = 1
            else:
                self._form_depth += 1
            return
        if self._form is None:
            return
        if low == "input":
            itype = (attrs.get("type") or "text").lower()
            if itype in {"text", "search"} and attrs.get("name"):
                self._form["inputs"].append(attrs["name"])
        elif low == "button" and "submit" in (attrs.get("type") or "").lower():
            self._form["inputs"].append("submit")

    def handle_endtag(self, tag):
        if tag.lower() != "form" or self._form is None:
            return
        self._form_depth -= 1
        if self._form_depth == 0:
            if (self._form["method"] == "get" and self._form["action"]
                    and self._form["inputs"]):
                self.found = {
                    "action": urljoin(self.base_url, self._form["action"]),
                    "input_name": self._form["inputs"][0],
                }
            self._form = None

# 从医院主页下钻时，按链接文本命中这些关键词找公告栏目（注意：不要用采集器的
# noise_link_texts 来筛栏目，那里把“通知公告/招标公告”当噪声，会误删目标栏目）。
COLUMN_KEYWORDS = (
    "招标采购", "采购公告", "招标公告", "招标信息", "采购信息", "招投标", "招标",
    "采购", "通知公告", "公告公示", "公示公告", "通知通告", "信息公告", "公告",
    "通知", "公示", "信息公开", "院务公开", "政务公开",
)
# 找栏目时使用的轻量噪声词（导航/联系类），与采集噪声分开维护。
DISCOVER_NOISE = {
    "首页", "网站首页", "本站首页", "联系我们", "网站地图", "登录", "注册",
    "友情链接", "相关链接", "院长信箱", "医院简介", "医院概况", "English", "EN",
}

PAGER_TEXT_RE = re.compile(r"^(?:\d{1,3}|下一页|下页|末页|尾页|Next|next)$")
PAGER_MAX = 5
SAMPLE_TITLE_MAX = 40


def discover_columns(body: str, base_url: str, max_columns: int = 3,
                     keywords: tuple[str, ...] = COLUMN_KEYWORDS) -> list[dict]:
    """从页面正文中找公告栏目链接：链接文本命中关键词越多越靠前，同 host 去重。

    排除“公告详情页”：标题过长（>16 字）或标题带日期的，基本是公告正文而非栏目名，
    避免把详情链接当成栏目候选。
    """
    parser = NoticeLinkParser(base_url, same_host_only=True)
    parser.feed(body)
    scored: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for link in parser.links:
        title = link["title"].strip()
        url = link["url"]
        if len(title) < 3 or len(title) > 16 or title in DISCOVER_NOISE or url in seen:
            continue
        if date_to_iso(text_date(title)):  # 标题带日期 → 公告详情，非栏目。
            continue
        score = sum(1 for keyword in keywords if keyword in title)
        if score > 0:
            seen.add(url)
            scored.append((score, title, url))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"title": title, "url": url} for _, title, url in scored[:max_columns]]


# 政采/公共资源平台的栏目词更宽：除“公告/招标/采购”外还有“交易信息/结果/成交/中标”等。
GOV_COLUMN_KEYWORDS = (
    "采购公告", "招标公告", "招标信息", "采购信息", "招投标", "招标", "采购",
    "交易信息", "交易公告", "交易公开", "工程建设", "政府采购", "招标采购",
    "通知公告", "公告公示", "公示公告", "信息公告", "结果公告", "成交公告",
    "中标公告", "更正公告", "公告", "公示", "通知",
)


def detect_get_search_form(body: str, base_url: str) -> dict | None:
    """轻量探测站点自带的 GET 搜索表单（如 ?keyword=xx），供参考；JS/POST 表单忽略。"""
    try:
        parser = SearchFormParser(base_url)
        parser.feed(body)
        return parser.found
    except Exception:
        return None


def pagination_hints(links: list[dict], base_url: str) -> list[str]:
    """翻页线索：链接文本是纯页码（1/2/3）或“下一页/末页”等，返回原始 href 样本。"""
    hints: list[str] = []
    seen: set[str] = set()
    for link in links:
        text = link["title"].strip()
        url = link["url"]
        if url == base_url or url in seen or not PAGER_TEXT_RE.match(text):
            continue
        seen.add(url)
        hints.append(url)
        if len(hints) >= PAGER_MAX:
            break
    return hints


def assess_url(url: str, source_type: str, name: str, noise: set[str],
               js_probe: bool = False) -> dict:
    """实测一个候选 URL：可达性、静态可解析、列表形态、标题日期、分页线索。

    js_probe=True 时，对“静态不可用但可访问”的候选额外用系统 Edge 无头渲染探测，
    渲染后可解析则标记 js_render_usable（建议启用 mode: js）。
    """
    entry = {"source_type": source_type, "name": name, "url": url, "fetch_ok": False}
    try:
        final_url, content_type, body = fetch_public_url(url, timeout=20)
        entry.update({"fetch_ok": True, "final_url": canonical_url(final_url),
                      "content_type": content_type, "mode": "html"})
        parser = NoticeLinkParser(final_url, same_host_only=True)
        parser.feed(body)
        entry["pagination_hints"] = pagination_hints(parser.links, canonical_url(final_url))
        items, stats = parse_html(body, final_url, max_items=200, same_host_only=True,
                                  noise_link_texts=noise)
        entry["links_seen"] = stats["links_seen"]
        entry["noise_filtered"] = stats["noise_filtered"]
        entry["dated_count"] = sum(1 for item in items if item.get("published_iso"))
        # 示例标题优先取“带日期”或标题较长的，更像真实公告，而不是导航链接。
        ranked = sorted(items, key=lambda item: (bool(item.get("published_iso")), len(item["title"])), reverse=True)
        entry["sample_titles"] = [item["title"][:SAMPLE_TITLE_MAX] for item in ranked[:5]]
        entry["warnings"] = quality_warnings(items, stats)
        # 站点自带 GET 搜索表单探测（供参考；JS/POST 搜索不计）。
        entry["search_form"] = detect_get_search_form(body, final_url)
        # “可用”= 可访问且像真实公告列表（条目数足够、无告警）；宁严勿宽，
        # 存疑的标“人工关注”让 AI/用户复核，避免“全部确认”启用到无效页面。
        entry["usable"] = bool(entry["fetch_ok"] and entry["links_seen"] >= 5 and not entry["warnings"])
        # JS 渲染兜底探测：静态不可用但可访问 → 用系统 Edge 渲染后若能解析出**带日期的
        # 公告标题**（rdated>0），标记 js_render_usable（建议启用 mode: js）。只有导航
        # 链接、无日期公告的平台不算可采，避免“全部确认”启用到无效页面；标题无日期的
        # 平台可由 AI/用户人工确认后启用。
        if js_probe and entry["fetch_ok"] and not entry["usable"]:
            try:
                _, _, rendered = render_public_url(url, timeout=50)
                ritems, rstats = parse_html(rendered, final_url, max_items=200,
                                            same_host_only=True, noise_link_texts=noise)
                rdated = sum(1 for item in ritems if item.get("published_iso"))
                if rstats["links_seen"] >= 5 and rdated > 0 and not quality_warnings(ritems, rstats):
                    entry["js_render_usable"] = True
                    entry["js_rendered_links"] = rstats["links_seen"]
                    entry["js_rendered_dated"] = rdated
                    entry["js_sample_titles"] = [item["title"][:SAMPLE_TITLE_MAX] for item in ritems[:3]]
            except Exception as error:
                entry["js_render_error"] = f"{type(error).__name__}: {str(error)[:80]}"
    except Exception as error:  # 单候选失败不中断，记录原因继续。
        entry["error"] = f"{type(error).__name__}: {error}"
        entry["warnings"] = ["采集失败，建议标注为“人工关注”"]
        entry["usable"] = False
    return entry


def province_platforms(province: str, table: dict) -> list[dict]:
    """取省份候选；省份表未覆盖时回退国家级平台。"""
    provinces = table.get("provinces", {})
    for key, value in provinces.items():
        if key in province or province in key:
            return value
    return table.get("national", [])


def format_summary(hospital: dict, official: list[dict], gov: list[dict]) -> str:
    usable = sum(1 for c in list(official) + list(gov) if c.get("usable"))
    js_usable = sum(1 for c in list(official) + list(gov) if c.get("js_render_usable"))
    flag = "需AI搜索主页" if hospital.get("need_search") else ""
    js_note = f"/JS渲染可采{js_usable}" if js_usable else ""
    if usable > 0:
        return f"{hospital['official_name']}{flag}: 可启用来源{usable}个{js_note}"
    return f"⚠️全挂 {hospital['official_name']}{flag}: 所有候选均不可用，需人工处理"


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover & live-verify hospital notice sources (one-time, low-frequency).")
    parser.add_argument("--hospitals", required=True, help="Hospitals input JSON (see templates/hospitals-input.example.json).")
    parser.add_argument("--data-dir", required=True, help="User-owned monitor data directory.")
    parser.add_argument("--platforms", help="Province platform table; default: <skill>/references/gov-platforms.json")
    parser.add_argument("--max-columns", type=int, default=3, help="Max announcement columns found per hospital (default 3).")
    parser.add_argument("--delay-seconds", type=float, default=0.5, help="Delay between requests (default 0.5; keep >=1 for daily runs).")
    parser.add_argument("--js-probe", action="store_true",
                        help="实验性：对静态不可用的候选用系统浏览器无头渲染探测（实测多数政采平台无效，默认关闭）。")
    args = parser.parse_args()
    if args.delay_seconds < 0.2:
        parser.error("--delay-seconds 不得低于 0.2 秒")
    if not (1 <= args.max_columns <= 6):
        parser.error("--max-columns 需在 1~6 之间")
    if args.platforms:
        table = load_json(args.platforms)
    else:
        default = Path(__file__).resolve().parent.parent / "references" / "gov-platforms.json"
        table = load_json(default)

    raw_hospitals = load_json(args.hospitals)
    # 兼容两种输入：顶层数组，或 {"hospitals": [...]}（见 templates/hospitals-input.example.json）。
    hospitals = raw_hospitals if isinstance(raw_hospitals, list) else raw_hospitals.get("hospitals", [])
    if not hospitals:
        parser.error("医院清单为空；请提供带 id/official_name/province/city 的医院数组。")
    data_dir = ensure_data_dir(args.data_dir)
    noise = set(NOISE_LINK_TEXTS_BUILTIN)
    per_hospital: list[dict] = []
    summary: list[str] = []

    for index, hospital in enumerate(hospitals):
        hospital_id = str(hospital.get("id", f"h{index}"))
        name = str(hospital.get("official_name", hospital_id))
        province = str(hospital.get("province", ""))
        homepage = str(hospital.get("homepage") or hospital.get("website") or "").strip()
        entry = {"hospital_id": hospital_id, "official_name": name, "province": province,
                 "city": str(hospital.get("city", "")), "homepage": homepage,
                 "need_search": False, "official_candidates": [], "gov_candidates": []}
        official_candidates: list[dict] = []

        if homepage:
            try:
                final_url, _, body = fetch_public_url(homepage, timeout=20)
                base = canonical_url(final_url)
                path = urlparse(base).path
                # 用户给的是“公告直达页”（路径非首页）时，直接把该页作为官网候选之一。
                if path not in ("", "/"):
                    official_candidates.append({"title": "用户/搜索提供的直达地址", "url": base})
                for column in discover_columns(body, base, args.max_columns):
                    if all(c["url"] != column["url"] for c in official_candidates):
                        official_candidates.append(column)
            except Exception as error:
                entry["homepage_error"] = f"{type(error).__name__}: {error}"
                entry["need_search"] = True
        else:
            entry["need_search"] = True  # 无主页：需 AI 至多 1 次搜索，或用户补提供。

        # 官网候选实测（请求数 ≤ 4/家）。
        seen_urls: set[str] = set()
        for candidate in official_candidates[: 1 + args.max_columns]:
            url = candidate["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            assessed = assess_url(url, "hospital_official", f"官网-{candidate['title']}", noise,
                                  js_probe=args.js_probe)
            entry["official_candidates"].append(assessed)
            time.sleep(max(0.2, args.delay_seconds))

        # 省平台候选：先抓平台首页（门户），下钻找静态公告栏目，再连同栏目一起实测。
        # 政采/公共资源平台首页多为 JS 门户，栏目页（老式平台）可能是静态列表——这也是
        # “用 Python 定位公告查询页面”的实现。每家平台候选 ≤ 2，栏目下钻 ≤ 2，请求数可控。
        gov_platforms = province_platforms(province, table)[:2]
        gov_urls: list[dict] = []
        for platform in gov_platforms:
            gov_urls.append({"title": platform["name"], "url": platform["url"],
                             "source_type": platform["source_type"], "origin": "platform_home"})
        for platform in gov_platforms:
            try:
                final_url, _, body = fetch_public_url(platform["url"], timeout=20)
                for column in discover_columns(body, canonical_url(final_url), 2, GOV_COLUMN_KEYWORDS):
                    if all(c["url"] != column["url"] for c in gov_urls):
                        gov_urls.append({"title": column["title"], "url": column["url"],
                                         "source_type": platform["source_type"],
                                         "origin": "discovered_column"})
                time.sleep(max(0.2, args.delay_seconds))
            except Exception as error:
                entry.setdefault("gov_notes", []).append(
                    f"{platform['name']} 栏目下钻失败: {type(error).__name__}: {error}")
        for candidate in gov_urls[:4]:
            assessed = assess_url(candidate["url"], candidate["source_type"],
                                  f"{candidate['title']}", noise, js_probe=args.js_probe)
            assessed["origin"] = candidate.get("origin", "platform_home")
            entry["gov_candidates"].append(assessed)
            time.sleep(max(0.2, args.delay_seconds))

        # 汇总每家医院的可启用来源数（含 JS 渲染可采），用于“只报全挂医院”的呈现。
        all_candidates = entry["official_candidates"] + entry["gov_candidates"]
        entry["usable_source_count"] = sum(1 for c in all_candidates if c.get("usable"))
        entry["js_usable_source_count"] = sum(1 for c in all_candidates if c.get("js_render_usable"))
        entry["has_usable_source"] = entry["usable_source_count"] > 0

        per_hospital.append(entry)
        summary.append(format_summary(entry, entry["official_candidates"], entry["gov_candidates"]))
        print(f"[discover] {summary[-1]}", file=sys.stderr)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = write_json(data_dir / "work" / f"source-assessment-{stamp}.json",
                        {"generated_at": utc_now(), "summary": summary, "per_hospital": per_hospital})
    print(output)
    print(f"实测完成：{len(hospitals)} 家医院；每家官网候选 ≤ {1 + args.max_columns} 条 + "
          f"省平台候选 ≤ 4 条（含下钻栏目）。AI 请读 {output.name} 转成可用性评估表"
          f"（逐条一行摘要）请用户确认。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

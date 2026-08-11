#!/usr/bin/env python3
"""Batch look up hospital official websites via Amap POI search (optional accelerator).

一次性新增较多医院时，用高德开放平台「关键字搜索 POI」按医院全名查询官网，
自动把结果填入 hospitals[].homepage，输出文件可直接作为 discover_sources.py 的输入，
实现批量新增医院「零 AI 搜索」。

没有 Key 时不使用本脚本：默认通道由 AI 对缺官网的医院逐家至多 1 次搜索补全（用户零操作），
见 SKILL.md「信息源整理三步」。

用法：
  export AMAP_KEY=你的高德Web服务Key
  python scripts/lookup_homepages.py --hospitals <医院清单.json> --output <补全后.json>
或直接传参：
  python scripts/lookup_homepages.py --hospitals <医院清单.json> --key <KEY> --output <补全后.json>

说明：
- 仅对 homepage 为空的医院发起查询；已有 homepage 的直接跳过。
- 接口：https://restapi.amap.com/v3/place/text ，参数 keywords=医院全名、city=医院所在市、
  citylimit=true、extensions=all、offset=1（website 字段需 extensions=all 才返回）。
- POI 名称与医院全名/别名做包含校验后才采用 website，避免配错医院。
- 未返回 website 的医院保持 homepage 为空，summary 中标出，由 AI 补搜或用户补提供。
- 请求间隔默认 0.3 秒，避免触发限流（个人开发者 Key 默认 QPS 较小）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

AMAP_URL = "https://restapi.amap.com/v3/place/text"


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


def name_matches(poi_name: str, hospital_names: list[str]) -> bool:
    """POI 名与医院全名/别名做包含校验；名字过短（<4 字）不采用，避免误配。"""
    poi = normalize_name(poi_name)
    if not poi:
        return False
    for candidate in hospital_names:
        candidate = normalize_name(candidate)
        if len(candidate) < 4:
            continue
        if candidate in poi or poi in candidate:
            return True
    return False


def normalize_website(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def lookup_website(key: str, official_name: str, city: str, timeout: int = 10) -> tuple[str, str, str]:
    """查询单个医院官网。返回 (website, poi_name, poi_address)。查不到时 website 为空。"""
    params = {
        "key": key,
        "keywords": official_name,
        "extensions": "all",
        "offset": "1",
        "page": "1",
        "output": "json",
    }
    if city:
        params["city"] = city
        params["citylimit"] = "true"
    url = AMAP_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "HospitalProcurementMonitor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if str(payload.get("status")) != "1":
        return "", "", f"接口返回异常: {payload.get('info', payload)}"
    names = [official_name]
    for poi in payload.get("pois", [])[:5]:
        poi_name = str(poi.get("name", ""))
        if name_matches(poi_name, names):
            return normalize_website(poi.get("website", "")), poi_name, str(poi.get("address", ""))
    # 前 5 条都未与医院名匹配：避免配错，返回空。
    first = payload.get("pois", [])
    if first:
        return "", "", f"无名称匹配的 POI（最近似: {first[0].get('name', '')}）"
    return "", "", "未查询到 POI"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch look up hospital websites via Amap POI (optional).")
    parser.add_argument("--hospitals", required=True, help="Hospitals input JSON (same schema as discover_sources.py).")
    parser.add_argument("--output", required=True, help="Output JSON with homepage filled; feed to discover_sources.py.")
    parser.add_argument("--key", default=os.environ.get("AMAP_KEY", ""), help="Amap Web Service key (or set AMAP_KEY).")
    parser.add_argument("--delay-seconds", type=float, default=0.3, help="Delay between requests (default 0.3).")
    args = parser.parse_args()
    if not args.key:
        print("未配置高德 Key。请先在 https://lbs.amap.com 注册开发者并申请 Web 服务 Key，"
              "再设置环境变量 AMAP_KEY 或传 --key。\n没有 Key 时无需本脚本：直接把医院名单交给 AI，"
              "AI 会逐家至多 1 次搜索自动补齐官网地址。", file=sys.stderr)
        return 2

    raw = json.load(open(args.hospitals, encoding="utf-8"))
    hospitals = raw if isinstance(raw, list) else raw.get("hospitals", [])
    if not hospitals:
        parser.error("医院清单为空。")
    filled, skipped, missing = 0, 0, 0
    lookup_notes = []
    for index, hospital in enumerate(hospitals):
        official = str(hospital.get("official_name", ""))
        if not official:
            continue
        if hospital.get("homepage"):
            skipped += 1
            continue
        city = str(hospital.get("city", ""))
        website, poi_name, note = lookup_website(args.key, official, city)
        if website:
            hospital["homepage"] = website
            hospital["_homepage_source"] = "amap_poi"
            filled += 1
            lookup_notes.append(f"{official} → {website}（POI: {poi_name}）")
        else:
            missing += 1
            lookup_notes.append(f"{official} ← 未补全（{note}）")
        print(f"[lookup] {lookup_notes[-1]}", file=sys.stderr)
        if index < len(hospitals) - 1:
            time.sleep(max(0.2, args.delay_seconds))

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(args.output)
    print(f"官网补全完成：填充 {filled}，跳过（已有）{skipped}，未补全 {missing}。"
          f"请把 {args.output} 交给 discover_sources.py 继续实测；未补全的医院由 AI 补搜或用户补提供。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

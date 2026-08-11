#!/usr/bin/env python3
"""Record AI-reviewed candidates in SQLite and export a user-facing report.

v1.0 enhancements:
- B3: candidates judged irrelevant (or left unannotated) are persisted to the
  skipped_notices table so they are not re-sent for review on every run.
  Skipped records are never exported into lead reports.
- B4: candidates whose canonical URL is already a lead are not inserted again
  (same URL keeps the first record).
- W7: annotations with "update": true update status/fields of an existing lead
  instead of being ignored.
- W9: fixed the double-Workbook bug in the xlsx export.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from monitor_common import canonical_url, create_tables, ensure_data_dir, load_json, utc_now

FIELDS = ["hospital_name", "title", "notice_type", "project_type", "matched_keywords",
          "ai_reason", "published_text", "collected_at", "source_name", "source_type", "url", "budget",
          "deadline", "status", "notes", "notice_id", "recorded_at"]

# 导出表头使用中文；内部字段名保持英文，以保证去重主键与查询逻辑稳定。
CHINESE_HEADERS = ["医院名称", "公告标题", "公告类型", "项目类型", "命中关键词",
                   "智能判断依据", "发布日期", "采集时间", "信息来源", "来源类型", "公告链接", "预算",
                   "截止日期", "状态", "备注", "公告ID", "入库时间"]

UPDATABLE_FIELDS = ("hospital_name", "notice_type", "project_type", "ai_reason",
                    "budget", "deadline", "status", "notes")


def database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    create_tables(connection)
    return connection


def export_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CHINESE_HEADERS)
        writer.writerows([[row.get(field, "") for field in FIELDS] for row in rows])


def export_xlsx(rows: list[dict], path: Path) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError:
        return False
    # W9: 只创建一个 Workbook（旧版本误建两个，且引用了错误工作簿的 sheet）。
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "采购线索"
    sheet.append(CHINESE_HEADERS)
    for row in rows:
        sheet.append([row.get(field, "") for field in FIELDS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(max(12, max(len(str(cell.value or "")) for cell in column) + 2), 42)
        for cell in column:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist AI-reviewed procurement leads and export reports.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--candidates", required=True, help="candidates JSON emitted by prepare_candidates.py")
    parser.add_argument("--annotations", required=True, help="Human/AI reviewed annotation JSON array (may be [])")
    parser.add_argument("--report-name", default="procurement-leads")
    args = parser.parse_args()
    data_dir, candidates = ensure_data_dir(args.data_dir), load_json(args.candidates)
    annotation_list = load_json(args.annotations)
    if not isinstance(annotation_list, list):
        print("标注文件必须是 JSON 数组（没有标注时请传空数组 []）", file=sys.stderr)
        return 2
    annotations = {item["notice_id"]: item for item in annotation_list
                   if isinstance(item, dict) and item.get("notice_id")}

    connection = database(data_dir / "monitor.sqlite3")
    existing_lead_ids = {row[0] for row in connection.execute("SELECT notice_id FROM leads")}
    existing_skipped_ids = {row[0] for row in connection.execute("SELECT notice_id FROM skipped_notices")}
    lead_urls = {canonical_url(url) for (url,) in connection.execute("SELECT url FROM leads WHERE url != ''")}

    rows, skipped_rows, now = [], [], utc_now()
    updated, not_annotated, duplicate_url = 0, 0, 0
    update_missed: list[str] = []
    # W7: 更新类标注独立生效——已入库线索会被 prepare_candidates 过滤出候选，
    # 因此更新不能依赖候选文件，这里直接按标注中的 notice_id 更新 leads。
    for annotation in annotations.values():
        if not annotation.get("update"):
            continue
        assignments, params = [], {"notice_id": annotation["notice_id"]}
        for key in UPDATABLE_FIELDS:
            if key in annotation:
                assignments.append(f"{key} = :{key}")
                params[key] = annotation[key]
        if assignments:
            count = connection.execute(
                f"UPDATE leads SET {', '.join(assignments)} WHERE notice_id = :notice_id",
                params).rowcount
            updated += count
            if count == 0:
                update_missed.append(annotation["notice_id"][:16])
    for candidate in candidates.get("candidates", []):
        annotation = annotations.get(candidate["notice_id"])
        if annotation and annotation.get("update"):
            continue
        if not annotation:
            not_annotated += 1
            skipped_rows.append({
                "notice_id": candidate["notice_id"], "url": candidate.get("url", ""),
                "title": candidate.get("title", ""), "source_name": candidate.get("source_name", ""),
                "skip_reason": "未标注（本批候选未经审阅，如需复核请从 skipped_notices 移除后重跑）",
                "ai_reason": "", "skipped_at": now,
            })
            continue
        if not annotation.get("relevant", True):
            skipped_rows.append({
                "notice_id": candidate["notice_id"], "url": candidate.get("url", ""),
                "title": candidate.get("title", ""), "source_name": candidate.get("source_name", ""),
                "skip_reason": "AI判断不相关", "ai_reason": annotation.get("ai_reason", ""), "skipped_at": now,
            })
            continue
        url_key = canonical_url(candidate.get("url", ""))
        # B4: 同一 canonical URL 只保留首条线索。
        if url_key and url_key in lead_urls:
            duplicate_url += 1
            continue
        evidence = candidate.get("evidence", {})
        hospital_name = annotation.get("hospital_name") or candidate.get("hospital_name", "")
        rows.append({
            "hospital_name": hospital_name, "title": candidate.get("title", ""),
            "notice_type": annotation.get("notice_type", ""), "project_type": annotation.get("project_type", ""),
            "matched_keywords": "; ".join(evidence.get("required_keywords", []) + evidence.get("synonym_keywords", [])),
            "ai_reason": annotation.get("ai_reason", ""), "published_text": candidate.get("published_text", ""),
            "collected_at": candidate.get("collected_at", ""), "source_name": candidate.get("source_name", ""),
            "source_type": candidate.get("source_type", ""), "url": candidate.get("url", ""),
            "budget": annotation.get("budget", ""), "deadline": annotation.get("deadline", ""),
            "status": annotation.get("status", "新线索"), "notes": annotation.get("notes", ""),
            "notice_id": candidate["notice_id"], "recorded_at": now,
        })
        if url_key:
            lead_urls.add(url_key)

    connection.executemany("""INSERT OR IGNORE INTO leads
        (notice_id, hospital_name, title, notice_type, project_type, matched_keywords, ai_reason,
         published_text, collected_at, source_name, source_type, url, budget, deadline, status, notes, recorded_at)
        VALUES (:notice_id, :hospital_name, :title, :notice_type, :project_type, :matched_keywords, :ai_reason,
         :published_text, :collected_at, :source_name, :source_type, :url, :budget, :deadline, :status, :notes, :recorded_at)""", rows)
    # B3: 不相关/未标注候选持久化到 skipped_notices，终止“每轮重复送审”循环。
    connection.executemany("""INSERT OR IGNORE INTO skipped_notices
        (notice_id, url, title, source_name, skip_reason, ai_reason, skipped_at)
        VALUES (:notice_id, :url, :title, :source_name, :skip_reason, :ai_reason, :skipped_at)""", skipped_rows)
    connection.commit()
    new_count = sum(1 for row in rows if row["notice_id"] not in existing_lead_ids)
    new_skipped = sum(1 for row in skipped_rows if row["notice_id"] not in existing_skipped_ids)
    all_rows = [dict(zip(FIELDS, row)) for row in connection.execute("SELECT " + ",".join(FIELDS) + " FROM leads ORDER BY recorded_at DESC")]
    connection.close()

    reports = data_dir / "reports"
    csv_path = reports / f"{args.report_name}.csv"
    export_csv(all_rows, csv_path)
    xlsx_path = reports / f"{args.report_name}.xlsx"
    made_xlsx = export_xlsx(all_rows, xlsx_path)
    print(xlsx_path if made_xlsx else csv_path)
    print(f"本次新增线索：{new_count} 条（共入库 {len(all_rows)} 条）")
    if updated:
        print(f"已更新既有线索字段/状态：{updated} 条")
    if update_missed:
        print(f"[warn] {len(update_missed)} 条 update 标注未命中任何已有线索（notice_id 前 16 位："
              f"{', '.join(update_missed[:5])}{'…' if len(update_missed) > 5 else ''}），请核对 ID", file=sys.stderr)
    if skipped_rows:
        print(f"已记入 skipped：新增 {new_skipped} 条（未标注 {not_annotated}，AI判断不相关 "
              f"{len(skipped_rows) - not_annotated}）；skipped 记录不会出现在线索报告中")
    if not_annotated:
        # 常见笔误：标注里的 notice_id 被截断（前 16 位），导致静默按“未标注”处理。
        annotation_ids = {item["notice_id"] for item in annotation_list if isinstance(item, dict) and item.get("notice_id")}
        truncated = [c["notice_id"] for c in candidates.get("candidates", [])
                     if c["notice_id"] not in annotation_ids
                     and c["notice_id"][:16] in {a[:16] for a in annotation_ids}]
        if truncated:
            print(f"[warn] 检测到 {len(truncated)} 条标注的 notice_id 与候选前缀相同但长度不同"
                  f"（疑似截断），请使用 candidates 中的完整 64 位 ID 重新标注", file=sys.stderr)
    if duplicate_url:
        print(f"按 URL 去重跳过：{duplicate_url} 条（同一公告已有线索记录）")
    if not made_xlsx:
        print("未安装 openpyxl，已导出 CSV；如需 Excel 请安装 requirements.txt 中的可选依赖。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

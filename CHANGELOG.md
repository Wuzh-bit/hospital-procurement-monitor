# 更新日志

## v1.0.0 — 2026-08-11（首次公开发布）

首个公开发布版本。功能上承自 v0.3 的成熟实现，本次聚焦**可分发性、合规性与可维护性**：

### 新增
- 冒烟测试（`tests/`）：覆盖 URL 校验（SSRF 防护）、canonical 去重、notice_id 生成、日期解析、关键词匹配（词边界）等核心函数。
- `CHANGELOG.md`：版本历史与发布说明。

### 修复与整理
- **数据隔离**：`.gitignore` 新增 `monitor-data-fresh/` 与 `.workbuddy/`，确保真实医院清单、配置、缓存、数据库与开发日志绝不进入仓库。
- **版本统一**：所有脚本的 docstring 与 User-Agent 版本标识统一为 1.0（此前 README/脚本间存在 0.2/0.3 混用）；SKILL.md frontmatter 增加 `version: 1.0.0`。
- **清理内部痕迹**：`references/gov-platforms.json` 中"用户提供真实域名"等开发协作备注改写为中性的盘点实测说明。
- 清理本地 `__pycache__` 与临时探针脚本（不入库）。

### 兼容性
- 配置格式保持 `version: 2/3` 兼容，历史数据目录与既有 `monitor-config.json` 无需迁移。
- 采集、初筛、审阅、入库、通知、清理的完整流程与 v0.3 行为一致。

## v0.3 — 轻量化与防死循环

- 信息源收敛为两类（官网公告栏目 + 省/市政采或公共资源平台），删除代理机构自动发现。
- 新增 `scripts/discover_sources.py`：Python 直接实测官网栏目与省平台（含公告栏目下钻、GET 搜索表单探测），AI 不再逐页搜索实测。
- 新增 `scripts/lookup_homepages.py`（可选加速）：高德 POI 批量补官网，零 AI 搜索。
- 来源级关键词定向采集（`keyword_filter` + `keyword_early_stop_pages`）。
- JS 平台浏览器无头渲染（`mode: "js"`）：Playwright（可选）+ 系统浏览器 dump-dom 兜底。
- 可用性报告异常优先（只报全挂医院）+ 双来源默认同启用（交叉验证、审阅时合并）。
- 全网搜索刹车（单次查询必有结论、预算 ≤ 医院数 + 3）。
- 首次试运行 `--since` 近 7 天轻量化。

## v0.2 — 真实数据回测加固

- 采集器来源级分页（`pagination`，可随 `--since` 提前停止）；导航噪声自动过滤。
- 线索按 canonical URL 去重；判不相关/未标注候选持久化到 `skipped_notices` 避免重复送审。
- RSS/Atom 日期、GBK 编码、英文词边界匹配、xlsx 导出等健壮性修复。
- 已入库线索状态更新（`update: true`）；TLS/证书问题中文诊断。

## v0.1 — 初始版本

- 基础采集（HTML/RSS/Atom）、两级 AI 审阅、SQLite 台账与 CSV 导出、邮件通知、缓存清理。

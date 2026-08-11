# 配置字段

配置采用 UTF-8 JSON。复制 `templates/monitor-config.example.json` 后编辑，真实配置只能保存在用户数据目录。

## 顶层字段

| 字段 | 说明 |
|---|---|
| `monitor_name` | 用户可识别的任务名称。 |
| `timezone` | IANA 时区，例如 `Asia/Shanghai`。 |
| `schedule` | 频率及执行时间，仅在用户确认后用于创建宿主平台自动化。 |
| `notification` | 用户提供的收件地址；本轮 AI 判为相关的所有新增线索都会被发送，不含任何密码或令牌。 |
| `keywords` | 已确认的关键词规则。`required` 至少有一项。 |
| `hospitals` | 已确认的医院实体。 |
| `sources` | 已确认的信息源。 |

## 医院与来源

`hospitals[].id` 必须稳定且唯一；`official_name`、`province`、`city`、`aliases` 均经用户确认。`hospitals[].homepage`（可选）为用户提供或 AI 单次搜索得到的官网主页/公告直达页，仅供 `discover_sources.py` 发现信息源时使用，采集阶段不直接使用。未确认实体保留 `confirmed: false`，不能作为长期监测对象；中途停用某医院时也置为 `confirmed: false`（或其来源 `enabled: false`），已入库的历史线索不受影响。

`hospitals[].province` 用于在 `references/gov-platforms.json` 预置表中匹配省/市政府采购与公共资源交易平台，匹配不到时回退国家级平台（ccgp.gov.cn / ggzy.gov.cn）。

`sources[].type` 使用 `hospital_official`、`government_procurement`、`public_resources` 或 `agency`（`agency` 仅保留兼容历史已确认来源，**不再自动发现**）。`mode` 为 `html`、`rss`、`atom`、`feed` 或 `js`。首版 HTML 采集提取公告页内的可见链接，适合结构稳定的公告列表；复杂 JavaScript、搜索表单、验证码页面不应启用。RSS/Atom 是优先选择。

**`mode: "js"`（浏览器无头渲染）**：对公开、无需登录/验证码的 JS 列表页（如中国政府采购网、部分省级平台）使用浏览器无头渲染后采集。**已集成 Playwright（可选依赖，优先）**：`pip install playwright` 后自动使用，复用系统 Chrome/Edge（channel 方式，无需下载浏览器）；未安装时自动退回系统浏览器 `--dump-dom` 兜底（自动探测 Chrome/Edge/Firefox，`RENDER_BROWSER` 可指定）。已验证可解锁中国政府采购网（国家级，翻页 `index_{page}.htm` 适配 pagination）、北京/河南/江苏/海南等省平台。仅限公开页面，使用合理 UA，不填表、不点验证码、不绕过访问控制，限速不变，用完即关；渲染失败仍标“人工关注”。**Safari 不支持命令行无头渲染**。默认不开启，由来源确认时按 `js_render_usable` 标记或预置表 ok 标注启用。

只有同时为 `confirmed: true` 和 `enabled: true` 的来源才会被采集。默认 `same_host_only: true`，避免公告列表中的外部链接被当成采集目标。

### 分页（pagination）

多页静态列表优先为单一来源配置分页，而不是把每一页注册成独立来源：

| 字段 | 说明 |
|---|---|
| `sources[].pagination.url_template` | 翻页 URL 模板，必须包含 `{page}` 占位符，例如 `https://example.org/list-113-{page}.html`。 |
| `sources[].pagination.first_page` | 起始页码，默认 2（`sources[].url` 视为第 1 页）。 |
| `sources[].pagination.max_pages` | 最多追加采集的页数；达到即停。页面为空、与已有内容完全重复，或整页日期均早于 `--since` 时也会提前停止。 |
| `sources[].pagination.step` | 页码增量，默认 1。offset 分页站点（如 `offset/10`、`offset/20`…）可设 `step: 10` 与对应 `first_page`。 |
| `sources[].pagination.descending` | 是否递减翻页，默认 false。页码从 `first_page` 开始递减，适配“旧页在前”（如列表第 2 页是 `…/21.htm`、第 3 页是 `…/20.htm`）的站点；递减到小于 1 时停止。 |

翻页请求与来源之间共用同一限速（`--delay-seconds`，最低 1 秒）。某一页采集失败时保留已采数据并在来源结果的 `pagination.page_errors` 中记录。

此外，采集器会自动过滤“短标题 + 列表形态 URL”的栏目导航链接（例如指向 `/cat/123`、`list-113.html`、`index_2.shtml`、`/node/35` 且标题少于 8 个字符的链接），避免栏目名占用送审配额。

### 关键词定向采集（keyword_filter，可选）

针对公告量巨大的来源（如省/市公共资源交易平台聚合页、政采平台栏目页），可在采集阶段按关键词定向，减少抓取范围：

| 字段 | 说明 |
|---|---|
| `sources[].keyword_filter` | 关键词列表。启用后，**只有标题命中任一关键词的公告才会保留**并落盘；未命中的直接丢弃并计数（结果中 `keyword_filter.filtered_count`）。不配置则全量采集（默认行为）。 |
| `sources[].keyword_early_stop_pages` | 连续 N 页零命中关键词时提前停止翻页（默认 0 = 按 1 页处理，即首个零命中页即停）。适合关键词公告分散、列表按时间倒序的大平台。 |

注意：`keyword_filter` 与全局 `keywords` 语义不同——前者在**采集阶段丢弃**未命中公告（不落盘、不可追溯），后者在**初筛阶段**保留未命中记录供追踪。因此 keyword_filter 只应给“公告量巨大、几乎不可能漏报”的政采/公共资源平台使用；医院官网等小来源保持全量采集，靠全局关键词初筛即可。启用前请与用户确认。

## 关键词规则

- `required`：用户明确希望跟踪的项目词，至少一个。
- `synonyms`：由 AI 建议、用户确认的别名/缩写/近义表达。
- `exclude`：出现时应排除的词；应避免过于宽泛。
- `priority_notice_types`：命中项目词后可提高优先级的公告类型。
- `noise_link_texts`（可选）：额外的导航/板块链接文本黑名单，命中即跳过采集，作为结构识别（nav/header/footer）之外的兜底去噪。规范位置在 `keywords` 下；为兼容旧配置，顶层同名配置也会被采集器读取并合并。

匹配规则：非 ASCII 关键词（中文等）按子串匹配；纯 ASCII 关键词（如 `CT`、`HIS`）按词边界匹配，避免命中 `doctor`、`historical` 等单词内部造成误报。

规则引擎不会从互联网自动改写这些字段。每次调整均应记录调整人、日期和理由（可写入台账备注）。

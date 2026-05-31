# Auto Paper Pipeline — 自动论文发现与审阅流水线

## 产品定位

一个编排器型 Skills。用户输入领域关键词后，自动完成：多源搜索 → 打分排序 → 筛选 Top N → 批量下载 → 结构化审阅 → 生成 Obsidian 笔记。不直接抓论文、不直接写报告，而是指挥 paper-fetch-skill 和 paper-obsidian-review 完成各自擅长的任务。

## 目标用户

需要系统性地追踪某个研究方向的科研人员。典型场景：进入新领域做文献调研、写基金申请需要文献支撑、定期追踪领域最新进展。

## 核心原则

1. **轻编排、重委托** — 自己只做搜索和打分，下载和审阅全委托已有 skill
2. **用户可见、可控** — 每个阶段结束展示中间结果，用户确认后再进入下一阶段
3. **可调权重** — 打分公式的参数写在单独 YAML 文件里，用户修改后立即生效
4. **领域可扩展** — 审阅模板引用 paper-obsidian-review 后追加领域专属补充
5. **容错继续** — 单篇论文的下载/审阅失败不阻塞整条流水线

---

## 五阶段流水线

```
用户输入关键词
    ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 1：参数确认（AI 对话，无脚本）                             │
│   收集：领域关键词、排除关键词、关键词权重、语言偏好             │
│   收集：时间范围、偏好期刊、返回数量 N、论文类型偏好             │
│   收集：保存目录、vault 根目录、图片资源偏好（供阶段 4 直传）   │
│   AI 负责将用户关键词翻译为英文查询词，记录到 search_queries    │
└──────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 2：多源搜索（scripts/search_papers.py）                    │
│   并行查询 Semantic Scholar + Crossref + arXiv + bioRxiv      │
│   可选：PubMed（生命科学领域推荐开启）                          │
│   输出：search_results.json（去重后）                           │
│   报告：各来源数量 + 去重统计 + 摘要覆盖率                     │
└──────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 3：打分排序（scripts/score_papers.py）                     │
│   3a. 预筛：排除关键词命中排除词、类型不符、最低关键词命中数    │
│   3b. 综合评分 + 排序 + 截断 Top N                             │
│   输出：top_n_dois.json（Top N + 分数明细）                     │
│   展示给用户确认/调整                                          │
└──────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────┐
│ 阶段 4：批量下载（通过 MCP 工具直调 fetch_paper）   │
│   遍历 Top N，逐篇调用 MCP fetch_paper 下载 Markdown │
│   输出：papers/fulltext/ 下的 Markdown 全文           │
│   进度报告：每下载 3 篇报告一次                      │
│   失败处理：记录失败条目，不阻塞后续论文             │
└──────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────┐
│ 阶段 5：结构化审阅（按合并模板直接生成）            │
│   逐篇按审阅模板 + 领域补充生成 Obsidian 笔记        │
│   生成总览对比笔记                                  │
│   输出：papers/reviews/ 下的 .md 笔记文件              │
└──────────────────────────────────────────────────┘
```

---

## 组件协作机制

> 本节明确 pipeline 与 paper-fetch-skill、paper-obsidian-review 的代码级协作方式。

### 阶段 4：批量下载 — 绕过 skill BLOCKING

paper-fetch-skill 的 SKILL.md 有 ⛔ BLOCKING 规则（逐篇确认保存方式、≥3 篇建议 CLI），不适用于 pipeline 的自动批量场景。解决方案：

**pipeline 直接调用 MCP 工具 `mcp__paper-fetch__fetch_paper`，绕过 skill 指令层的 BLOCKING 检查。**

#### 前置依赖版本声明

pipeline 对 paper-fetch-skill 的 MCP 接口有语义耦合，需在 SKILL.md 头部和 `pipeline_params.json` 中声明最低版本要求：

```yaml
# pipeline_params.json 中的依赖声明
"requires": {
  "paper-fetch-skill": ">=0.1.0"
}
```

若 paper-fetch-skill 升级导致 MCP 接口签名变更，pipeline 应报错并提示用户升级，而非静默失败。

#### 阶段 4 启动前：MCP 健康检查

在进入批量下载前，必须先确认 MCP server 可用：

```
步骤 1：调用 mcp__paper-fetch__provider_status() 确认 MCP server 健康
  - 若返回正常 → 继续步骤 2
  - 若调用失败或返回异常 → 进入降级方案（见下方）
```

#### 正常流程：MCP 批量下载

阶段 1 已收集用户的保存偏好，阶段 4 将这些偏好作为 MCP 参数直接传入：

```
调用示例：
mcp__paper-fetch__fetch_paper(
    query = "<DOI>",
    save_markdown = True,
    markdown_output_dir = "<vault>/papers/fulltext/",
    markdown_filename = "<safe_filename>.md",
    strategy = {
        "asset_profile": "body",       # 下载正文图片，不含补充材料
        "allow_metadata_only_fallback": True
    },
    artifact_mode = "markdown-assets",
    prefer_cache = True                 # 优先使用缓存，避免重复下载
)
```

这样既满足了 paper-fetch-skill 用户的保存需求（在阶段 1 已确认），又不会在阶段 4 被逐篇 BLOCKING。

#### 降级方案：MCP 不可用时回退 CLI

当 MCP 健康检查失败或批量下载过程中连续报错（如 MCP server 崩溃），自动降级为 CLI 方式：

```bash
# 生成 query-file（每行一个 DOI）
cat top_n_dois.json | python -c "
import json,sys
d=json.load(sys.stdin)
for p in d['papers']:
    print(p.get('doi') or p.get('arxiv_id',''))
" > /tmp/pipeline-queries.txt

# 调用 paper-fetch CLI 批量下载
paper-fetch --query-file /tmp/pipeline-queries.txt \
    --output-dir <vault>/papers/fulltext/ \
    --batch-concurrency 2
```

降级触发条件：
1. MCP `provider_status()` 调用失败（server 未启动或超时）
2. 连续 3 篇论文的 `fetch_paper` 调用返回相同的 MCP 连接错误（非论文本身的无全文等业务错误）
3. 降级后向用户报告："MCP 不可用，已切换为 CLI 模式下载"

降级不适用的情况：若 CLI 也未安装（`paper-fetch` 命令不存在），则暂停阶段 4，向用户报告并建议安装 paper-fetch-skill。

### 阶段 5：结构化审阅 — 引用式委托

paper-obsidian-review **不是可调用的 API 或 MCP 服务**，它是一份 Skill 指令文档（SKILL.md），本质是 prompt 模板。解决方案：

**pipeline SKILL.md 在 `references/` 下引用 paper-obsidian-review 的模板结构，AI 按合并后的模板直接生成笔记。**

具体做法：
1. pipeline SKILL.md 的阶段 5 指令中，引用 paper-obsidian-review 的单篇笔记结构和对比笔记结构
2. 追加 `references/domain-supplement.yaml` 中当前领域的额外字段
3. AI 读取论文 Markdown 全文后，按合并模板一次性生成 Obsidian 笔记

**不复制模板内容**，而是在 SKILL.md 中写明"读取 paper-obsidian-review 的 SKILL.md 获取标准结构"，避免模板重复导致维护不同步。

---

## 文件结构

```
auto-paper-pipeline/
├── SKILL.md                         # 编排指令
│
├── references/
│   ├── scoring-rules.yaml           # 打分公式参数（程序化读取）
│   ├── scoring-rules.md             # 打分公式说明（人类可读）
│   ├── search-apis.md               # 搜索 API 接口文档
│   ├── domain-supplement.yaml       # 领域审阅补充模板（多领域，YAML 格式）
│   └── journal-tiers.yaml           # 期刊分层映射表
│
└── scripts/
    ├── search_papers.py             # 多源搜索
    ├── score_papers.py              # 打分排序
    └── requirements.txt             # httpx, PyYAML（最少依赖）
```

依赖数量：2 个 pip 包，不依赖浏览器、LaTeX、conda。

---

## Python 脚本 CLI 接口

### search_papers.py

```bash
# 基本用法
python search_papers.py \
    --keywords "plant genome foundation model" \
    --year-from 2022 \
    --year-to 2026 \
    --max-results 100 \
    --output search_results.json

# 多关键词（逗号分隔，各自独立查询后合并）
python search_papers.py \
    --keywords "plant genome foundation model,plant LLM,gene discovery deep learning" \
    --year-from 2022 \
    --max-results 100 \
    --output search_results.json

# 指定搜索源
python search_papers.py \
    --keywords "plant genome foundation model" \
    --sources semantic_scholar,crossref,arxiv,biorxiv,pubmed \
    --year-from 2022 \
    --max-results 100 \
    --output search_results.json
```

**参数说明：**

| 参数 | 必选 | 默认值 | 说明 |
|---|---|---|---|
| `--keywords` | ✅ | — | 逗号分隔的关键词列表（应为英文） |
| `--sources` | ❌ | `semantic_scholar,crossref,arxiv,biorxiv` | 启用的搜索源，逗号分隔；可选值：`semantic_scholar`、`crossref`、`arxiv`、`biorxiv`、`pubmed` |
| `--year-from` | ❌ | 2018 | 起始年份 |
| `--year-to` | ❌ | 当前年份 | 截止年份 |
| `--max-results` | ❌ | 100 | 每个 API 返回上限 |
| `--output` | ✅ | — | 输出 JSON 路径 |
| `--verbose` | ❌ | False | 打印各来源请求数和去重统计 |

**退出码：** 0 = 成功（有结果），1 = 部分来源失败但有结果，2 = 全部来源失败或无结果

**输出：** `search_results.json`（见下方 JSON Schema）

### score_papers.py

```bash
# 基本用法
python score_papers.py \
    --input search_results.json \
    --config references/scoring-rules.yaml \
    --top-n 10 \
    --output top_n_dois.json

# 带预筛参数：排除关键词 + 最低关键词命中数 + 论文类型
python score_papers.py \
    --input search_results.json \
    --config references/scoring-rules.yaml \
    --exclude-keywords "protein LLM,protein language model" \
    --min-keyword-hits 1 \
    --paper-types method,empirical_study,dataset_benchmark \
    --top-n 10 \
    --output top_n_dois.json

# 从 pipeline_params.json 自动读取预筛参数
python score_papers.py \
    --input search_results.json \
    --config references/scoring-rules.yaml \
    --params pipeline_params.json \
    --top-n 10 \
    --output top_n_dois.json

# 不截断，输出全量排序结果
python score_papers.py \
    --input search_results.json \
    --config references/scoring-rules.yaml \
    --top-n 0 \
    --output top_n_dois.json
```

**参数说明：**

| 参数 | 必选 | 默认值 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | search_papers.py 输出的 JSON 路径 |
| `--config` | ❌ | `references/scoring-rules.yaml` | 打分参数 YAML 路径 |
| `--top-n` | ❌ | 10 | 截断数量，0 表示不截断 |
| `--exclude-keywords` | ❌ | — | 排除关键词，逗号分隔；标题或摘要命中任一词则预筛排除 |
| `--min-keyword-hits` | ❌ | 0 | 最低关键词命中数，低于此值的论文在预筛阶段排除 |
| `--paper-types` | ❌ | — | 论文类型白名单，逗号分隔；可选 `method`、`empirical_study`、`review_survey`、`dataset_benchmark` |
| `--params` | ❌ | — | pipeline_params.json 路径；若提供，自动从中读取 exclude_keywords、keyword_weights、paper_types |
| `--output` | ✅ | — | 输出 JSON 路径 |
| `--verbose` | ❌ | False | 打印每篇论文的分数明细 + 预筛统计 |

**预筛逻辑（阶段 3a）：**

在正式打分前，先执行轻量级预筛，排除明显不相关的论文，避免弱相关论文稀释 Top N 质量：

1. **排除关键词过滤**：若论文标题或摘要命中任一排除关键词，直接排除
2. **最低关键词命中数**：论文标题+摘要中命中的搜索关键词数低于 `--min-keyword-hits`，直接排除
3. **论文类型过滤**：若指定了 `--paper-types`，通过 Semantic Scholar 的 `paperType` 字段或标题启发式判断（含"review"/"survey"判定为综述），不符合类型的排除

预筛排除的论文不进入打分，但在 `--verbose` 输出中报告预筛统计（排除数、排除原因分布）。

**退出码：** 0 = 成功，1 = 输入文件读取失败或为空，2 = 配置文件格式错误

**输出：** `top_n_dois.json`（见下方 JSON Schema）

---

## 阶段间状态传递 — JSON Schema

### pipeline_params.json（阶段 1 输出 → 后续阶段共用）

```json
{
  "run_id": "20260528-plant-genome",
  "keywords": ["plant genome foundation model", "plant LLM"],
  "search_queries": ["plant genome foundation model", "plant LLM", "gene discovery deep learning"],
  "exclude_keywords": ["protein LLM", "protein language model"],
  "keyword_weights": {
    "plant genome foundation model": 1.0,
    "plant LLM": 0.8
  },
  "language": "en",
  "paper_types": ["method", "empirical_study", "dataset_benchmark"],
  "year_from": 2022,
  "year_to": 2026,
  "preferred_journals": ["Nature", "Nature Genetics", "Cell", "Science"],
  "top_n": 10,
  "save_dir": "papers",
  "vault_root": "/path/to/vault",
  "asset_profile": "body",
  "domain": "plant_genomics",
  "search_sources": ["semantic_scholar", "crossref", "arxiv", "biorxiv", "pubmed"],
  "requires": {
    "paper-fetch-skill": ">=0.1.0"
  },
  "created_at": "2026-05-28T22:00:00+08:00"
}
```

字段说明：
- `search_queries`：AI 将用户关键词翻译为英文后的查询词列表，供搜索脚本直接使用
- `exclude_keywords`：排除关键词，标题或摘要命中这些词的论文在预筛阶段被排除
- `keyword_weights`：各关键词权重，默认 1.0，影响关键词匹配度计算
- `language`：语言偏好，默认 `"en"`，仅搜索英文论文
- `paper_types`：论文类型偏好，可选值 `method`、`empirical_study`、`review_survey`、`dataset_benchmark`
- `vault_root`：Obsidian vault 根目录，用于解析脚本相对路径
- `search_sources`：启用的搜索源列表，生命科学领域默认包含 bioRxiv + PubMed
- `requires`：前置依赖版本声明，防止接口不兼容时静默失败

### search_results.json（阶段 2 输出 → 阶段 3 输入）

```json
{
  "run_id": "20260528-plant-genome",
  "created_at": "2026-05-28T22:05:00+08:00",
  "stats": {
    "semantic_scholar": 85,
    "crossref": 120,
    "arxiv": 42,
    "biorxiv": 35,
    "pubmed": 68,
    "total_before_dedup": 350,
    "total_after_dedup": 265,
    "duplicates_removed": 85,
    "abstract_coverage": 0.72
  },
  "errors": [
    {
      "source": "semantic_scholar",
      "error": "HTTP 429 rate limited",
      "retried": true,
      "recovered": true
    }
  ],
  "papers": [
    {
      "doi": "10.1038/s41586-024-07189-3",
      "title": "A foundation model for plant genomics",
      "abstract": "...",
      "citation_count": 42,
      "year": 2024,
      "venue": "Nature",
      "authors": ["Author A", "Author B"],
      "source": ["semantic_scholar", "crossref"],
      "arxiv_id": null,
      "has_abstract": true,
      "raw": {}
    }
  ]
}
```

字段说明：
- `source`：数组，记录该论文来自哪些搜索源（去重后合并）
- `has_abstract`：标记是否有摘要，影响打分时关键词匹配度的计算
- `raw`：保留原始 API 响应的关键字段，供调试

### top_n_dois.json（阶段 3 输出 → 阶段 4 输入）

```json
{
  "run_id": "20260528-plant-genome",
  "created_at": "2026-05-28T22:10:00+08:00",
  "config": {
    "keywords": ["plant genome foundation model"],
    "top_n": 10,
    "scoring_config": "references/scoring-rules.yaml"
  },
  "papers": [
    {
      "doi": "10.1038/s41586-024-07189-3",
      "title": "A foundation model for plant genomics",
      "year": 2024,
      "venue": "Nature",
      "citation_count": 42,
      "score": 0.82,
      "score_breakdown": {
        "keyword_match": 0.90,
        "citation_weighted": 0.70,
        "recency_decay": 0.92,
        "journal_quality": 1.00
      },
      "has_abstract": true
    }
  ]
}
```

### pipeline_state.json（断点续跑状态）

```json
{
  "run_id": "20260528-plant-genome",
  "status": "stage_3_completed",
  "current_stage": 4,
  "params_path": "pipeline_params.json",
  "artifacts": {
    "stage_2": "search_results.json",
    "stage_3": "top_n_dois.json"
  },
  "fetch_progress": {
    "total": 10,
    "succeeded": ["10.1038/...", "10.1016/..."],
    "failed": [{"doi": "10.1126/...", "reason": "no_fulltext"}],
    "pending": ["10.1101/..."]
  },
  "created_at": "2026-05-28T22:00:00+08:00",
  "updated_at": "2026-05-28T22:30:00+08:00"
}
```

断点续跑逻辑：
- SKILL.md 启动时检查当前目录是否存在 `pipeline_state.json`
- 若存在且 `status` 不是 `completed`，向用户报告上次进度并询问"续跑还是重新开始"
- 若用户选择续跑，从 `current_stage` 和 `fetch_progress.pending` 继续
- 若用户换了关键词（`run_id` 不同），清除旧状态重新开始

---

## 打分公式

打分前先执行**预筛**（阶段 3a，详见 score_papers.py 预筛逻辑），排除明显不相关的论文。通过预筛的论文再进入正式打分（阶段 3b）。

```
总分 = w_kw * 关键词匹配度 + w_cit * 引用数加权 + w_rec * 时效衰减 + w_jrn * 期刊质量
```

默认权重 `w_kw=0.40, w_cit=0.30, w_rec=0.20, w_jrn=0.10`，所有权重和参数写在 `references/scoring-rules.yaml`。

关键词匹配度计算时，若 `keyword_weights` 中指定了某关键词的权重，该关键词命中时按权重计分而非简单计数。

### 各维度计算方式

#### 关键词匹配度（w_kw = 0.40）

```
关键词匹配度 = 命中关键词数 / 关键词总数
```

匹配规则：
1. **大小写不敏感**：统一 lower() 后匹配
2. **去标点**：移除标题/摘要中的标点后匹配
3. **子串匹配**：关键词 "genome" 可匹配 "genomics"、"genomic"（词干级别）
4. **短语优先**：多词关键词（如 "plant genome"）作为整体匹配，命中则该短语记为 1 次命中，不再拆词重复计算
5. **无摘要降权**：若论文缺少摘要（`has_abstract=false`），关键词匹配度按 `0.5 * 实际值` 计算，因为仅标题匹配的区分度低

#### 引用数加权（w_cit = 0.30）

```
引用数加权 = log(引用数 + 1) / log(citation_max + 1)
```

- `citation_max` 默认值为 **5000**（可通过 YAML 调整），而非硬编码 1000
- 使用对数归一化避免少数超高引论文独占，同时 `citation_max=5000` 保证高引区间仍有区分度
- 引用数为 0 的论文该维度得 0，但不影响其他维度

#### 时效衰减（w_rec = 0.20）

```
时效衰减 = max(floor, exp(-λ * 论文年龄))
```

- `λ`（衰减速率）默认 **0.3**，可通过 YAML 调整
- `floor`（衰减下限）默认 **0.1**，保证 >5 年的老论文不会直接归零
- 论文年龄 = 当前年份 - 发表年份
- 示例：1 年 → 0.74，3 年 → 0.41，5 年 → 0.22，10 年 → 0.05（不低于 0.1）

#### 期刊质量（w_jrn = 0.10）

期刊分层映射表写在 `references/journal-tiers.yaml`，按以下规则判定：

```yaml
# journal-tiers.yaml 结构
tiers:
  tier1:  # 分值 1.0
    keywords: ["nature", "science", "cell"]
    exact: ["Nature", "Science", "Cell"]
  tier1_sub:  # 子刊 分值 0.9
    keywords: ["nature communications", "nature genetics", "nature methods", "nature biotechnology", "cell reports", "cell systems"]
    exact: []
  tier2:  # 领域顶刊 分值 0.6
    keywords: ["pnas", "genome research", "genome biology", "nucleic acids research", "bioinformatics", "plant cell", "plant journal", "new phytologist"]
    exact: []
  default: 0.2  # 未匹配到的期刊默认分值
```

匹配流程：
1. 将论文 `venue` 字段 lower() + 去标点后，先尝试 `exact` 精确匹配
2. 再尝试 `keywords` 中的子串匹配
3. 未匹配到任何条目则使用 `default` 分值
4. arXiv 预印本（无 venue）使用 `default` 分值

---

## 搜索策略

### 搜索源

| API | 请求频率 | 返回字段 | 覆盖范围 | 前沿性 |
|---|---|---|---|---|
| Semantic Scholar | 1 req/s（免费） | 标题、DOI、摘要、引用数、年份、venue、paperType | 综合学术 | 中（索引有 1-3 月延迟） |
| Crossref | 50 req/s（免费） | 标题、DOI、作者、期刊、摘要（部分） | 元数据最全 | 低（正式发表后收录） |
| arXiv | 无限制 | 标题、arXiv ID、摘要、分类 | CS/物理/定量生物 | 高（预印本首发） |
| bioRxiv | 无限制（免费） | 标题、DOI、摘要、分类、发布日期 | 生命科学预印本 | **极高**（比正式发表早 6-12 月） |
| PubMed | 10 req/s（免费，需 API key） | 标题、DOI、摘要、期刊、MeSH 词 | 生物医学最全索引 | 中（含 ahead-of-print） |

**前沿性说明：** 对于"捕捉领域最新趋势"的核心目标，bioRxiv 是最关键的搜索源——生命科学领域大量重要工作以预印本首发，比正式发表早 6-12 个月。仅依赖 Semantic Scholar + Crossref + arXiv 会系统性遗漏生物/医学方向的最新工作。

**搜索源选择策略：**
- 默认开启：Semantic Scholar + Crossref + arXiv + bioRxiv（4 源）
- 当 `domain` 包含生命科学关键词（如 `plant_genomics`、`single_cell`、`epigenomics`）时，自动开启 PubMed
- 用户可在阶段 1 通过 `search_sources` 参数手动选择搜索源组合

### 搜索策略

- 每个关键词组合在所有已开启的 API 上并行查询
- 每个 API 返回上限 100 条（可通过 `--max-results` 配置）
- 多路结果合并后去重（见下方去重策略）

### 去重策略

去重分三级，依次执行：

**第 1 级：DOI 精确去重**
- 同一 DOI 出现在多个来源 → 合并 `source` 数组，保留最完整的摘要（优先 Semantic Scholar）
- 无 DOI 的论文（部分 arXiv）进入第 2 级

**第 2 级：arXiv ID ↔ DOI 映射**
- 对有 arXiv ID 的论文，通过 Semantic Scholar 的 `externalIds` 字段查找对应 DOI
- 若找到 DOI 且该 DOI 已在结果中 → 合并；若未找到 → 保留，标记 `arxiv_only=true`

**第 3 级：标题模糊去重**
- 标题归一化：lower() + 去标点 + 去连字符 + 去多余空格 → 单一比较串
- 归一化后完全相同的标题视为重复 → 合并，保留有 DOI 的版本
- 不做编辑距离匹配（避免误合并不同论文）

去重后报告格式：
```
搜索结果统计：
  Semantic Scholar: 85 篇
  Crossref: 120 篇
  arXiv: 42 篇
  bioRxiv: 35 篇
  PubMed: 68 篇
  去重前总计: 350 篇
  DOI 去重: -45 篇
  arXiv/bioRxiv ID 映射去重: -18 篇
  标题模糊去重: -22 篇
  去重后总计: 265 篇
  摘要覆盖率: 72%
```

---

## 错误处理策略

### 搜索阶段（阶段 2）

| 故障 | 处理 | 用户通知 |
|---|---|---|
| 单个 API 返回 429/500 | 指数退避重试（最多 3 次，间隔 2s/4s/8s） | 重试成功则静默，3 次均失败则在报告中标注 |
| 单个 API 完全不可达 | 跳过该 API，使用其余 API 的结果 | 报告中标注该 API 失败 |
| 所有 API 全部失败 | 写空结果文件，退出码 2 | 告知用户搜索失败，建议检查网络或换关键词 |
| 搜索结果为零 | 写空结果文件，退出码 0 | 告知用户无结果，建议调整关键词或扩大时间范围 |

### 下载阶段（阶段 4）

| 故障 | 处理 | 用户通知 |
|---|---|---|
| 单篇论文无全文（付费墙等） | 标记为 `metadata_only`，保存元数据摘要 | 每 3 篇进度报告中标注 |
| 单篇论文 MCP 调用超时/报错 | 跳过该篇，记入 `pipeline_state.json` 的 failed 列表 | 进度报告中标注失败原因 |
| 连续 3 篇失败 | 暂停下载，向用户报告并询问是否继续 | BLOCKING：需用户确认 |

### 审阅阶段（阶段 5）

| 故障 | 处理 | 用户通知 |
|---|---|---|
| 论文仅有摘要 | 按"摘要级审阅"生成笔记，标题标注 `[摘要]` | 笔记中标注数据来源为摘要 |
| 论文 Markdown 解析异常 | 跳过该篇，记录到 failed 列表 | 最终报告标注 |

### 恢复策略

所有阶段遵循 **continue-on-error** 原则：单篇/单源失败不阻塞整体流程。失败详情记录在 `pipeline_state.json`，阶段 5 结束后输出一份失败汇总报告。

---

## 用户交互节点

| 节点 | 用户操作 | 默认行为 |
|---|---|---|
| 阶段 1 结束 | 确认关键词、时间范围、保存偏好 | — |
| 阶段 2 结束 | 查看搜索来源统计 + 去重统计，可调整关键词重新搜索 | — |
| 阶段 3 结束 | 查看 Top N 列表 + 分数明细，可手动剔除/增加论文 | 默认接受 Top N |
| 阶段 4 连续 3 篇失败 | 决定是否继续下载 | — |
| 阶段 4 进行中 | 无需操作 | 每 3 篇报告进度（含成功/失败） |
| 阶段 5 结束 | 查看生成的 Obsidian 笔记 | — |

---

## 领域审阅补充模板

领域补充模板存放在 `references/domain-supplement.yaml`，支持多领域切换：

```yaml
# domain-supplement.yaml
plant_genomics:
  name: "植物基因组"
  keywords: ["plant genome", "crop genome", "plant LLM", "gene discovery"]
  extra_sections:
    - title: "物种信息"
      fields:
        - label: "学名"
        - label: "品种/品系"
        - label: "基因组版本"
    - title: "训练数据"
      fields:
        - label: "数据集名称"
        - label: "样本量（正/负）"
        - label: "负样本构建方式"
    - title: "模型架构"
      fields:
        - label: "模型类型（Transformer/CNN/GNN/混合）"
        - label: "参数规模"
        - label: "输入序列长度"
    - title: "下游任务"
      fields:
        - label: "基因发现 / 表达预测 / 变异效应预测 / 染色质状态 / 其他"
    - title: "与项目指标关联"
      fields:
        - label: "本项目研究物种是否与该论文物种相同/近缘"
        - label: "论文方法是否可迁移到本项目物种"

# 示例：未来可扩展
# single_cell:
#   name: "单细胞组学"
#   keywords: ["single cell", "scRNA-seq", "spatial transcriptomics"]
#   extra_sections:
#     - title: "实验平台"
#       fields:
#         - label: "测序平台"
#         - label: "建库方法"
#         - label: "细胞数"
```

AI 在阶段 5 生成笔记时：
1. 读取 paper-obsidian-review SKILL.md 获取标准单篇/对比笔记结构
2. 从 `domain-supplement.yaml` 读取当前领域（由 `pipeline_params.json` 中的 `domain` 指定）的额外章节
3. 在标准结构的"与指标关联"章节之前，插入领域专属章节

---

## 环境依赖

```
Python 3.13（管理版，在 .venv 中运行）
httpx          # HTTP 客户端，调搜索 API
PyYAML         # 读 scoring-rules.yaml / domain-supplement.yaml / journal-tiers.yaml
```

不依赖 paper-fetch 或 paper-obsidian-review 的任何代码。阶段 4 通过 MCP 工具调用，阶段 5 通过 SKILL.md 引用。

---

## 安装方式

```bash
# SKILL.md 放到 WorkBuddy skills 目录（AI 自动识别）
mkdir -p ~/.workbuddy/skills/auto-paper-pipeline
cp SKILL.md ~/.workbuddy/skills/auto-paper-pipeline/

# Python 脚本和 venv 放到 .tools/ 下（遵循项目约定，不污染系统）
mkdir -p /path/to/vault/.tools/auto-paper-pipeline
cp -r scripts/ /path/to/vault/.tools/auto-paper-pipeline/scripts/
cp -r references/ /path/to/vault/.tools/auto-paper-pipeline/references/

# 安装 Python 依赖
python3 -m venv /path/to/vault/.tools/auto-paper-pipeline/.venv
/path/to/vault/.tools/auto-paper-pipeline/.venv/bin/pip install httpx PyYAML
```

SKILL.md 中的脚本路径写相对于 vault 根目录的路径：`.tools/auto-paper-pipeline/scripts/search_papers.py`。

---

## 前置依赖 Skill

| Skill | 用途 | 协作方式 | 最低版本 | 状态 |
|---|---|---|---|---|
| paper-fetch-skill | 下载论文全文（Markdown） | MCP 直调 + 健康检查 + CLI 降级（详见组件协作机制） | >=0.1.0 | ✅ 已安装 |
| paper-obsidian-review | 结构化审阅模板 | SKILL.md 引用式读取模板结构，AI 按合并模板生成 | — | ✅ 已安装 |

---

## 后续扩展计划

- [x] 支持 bioRxiv/PubMed 作为搜索源（v0.3 已加入）
- [ ] 支持 Google Scholar 作为额外搜索源
- [ ] 增量模式：每周自动搜索新论文，和已有库对比去重
- [ ] 定时任务：通过 WorkBuddy automation 每周自动运行
- [ ] 多领域关键词模板：单细胞组学、表观遗传学等
- [ ] 搜索质量反馈循环：用户标记"不相关"后调整打分权重

---

## 版本

- v0.4 — 2026-05-29 — 搜索源配置去冗余重构：引入 search_source_registry.yaml 唯一权威入口；打分阶段新增可下载性加分；search-apis.md 改为自动生成
- v0.3 — 2026-05-29 — 增加 bioRxiv/PubMed 搜索源；完善阶段 1 参数（排除关键词、关键词权重、论文类型、vault_root、search_queries）；增加阶段 3a 预筛逻辑；MCP 健康检查 + CLI 降级方案 + 版本依赖声明；增加 abstract_coverage 统计
- v0.2 — 2026-05-28 — 补充组件协作机制、CLI 接口、JSON Schema、去重策略、错误处理、打分公式细节
- v0.1 — 2026-05-28 — 初版产品文档

---

## 开发经验

### [v0.4] 搜索源配置去冗余重构

**核心问题**：搜索源配置散落在 3 处（`search_papers.py` 硬编码常量、`SKILL.md` 默认值、`search-apis.md` 手写文档），导致：
1. 新增搜索源需改 3 处代码+文档，且容易遗漏
2. `search-apis.md` 与代码不同步（arXiv 端点已有 HTTP vs HTTPS 不一致）
3. 打分阶段不知道论文能否被 paper-fetch 下载，搜索结果大量来自不支持的出版社（Frontiers/Wiley/Elsevier），阶段 4 全部落空

**解决思路**：
- 引入 `references/search_source_registry.yaml` 作为**唯一权威配置入口**，同时包含搜索源定义和可下载能力矩阵
- Python 代码从 YAML 动态加载，消除硬编码常量
- `search-apis.md` 改为从 YAML 自动生成，彻底消除文档与代码的 drift
- 打分阶段新增 `downloadability_bonus` 维度，对论文 DOI 做 provider 匹配，从源头引导搜索结果偏向"能实际下载"的论文

**关键改动点**：

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `references/search_source_registry.yaml` | 新增 | 唯一权威搜索源配置，含 sources 节 + downloadable_providers 节 |
| `scripts/search_papers.py` | 重构 | `DEFAULT_SOURCES`/`VALID_SOURCES` 改为从 registry 动态加载；`_request_with_retry` 的重试参数从 registry 按源读取 |
| `scripts/score_papers.py` | 重构 | 新增 `get_downloadability()` 函数和 `downloadability_bonus` 评分维度；综合评分公式加入可下载性加分 |
| `scripts/generate_search_docs.py` | 新增 | 从 YAML 自动生成 search-apis.md 的工具脚本 |
| `references/search-apis.md` | 自动生成 | 改为由脚本生成，文件头标注"请勿手动修改" |
| `references/scoring-rules.md` | 更新 | 新增第 5 维度"可下载性加分"说明及分值表 |
| `SKILL.md` | 更构 | 新增"搜索源配置"章节说明职责边界；`search_sources` 默认值改为"参考 registry" |

**职责边界确立**：
- `auto-paper-pipeline`（编排层）：读取 registry 的 `sources` 节驱动搜索，读取 `downloadable_providers` 节计算可下载性加分
- `paper-fetch-skill`（全文获取层）：实际执行 DOI→Provider 路由和全文获取，不直接读取此配置
- 两层通过 YAML 契约解耦，而非通过代码耦合

**扩展性改进**：新增搜索源从"改 3 处代码+文档"降为"改 1 处 YAML + 写 1 个搜索函数"。

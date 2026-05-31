# paper-catch 架构设计

paper-catch 是一个新领域快速探索流水线，最终输出一个 Markdown 阅读报告。核心流程为：

```text
主题词
  -> [1] 参数确认
  -> [2] 多源搜索
  -> [3] 前沿优先打分
  -> [4] 主题聚类与阅读路线
  -> [5] paper-fetch CLI 批量下载
  -> [6] 结构化审阅与阅读报告
```

技术栈：Python 3.9+、httpx、PyYAML、paper-fetch CLI。

## 目录职责

```text
paper-catch/
├── pipeline/
│   ├── search.py       # 阶段 2：多源搜索 + 去重
│   ├── score.py        # 阶段 3：评分模式 + Top N 增强字段
│   └── download.py     # 阶段 5：paper-fetch CLI 封装
├── config/
│   ├── search_sources.yaml
│   ├── scoring.yaml
│   ├── journal_tiers.yaml
│   ├── domain_supplement.yaml
│   └── review_template.yaml
├── skills/
│   └── auto-paper-pipeline/SKILL.md
├── outputs/<run_id>/
│   ├── pipeline_params.json
│   ├── search_results.json
│   ├── top_n_dois.json
│   ├── cluster_summary.json
│   ├── 阅读报告.md
│   └── papers/
│       └── fulltext/
└── docs/
```

## 数据流

### 阶段 1：参数确认

AI 负责生成 `pipeline_params.json`。默认 `mode=quick_explore`，用户只需提供主题词。高级配置使用 `mode=tracking`。

关键字段：

- `mode`
- `scoring_mode`
- `relevance_profile`
- `output_profile`
- `search_queries`
- `query_expansion_notes`
- `search_sources`
- `top_n`

### 阶段 2：多源搜索

`pipeline/search.py` 读取 `config/search_sources.yaml`，查询 Semantic Scholar、Crossref、arXiv、bioRxiv、PubMed 等来源，执行 DOI、arXiv ID 和标题去重，输出 `search_results.json`。

### 阶段 3：打分排序

`pipeline/score.py` 读取：

- `config/scoring.yaml`
- `config/journal_tiers.yaml`
- `config/search_sources.yaml`
- `pipeline_params.json`

支持三种评分模式：

- `frontier`
- `foundation`
- `balanced`

输出 `top_n_dois.json`。每篇论文除分数外，还包含：

- `selection_reason`
- `risk_flags`
- `evidence_level`
- `download_provider`
- `download_reliability`
- `cluster_id`
- `recommended_reading_order`
- `topic_relevance`
- `matched_concept_groups`
- `missing_required_groups`
- `relevance_flags`

### 阶段 4：主题聚类与阅读路线

由 AI 基于 `top_n_dois.json` 的摘要、元数据、`cluster_id` 和排序结果生成：

- `cluster_summary.json`

第一版不引入 embedding、向量数据库或额外 ML 依赖。

### 阶段 5：批量下载

`pipeline/download.py` 是 `paper-fetch` CLI 的封装层，负责：

- 执行逐篇下载
- 重试临时失败
- 分类永久失败
- 验证输出 Markdown 是否包含全文
- 输出下载诊断

运行前必须确认 `paper-fetch` CLI 存在。CLI 缺失时暂停，不继续进入全文级审阅。

### 阶段 6：结构化审阅与阅读报告

AI 读取：

- `config/review_template.yaml`
- `config/domain_supplement.yaml`
- `cluster_summary.json`
- `top_n_dois.json`
- `papers/fulltext/*.md`

只输出一个 Markdown 阅读报告：

```text
outputs/<run_id>/阅读报告.md
```

报告模板参考 `paper-obsidian-review` 的学术中文章节，包含基本信息、核心结论摘要、研究背景与问题动因、核心科学问题、论文总览表、阅读路线、逐篇阅读笔记、横向对比、方法路线与技术趋势、复现优先级建议、证据审计与失败记录、开放问题与下一步检索。

## 配置事实源

### search_sources.yaml

唯一搜索源和下载能力矩阵入口。`pipeline/search.py` 使用 `sources`，`pipeline/score.py` 使用 `downloadable_providers` 计算可下载性。

### scoring.yaml

唯一评分配置入口。包含 `frontier`、`foundation`、`balanced` 三种模式。

### review_template.yaml

唯一阅读报告模板入口。项目不再运行时读取 `paper-obsidian-review` 的 `SKILL.md`。

## 外部依赖边界

- `paper-fetch`：作为 CLI 工具依赖，负责全文获取。
- `paper-obsidian-review`：只作为模板设计来源，不作为运行时依赖。

## 证据等级

所有阶段 6 输出必须使用证据等级：

- `fulltext_supported`
- `abstract_supported`
- `metadata_inferred`
- `unknown`

摘要级和元数据级材料不得生成全文级结论。

## 验证重点

- 文档、Skill 和脚本对阶段数量、路径和下载方式一致。
- `pipeline/score.py --scoring-mode frontier|foundation|balanced` 可用。
- `top_n_dois.json` 包含 Top N 增强字段。
- paper-fetch CLI 缺失时阶段 5 明确暂停。
- 最终输出是一个 Markdown 阅读报告。

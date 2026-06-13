---
name: auto-paper-pipeline
description: "新领域快速探索与单篇论文阅读报告生成 skill。用户输入主题词后，自动扩展英文查询，完成多源搜索、前沿优先排序、主题聚类、paper-fetch MCP/CLI 批量下载，并生成一个 Markdown 阅读报告。"
---

# Auto Paper Pipeline

## 执行纪律

这是严格的六阶段串行流程。每个阶段的产物必须写入 `outputs/<run_id>/`，下游阶段只读取上游产物。

BLOCKING 节点必须暂停等待用户确认。单个搜索源、单篇论文或单个笔记失败时遵循 continue-on-error，记录到失败报告后继续。

## 默认入口

默认使用 `quick_explore` 模式：用户只需要给出一个中文或英文主题词。AI 自动补全查询词、时间范围、搜索源、Top N、输出目录和评分模式。

高级用户可要求 `tracking` 模式。该模式允许显式配置排除词、关键词权重、论文类型、偏好期刊、搜索源、时间范围和 Top N。

## 依赖边界

- 全文获取只通过 `pipeline/download.py` 执行。默认 `--fetch-backend auto` 优先使用本地 paper-fetch MCP server，MCP 不可用时回退 `paper-fetch` CLI；不要绕过该脚本直接调用其他 skill 生成伪全文。
- 审阅模板使用本项目的 `config/review_template.yaml`。`paper-obsidian-review` 只作为模板设计来源，不在运行时动态读取。
- 搜索源和下载能力矩阵的唯一权威入口是 `config/search_sources.yaml`。
- 评分参数的唯一权威入口是 `config/scoring.yaml`。
- 主题相关性 profile 的唯一权威入口是 `config/relevance_profiles.yaml`。
- 领域补充字段读取 `config/domain_supplement.yaml`。

## 环境检查

启动阶段 5 前必须检查 paper-fetch 后端：

```bash
.venv/bin/python pipeline/download.py --check-backend --fetch-backend auto
```

若 MCP 和 CLI 都不可用，阶段 5 BLOCKING。不要继续生成伪全文审阅。

注意：MCP 后端优先使用当前 Python 环境中的 `mcp` SDK；若当前环境没有 SDK，但 mcp.json 的 server command 是带有 SDK 的 Python 解释器，脚本会借用该解释器执行轻量 MCP client。MCP 不可用时，`auto` 会继续尝试 CLI 回退。

向用户给出以下配置提示：

```bash
# MCP 优先：配置 WorkBuddy 兼容 mcp.json，默认查找：
# .workbuddy/mcp.json
# ~/.workbuddy/mcp.json
# ~/.config/paper-catch/mcp.json
#
# mcpServers 中默认 server 名为 paper-fetch。

# CLI 回退：推荐优先按上游 Releases 离线包安装：
# https://github.com/Dictation354/paper-fetch-skill/releases

# 开发/源码安装方式：
mkdir -p external
git clone https://github.com/Dictation354/paper-fetch-skill.git external/paper-fetch-skill
cd external/paper-fetch-skill
./install.sh --lite
# 或者只装进当前 Python 环境：
# python3 -m pip install .

paper-fetch --help
```

CLI 最小 smoke test：

```bash
paper-fetch --query "10.1186/1471-2105-11-421" \
  --output-dir /tmp/paper-fetch-smoke \
  --artifact-mode none
```

推荐环境变量：

| 变量 | 用途 |
| --- | --- |
| `CROSSREF_MAILTO` | Crossref 礼貌池和部分 provider 元数据获取 |
| `ELSEVIER_API_KEY` | Elsevier 全文获取 |
| `PUBMED_API_KEY` | PubMed 较高速率访问 |
| `CLOAKBROWSER_HEADLESS` | 低可靠 provider 的浏览器下载 |

## 阶段 1：参数确认

构造 `outputs/<run_id>/pipeline_params.json`。

`quick_explore` 默认字段：

```json
{
  "mode": "quick_explore",
  "scoring_mode": "frontier",
  "relevance_profile": "由 AI 根据主题推断；植物基因组+大模型方向使用 plant_genome_llm",
  "output_profile": "single_markdown_report",
  "keywords": ["用户原始主题词"],
  "search_queries": ["AI 扩展后的英文查询词"],
  "query_expansion_notes": "说明如何从用户主题词扩展查询",
  "year_from": 2021,
  "year_to": "当前年份",
  "top_n": 20,
  "search_sources": "从 config/search_sources.yaml 默认项和 domain 自动规则解析",
  "save_dir": "papers",
  "asset_profile": "body",
  "domain": ""
}
```

向用户展示参数摘要后 BLOCKING。用户确认后进入阶段 2。

## 阶段 2：多源搜索

运行：

```bash
.venv/bin/python pipeline/search.py \
  --keywords "<search_queries 逗号连接>" \
  --sources "<search_sources 逗号连接>" \
  --year-from <year_from> \
  --year-to <year_to> \
  --max-results 100 \
  --params outputs/<run_id>/pipeline_params.json \
  --output outputs/<run_id>/search_results.json \
  --verbose
```

阶段结束展示来源数量、去重统计、摘要覆盖率和失败来源。无结果或全部失败时 BLOCKING，请用户调整关键词或时间范围。

## 阶段 3：前沿优先打分排序

默认使用 `frontier` 模式。

```bash
.venv/bin/python pipeline/score.py \
  --input outputs/<run_id>/search_results.json \
  --config config/scoring.yaml \
  --params outputs/<run_id>/pipeline_params.json \
  --scoring-mode <scoring_mode> \
  --relevance-profile <relevance_profile> \
  --top-n <top_n> \
  --output outputs/<run_id>/top_n_dois.json \
  --verbose
```

`top_n_dois.json` 中每篇论文必须包含：

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

向用户展示 Top N 表格，包含标题、年份、分数、入选理由、风险标记、下载可靠性。用户可删除、补充或确认。确认后进入阶段 4。

## 阶段 4：主题聚类与阅读路线

基于 `top_n_dois.json` 的 `cluster_id`、摘要、年份、来源和分数，生成：

- `outputs/<run_id>/cluster_summary.json`

`cluster_summary.json` 必须包含：

- 先读 5 篇
- 按主题阅读
- 按目标阅读：快速了解、写基金、复现实验、找数据集

该阶段由 AI 生成，不引入 embedding 或向量数据库依赖。

## 阶段 5：批量下载

通过 `pipeline/download.py` 使用 paper-fetch MCP/CLI 后端执行：

```bash
.venv/bin/python pipeline/download.py \
  --input outputs/<run_id>/top_n_dois.json \
  --output-dir outputs/<run_id>/papers/fulltext/ \
  --state-file outputs/<run_id>/pipeline_state.json \
  --fetch-backend auto \
  --verbose
```

下载前向用户确认：下载数量、输出目录、是否允许元数据兜底。确认后执行。

下载失败不阻塞后续论文。失败详情记录到 `outputs/<run_id>/pipeline_state.json` 和最终失败报告。

## 阶段 6：生成单个 Markdown 阅读报告

读取：

- `config/review_template.yaml`
- `config/domain_supplement.yaml`
- `outputs/<run_id>/top_n_dois.json`
- `outputs/<run_id>/papers/fulltext/*.md`
- `outputs/<run_id>/cluster_summary.json`

只生成一个 Markdown 文件：

```text
outputs/<run_id>/阅读报告.md
```

报告结构参考 `paper-obsidian-review` 的学术中文标题，但必须压缩在同一个文件中。报告必须包含：基本信息、核心结论摘要、研究背景与问题动因、核心科学问题、论文总览表、阅读路线、逐篇阅读笔记、横向对比、方法路线与技术趋势、复现优先级建议、证据审计与失败记录、开放问题与下一步检索。

不得生成论文卡片目录、领域地图文件、阅读路线文件、总览对比文件或失败报告文件。

证据等级规则：

- `fulltext_supported`：有全文支撑
- `abstract_supported`：仅摘要支撑
- `metadata_inferred`：仅元数据推断
- `unknown`：材料不足

摘要级或元数据级材料不得补写实验细节、结果数值或作者未提供的局限性。

## 断点续跑

启动时检查 `outputs/<run_id>/pipeline_state.json`。若存在且未完成，报告当前阶段、成功数、失败数和待处理论文，并 BLOCKING 询问续跑还是重新开始。

## 最终汇报

完成后向用户报告：

- 搜索来源和去重统计
- Top N 论文数量与评分模式
- 下载成功、失败、仅元数据数量
- Markdown 阅读报告路径
- 需要人工补充的论文列表

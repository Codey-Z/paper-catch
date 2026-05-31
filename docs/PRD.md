# Auto Paper Pipeline PRD

## 产品定位

Auto Paper Pipeline 是一个新领域快速探索 skill。用户输入一个主题词后，系统自动扩展英文查询，完成多源搜索、前沿优先排序、主题聚类、paper-fetch CLI 批量下载，并生成一个可直接阅读和放入 Obsidian 的 Markdown 阅读报告。

目标不是单纯“下载论文并总结”，而是帮助用户快速形成对新研究方向的结构化理解：核心问题、技术路线、关键论文、阅读顺序、证据强弱和后续追踪点。

## 目标用户

- 刚进入新领域，需要快速建立方向地图的科研人员
- 写基金、开题、综述前需要快速扫描前沿的研究者
- 需要定期追踪某一方向新论文的用户

## 核心原则

1. **快速探索优先**：默认只要求用户给出主题词，其余参数自动补全。
2. **前沿可解释**：排序不仅给分数，还给入选理由、风险标记和证据等级。
3. **工具边界清晰**：全文获取只依赖 `paper-fetch` CLI；审阅模板由本项目维护。
4. **单文件报告输出**：最终产物是一个 Markdown 阅读报告，不拆成多个笔记文件。
5. **证据分级**：全文、摘要、元数据和未知信息必须区分，避免把推断写成事实。
6. **容错继续**：单源、单篇、单笔记失败不阻塞整体流程。

## 六阶段流水线

```text
用户输入主题词
  ↓
阶段 1：参数确认
  - quick_explore 默认入口
  - AI 扩展英文 search_queries
  - 写入 pipeline_params.json
  ↓
阶段 2：多源搜索
  - pipeline/search.py
  - 输出 search_results.json
  ↓
阶段 3：打分排序
  - pipeline/score.py --scoring-mode frontier
  - 输出 top_n_dois.json
  - 包含 selection_reason / risk_flags / evidence_level
  ↓
阶段 4：主题聚类与阅读路线
  - 基于 Top N 元数据和摘要由 AI 归纳
  - 输出 cluster_summary.json
  ↓
阶段 5：批量下载
  - pipeline/download.py 封装 paper-fetch CLI
  - 输出 papers/fulltext/*.md
  ↓
阶段 6：结构化审阅与阅读报告
  - 读取 config/review_template.yaml
  - 输出 阅读报告.md
```

## 用户模式

### quick_explore

默认模式。用户只输入一个主题词，例如“植物基因组基础模型”。系统自动生成：

- `search_queries`
- `query_expansion_notes`
- `year_from` / `year_to`
- `top_n`
- `search_sources`
- `scoring_mode=frontier`
- `output_profile=single_markdown_report`

### tracking

高级模式。用户可显式配置：

- 排除关键词
- 关键词权重
- 论文类型
- 偏好期刊
- 搜索源
- 时间范围
- Top N
- 评分模式

## 组件边界

### paper-fetch

本项目不重写全文获取能力。阶段 5 只通过 `paper-fetch` CLI 获取全文，由 `pipeline/download.py` 负责重试、错误分类和结果诊断。

若 `paper-fetch` 不存在，阶段 5 必须暂停，并提示用户安装 CLI。不得继续生成伪全文审阅。

### paper-obsidian-review

`paper-obsidian-review` 只作为模板设计参考，不作为运行时依赖。本项目维护稳定模板：

```text
config/review_template.yaml
```

阶段 6 只读取本项目模板、领域补充字段和下载结果。

## 核心接口

### pipeline_params.json

```json
{
  "run_id": "20260531-plant-genome",
  "mode": "quick_explore",
  "scoring_mode": "frontier",
  "relevance_profile": "plant_genome_llm",
  "output_profile": "single_markdown_report",
  "keywords": ["植物基因组基础模型"],
  "search_queries": [
    "plant genome foundation model",
    "plant genomic language model",
    "deep learning for gene discovery in plants"
  ],
  "query_expansion_notes": "从中文主题扩展为英文检索词，覆盖 foundation model、language model 和 gene discovery。",
  "exclude_keywords": [],
  "keyword_weights": {},
  "paper_types": [],
  "year_from": 2021,
  "year_to": 2026,
  "top_n": 20,
  "search_sources": ["semantic_scholar", "crossref", "arxiv", "biorxiv", "pubmed"],
  "save_dir": "papers",
  "asset_profile": "body",
  "domain": "plant_genomics",
  "created_at": "2026-05-31T00:00:00+08:00"
}
```

### top_n_dois.json

每篇论文必须包含：

```json
{
  "doi": "10.xxxx/example",
  "title": "Example paper",
  "year": 2026,
  "venue": "bioRxiv",
  "score": 0.82,
  "score_breakdown": {
    "keyword_match": 0.9,
    "citation_weighted": 0.1,
    "recency_decay": 1.0,
    "journal_quality": 0.2,
    "downloadability": 0.08,
    "paper_type_bonus": 0.08,
    "paper_type": "method",
    "download_provider": "biorxiv",
    "download_reliability": "medium"
  },
  "selection_reason": "关键词高度匹配；发表时间较新；偏方法论文；全文获取可靠性为 medium",
  "risk_flags": ["abstract_only_before_download"],
  "evidence_level": "abstract_supported",
  "download_provider": "biorxiv",
  "download_reliability": "medium",
  "cluster_id": "method_route",
  "recommended_reading_order": 1
}
```

## 评分模式

`config/scoring.yaml` 提供三种模式：

- `frontier`：默认。提高近年论文、预印本、可下载性、方法/benchmark 权重。
- `foundation`：提高引用数和期刊质量权重，适合找经典论文和综述。
- `balanced`：折中模式，接近旧版公式。

CLI：

```bash
.venv/bin/python pipeline/score.py \
  --input outputs/<run_id>/search_results.json \
  --config config/scoring.yaml \
  --params outputs/<run_id>/pipeline_params.json \
  --scoring-mode frontier \
  --relevance-profile plant_genome_llm \
  --top-n 20 \
  --output outputs/<run_id>/top_n_dois.json
```

## 主题相关性 Profile（V1.2）

V1.2 新增 `config/relevance_profiles.yaml`，用于解决测试中出现的排序过宽问题。

以 `plant_genome_llm` 为例，Top N 应优先满足三个 required concept groups：

- `plant_domain`：plant、crop、rice、wheat、maize、Arabidopsis、root、breeding 等
- `genomics`：genome、genomic、gene、annotation、regulatory element、sequence、variant 等
- `ai_model`：foundation model、genomic language model、large language model、LLM、transformer、SparseMoE 等

阶段 3 使用：

```bash
.venv/bin/python pipeline/score.py \
  --input outputs/<run_id>/search_results.json \
  --config config/scoring.yaml \
  --scoring-mode frontier \
  --relevance-profile plant_genome_llm \
  --top-n 20 \
  --output outputs/<run_id>/top_n_dois.json
```

输出新增字段：

- `topic_relevance`
- `matched_concept_groups`
- `missing_required_groups`
- `relevance_flags`

若命中所有 required groups 的论文少于 5 篇，则触发 soft fallback，不再出现完整短语预筛把结果全部排空的问题。

## Markdown 阅读报告

阶段 6 输出：

```text
outputs/<run_id>/阅读报告.md
```

只生成这一个 Markdown 文件。报告结构参考 `paper-obsidian-review` 的学术中文标题，但把单篇笔记、多篇对比、复现建议和证据审计压缩到同一篇报告中。

### 阅读报告.md

- 基本信息
- 核心结论摘要
- 研究背景与问题
- 核心科学问题
- 论文总览表
- 阅读路线
- 逐篇阅读笔记
- 横向对比
- 方法路线与技术趋势
- 复现优先级建议
- 证据审计与失败记录
- 开放问题与下一步检索

逐篇阅读笔记参考 `paper-obsidian-review` 的结构，包含：基本信息、研究背景与问题动因、核心科学问题、问题求解路径、方法定位、输入输出、方法框架、关键机制、主要创新贡献、数据集/实验设置/配置依赖、复现要点、与研究指标关联、为什么入选。

所有结论必须标注证据等级。

## 证据等级

- `fulltext_supported`：有论文全文支撑
- `abstract_supported`：仅摘要支撑
- `metadata_inferred`：仅标题、年份、venue、DOI 等元数据推断
- `unknown`：材料不足

摘要级材料不得补写实验细节、结果数值、局限性和作者未明确表达的结论。

## 错误处理

- 搜索源失败：记录错误，其他来源继续。
- 搜索结果为零：阶段 2 BLOCKING，建议调整关键词或时间范围。
- `paper-fetch` CLI 缺失：阶段 5 BLOCKING，提示安装。
- 单篇下载失败：记录到 `pipeline_state.json`，并写入 `阅读报告.md` 的“证据审计与失败记录”章节。
- 仅摘要或仅元数据：仍可写入阅读报告，但证据等级必须降级。

## 验收标准

1. 用户输入一个主题词即可完成 quick_explore 参数构造。
2. `pipeline/score.py --scoring-mode frontier` 可运行，并在 `top_n_dois.json` 中输出新增字段。
3. `frontier` 模式相对 `foundation` 更偏向新近、可下载、方法/benchmark 论文。
4. `--relevance-profile plant_genome_llm` 能让 PlantGFM、PlantBiMoE、genomic language model 等核心论文进入前列，并压低弱相关论文。
5. 阶段 6 不动态读取 `paper-obsidian-review`，只读取 `config/review_template.yaml`。
6. 最终输出是单个 Markdown 阅读报告，而不是多个 Obsidian 文件组成的知识包。
7. 摘要级和元数据级内容不会被写成全文级结论。

## 当前实现状态（2026-05-31）

本轮改动已落地到文档、Skill 指令、配置和评分脚本。

### 已实现

- `skills/auto-paper-pipeline/SKILL.md` 已更新为六阶段流程：参数确认、多源搜索、打分排序、主题聚类与阅读路线、批量下载、结构化审阅与阅读报告。
- `config/scoring.yaml` 已支持三种评分模式：`frontier`、`foundation`、`balanced`，默认模式为 `frontier`。
- `pipeline/score.py` 已新增 `--scoring-mode frontier|foundation|balanced` 参数。
- `top_n_dois.json` 的每篇论文已新增：
  - `selection_reason`
  - `risk_flags`
  - `evidence_level`
  - `download_provider`
  - `download_reliability`
  - `cluster_id`
  - `recommended_reading_order`
- `config/review_template.yaml` 已新增，作为本项目稳定审阅模板；运行时不再动态读取 `paper-obsidian-review`。
- `pipeline/search.py`、`pipeline/score.py`、`pipeline/download.py` 已加入 `from __future__ import annotations`，兼容当前 Python 3.9 虚拟环境。
- `README.md`、`docs/architecture.md`、`docs/scoring-rules.md`、`docs/search-apis.md` 已同步到六阶段、CLI 下载、稳定模板和评分模式的新设计。

### 已验证

- Python 语法检查：

```bash
.venv/bin/python -m py_compile pipeline/score.py pipeline/search.py pipeline/download.py
```

- CLI 契约检查：

```bash
.venv/bin/python pipeline/score.py --help
.venv/bin/python pipeline/search.py --help
.venv/bin/python pipeline/download.py --help
```

- 配置解析检查：`config/*.yaml` 均可被 PyYAML 正常读取。
- 临时 fixture 验证：`frontier` 模式优先新近方法论文，`foundation` 模式优先高引综述。
- 代码格式检查：

```bash
git diff --check
```

### 尚未验证

- 未执行真实联网搜索，原因是结果依赖外部 API、网络状态和限流。
- 未执行真实 `paper-fetch` 下载，原因是依赖本机是否安装 `paper-fetch` CLI、provider 凭证和目标论文可访问性。
- 阶段 4 的 `cluster_summary.json` 仍由 Skill 指令中的 AI 生成，当前没有单独脚本实现。
- 阶段 6 的单文件 Markdown 阅读报告仍由 Skill 指令中的 AI 按 `config/review_template.yaml` 执行，当前没有单独脚本实现。

## V1.2 实现状态（2026-05-31）

本轮 V1.2 已根据“植物基因组 + 大模型”测试结果完成排序质量修复。

### 已实现

- 新增 `config/relevance_profiles.yaml`，首个 profile 为 `plant_genome_llm`。
- `pipeline/score.py` 新增 `--relevance-profile` 参数。
- `frontier` 模式新增 `topic_relevance` 权重，并降低仅靠新近性、期刊和可下载性上榜的概率。
- `top_n_dois.json` 每篇论文新增：
  - `topic_relevance`
  - `matched_concept_groups`
  - `missing_required_groups`
  - `relevance_flags`
- 新增 profile gate：优先保留命中 required concept groups 的论文；结果不足时 soft fallback。
- 新增回归测试 `tests/test_relevance_scoring.py` 和 fixture `tests/fixtures/plant_genome_llm_search_results.json`。

### V1.2 验证结果

在本次真实搜索结果上，V1.2 输出 `outputs/20260531-plant-genome-llm-test/top_n_dois.v1_2.json`：

- `Genomic language models with k-mer tokenization strategies...` 排名第 1。
- `PlantGFM: A Genomic Foundation Model for Discovery and Creation of Plant Genes` 排名第 3。
- `PlantBiMoE: A Bidirectional Foundation Model with SparseMoE for Plant Genomes` 排名第 4。
- 初版 Top N 中靠前的弱相关论文 `Regulatory architecture...`、`From pollen precursor...`、`A conversational multi-agent AI system...` 已不在 V1.2 Top N 中。

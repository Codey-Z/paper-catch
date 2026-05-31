---
name: auto-paper-pipeline
description: "自动论文发现与审阅流水线。用户输入领域关键词后，自动完成：多源搜索 → 预筛 → 打分排序 → 批量下载 → 结构化审阅 → 生成 Obsidian 笔记。编排 paper-fetch-skill 和 paper-obsidian-review 完成各自擅长的任务。"
---

# Auto Paper Pipeline — 自动论文发现与审阅流水线

## 🚨 全局执行纪律（强制）

**本流水线是严格的五阶段串行流程。以下规则具有最高优先级：**

1. **串行执行**：阶段必须按顺序执行；每个阶段的输出是下一阶段的输入。
2. **BLOCKING = 强制暂停**：标记为 ⛔ BLOCKING 的节点必须完全暂停，等待用户明确回复后才能继续。
3. **continue-on-error**：单篇论文/单个搜索源的失败不阻塞整体流程，记录失败详情后继续。

---

## 环境变量要求

**运行本 pipeline 前必须配置以下环境变量**（或写入 `~/.config/paper-fetch/.env`）：

| 环境变量 | 必选 | 用途 | 示例值 |
|---------|------|------|--------|
| `CROSSREF_MAILTO` | ✅ 强烈推荐 | Crossref 礼貌池 + Wiley/Science/PNAS 等 provider 必需 | `pipeline@example.com` |
| `ELSEVIER_API_KEY` | ❌ | Elsevier 全文下载 | — |
| `CLOAKBROWSER_HEADLESS` | ❌ | 浏览器无头模式 | `true` |

**快速设置**：
```bash
mkdir -p ~/.config/paper-fetch
cat > ~/.config/paper-fetch/.env << 'EOF'
CROSSREF_MAILTO=your-email@example.com
CLOAKBROWSER_HEADLESS=true
EOF
```

若无 `CROSSREF_MAILTO`，Wiley/Science/PNAS/ACS/IOP/AIP/MDPI 等 provider 的全文获取会降级为元数据。

## 前置依赖

| Skill | 用途 | 协作方式 | 最低版本 |
|-------|------|---------|---------|
| paper-fetch-skill | 下载论文全文（Markdown） | MCP 直调 + 健康检查 + **CLI 降级（推荐）** | >=0.1.0 |
| paper-obsidian-review | 结构化审阅模板 | SKILL.md 引用式读取 | — |

**启动前检查**：
1. 确认以上两个 skill 已安装
2. 确认 `paper-fetch` CLI 可用：`which paper-fetch`
3. 确认 `CROSSREF_MAILTO` 环境变量已设置
4. 若缺失 CLI，提示用户安装后再启动流水线

## 流水线脚本路径

所有 Python 脚本位于 `pipeline/`，配置文件位于 `config/`。

在项目根目录 `.venv` 中运行：
```bash
VENV=".venv/bin/python"
```

## 搜索源配置（唯一权威入口）

**搜索源的唯一权威配置文件**为 `config/search_sources.yaml`。

该文件同时包含：
- **搜索源定义**（`sources` 节）：API 端点、参数模板、重试策略、默认启用状态、领域归属
- **可下载能力矩阵**（`downloadable_providers` 节）：paper-fetch 支持的 Provider 及其 DOI 前缀映射

**职责边界**：
- `pipeline`（编排层）：读取 `sources` 节，驱动搜索 API 调用；读取 `downloadable_providers` 节，在打分阶段计算可下载性加分
- `paper-fetch-skill`（全文获取层）：实际执行 DOI→Provider 路由和全文获取，不直接读取此配置
- **新增搜索源只需修改此 YAML 文件**，无需改动 Python 常量或其他配置文档

`docs/search-apis.md` 为人类可读的 API 参考文档。

---

## 五阶段流水线

### 阶段 1：参数确认（AI 对话，无脚本） ⛔ BLOCKING

与用户对话收集以下参数，构造 `pipeline_params.json`：

| 参数 | 必选 | 默认值 | 说明 |
|------|------|--------|------|
| `keywords` | ✅ | — | 领域关键词（中文/英文均可） |
| `search_queries` | ✅ | — | AI 将关键词翻译为英文查询词 |
| `exclude_keywords` | ❌ | [] | 排除关键词 |
| `keyword_weights` | ❌ | {} | 各关键词权重，默认 1.0 |
| `language` | ❌ | "en" | 语言偏好 |
| `paper_types` | ❌ | [] | 论文类型偏好 |
| `year_from` | ❌ | 2018 | 起始年份 |
| `year_to` | ❌ | 当前年份 | 截止年份 |
| `preferred_journals` | ❌ | [] | 偏好期刊 |
| `top_n` | ❌ | 10 | 返回数量 |
| `save_dir` | ❌ | "papers" | 保存目录名（在 `outputs/<run_id>/` 下，分为 `fulltext/` 和 `reviews/` 子目录） |
| `vault_root` | ✅ | — | Obsidian vault 根目录 |
| `asset_profile` | ❌ | "body" | 图片资源偏好 |
| `domain` | ❌ | "" | 领域标识（用于领域补充模板） |
| `search_sources` | ❌ | 参考 registry 默认值 | 搜索源列表（从 registry 自动读取默认启用项） |

**AI 翻译关键词**：将用户输入的中文关键词翻译为英文查询词，记录到 `search_queries`。例如用户输入"植物基因组基础模型"，AI 翻译为 `["plant genome foundation model", "plant LLM", "gene discovery deep learning"]`。

**搜索源自动开启规则**：`search_source_registry.yaml` 中 `auto_enable_rules` 定义了按领域自动开启的搜索源（如 `domain=plant_genomics` 时自动启用 PubMed）。

**收集完毕后**：
1. 将参数写入 `pipeline_params.json`
2. 展示给用户确认
3. ⛔ BLOCKING：等待用户确认后才进入阶段 2

---

### 阶段 2：多源搜索（`pipeline/search.py`）

```bash
$VENV pipeline/search.py \
    --keywords "<search_queries 逗号连接>" \
    --sources "<search_sources 逗号连接>" \
    --year-from <year_from> \
    --year-to <year_to> \
    --max-results 100 \
    --output search_results.json \
    --verbose
```

**退出码处理**：
- 0：成功，继续
- 1：部分来源失败但有结果，向用户报告失败的来源，继续
- 2：全部失败或无结果，⛔ BLOCKING 询问用户是否调整关键词重新搜索

**阶段 2 结束后**：
1. 展示搜索统计：各来源数量、去重统计、摘要覆盖率
2. ⛔ BLOCKING：用户可调整关键词重新搜索，或确认进入阶段 3

---

### 阶段 3：打分排序（`pipeline/score.py`）

```bash
$VENV pipeline/score.py \
    --input search_results.json \
    --config config/scoring.yaml \
    --params pipeline_params.json \
    --top-n <top_n> \
    --output top_n_dois.json \
    --verbose
```

**阶段 3 结束后**：
1. 展示 Top N 列表 + 分数明细
2. ⛔ BLOCKING：用户可手动剔除/增加论文，或确认进入阶段 4

---

### 阶段 4：批量下载（CLI 优先）

**4.0 前置：CLI 可用性检查**

```bash
which paper-fetch && echo "CLI OK" || echo "CLI MISSING"
export CROSSREF_MAILTO="${CROSSREF_MAILTO:-pipeline@example.com}"
```

CLI 不可用时 → ⛔ BLOCKING 向用户报告并建议安装 `pip install -e path/to/paper-fetch-skill/`。

**4.1 下载策略：按可下载性分组**

从 `top_n_dois.json` 中读取每篇论文的 `score_breakdown.download_provider`：

| Provider | 可靠性 | 策略 |
|----------|--------|------|
| `springer` / `plos` / `arxiv` / `copernicus` / `royal_society` / `oxford_academic` | **high** | ✅ 直接下载，预期成功 |
| `mdpi` / `iop` / `aip` / `annual_reviews` | **medium** | ⚠️ 尝试下载，可能降级元数据 |
| `wiley` / `science` / `pnas` / `ieee` / `acs` | **low** | ⚠️ 需要 CloakBrowser，若无头环境可能失败 |
| `elsevier` | **low** | ❌ 需要 `ELSEVIER_API_KEY` |
| `biorxiv` | **medium** | ⚠️ bioRxiv/medRxiv 支持全文 PDF + API 元数据 |
| `none` | **none** | ❌ 无对应 provider，只能获取元数据 |

**优先下载高可靠性论文**，然后按需尝试低可靠性论文。

**4.2 下载命令（CLI）**

```bash
# 逐篇下载
CROSSREF_MAILTO="${CROSSREF_MAILTO}" paper-fetch \
    --query "<DOI>" \
    --output-dir "<run_output_dir>/papers/fulltext/" \
    --save-markdown \
    --artifact-mode none

# 批量下载（生成 query file）
python3 -c "
import json, sys
with open('top_n_dois.json') as f:
    for p in json.load(f)['papers']:
        bd = p.get('score_breakdown', {})
        if bd.get('download_provider') in {'springer','plos','arxiv','copernicus'}:
            print(p.get('doi') or p.get('arxiv_id',''))
" > /tmp/queries_high_reliability.txt

CROSSREF_MAILTO="${CROSSREF_MAILTO}" paper-fetch \
    --query-file /tmp/queries_high_reliability.txt \
    --output-dir "<run_output_dir>/papers/fulltext/" \
    --batch-concurrency 2
```

**4.3 失败处理**

- 单篇无全文 → 标记 `metadata_only`，保存元数据摘要
- 单篇超时/报错 → 跳过，记入 `pipeline_state.json` 的 failed 列表
- 下载完成后统计成功/失败/仅元数据数量

**4.4 阶段 4 状态持久化**

每下载一篇后更新 `pipeline_state.json`（格式同阶段 2 状态文件，新增 `fetch_progress` 字段）。

---

### 阶段 5：结构化审阅（按合并模板生成）

**5.1 读取模板**

1. 读取 `paper-obsidian-review` 的 SKILL.md 获取标准单篇笔记结构和对比笔记结构
2. 从 `config/domain_supplement.yaml` 读取当前领域（由 `pipeline_params.json` 中的 `domain` 指定）的额外章节

**5.2 逐篇生成 Obsidian 笔记**

对 `<run_output_dir>/papers/fulltext/` 下的每篇论文 Markdown：
1. 读取论文全文
2. 按合并模板（标准结构 + 领域补充）生成 Obsidian 笔记
3. 笔记保存到 `<run_output_dir>/papers/reviews/` 下，文件名格式：`review_<原文件名>.md`

**单篇笔记结构**（来自 paper-obsidian-review + 领域补充）：
- 元数据区（frontmatter）
- 研究背景与问题
- 核心方法
- 主要发现
- 局限性
- **[领域补充章节]**（从 domain-supplement.yaml 插入）
- 与项目指标关联
- 个人评注

**5.3 生成总览对比笔记**

所有论文审阅完成后，生成一份总览对比笔记：
- 保存到 `<run_output_dir>/papers/reviews/`
- 文件名：`overview_<run_id>.md`
- 内容：所有论文的方法对比表、结论对比、趋势总结

**5.4 失败处理**

- 论文仅有摘要 → 按"摘要级审阅"生成笔记，标题标注 `[摘要]`
- 论文 Markdown 解析异常 → 跳过该篇，记录到 failed 列表

---

## 断点续跑

启动时检查当前目录是否存在 `pipeline_state.json`：
- 若存在且 `status` 不是 `completed` → 向用户报告上次进度，⛔ BLOCKING 询问"续跑还是重新开始"
- 若用户选择续跑 → 从 `current_stage` 和 `fetch_progress.pending` 继续
- 若用户换了关键词（`run_id` 不同）→ 清除旧状态重新开始

---

## 最终输出

流水线完成后，输出以下内容：

1. **搜索统计报告**（阶段 2 输出）
2. **Top N 论文列表 + 分数明细**（阶段 3 输出）
3. **下载进度报告**（阶段 4 输出）
4. **Obsidian 笔记**：单篇审阅笔记 + 总览对比笔记（阶段 5 输出）
5. **失败汇总报告**：所有阶段中失败的论文/来源详情

所有中间文件（`pipeline_params.json`、`search_results.json`、`top_n_dois.json`、`pipeline_state.json`）保留在 `outputs/<run_id>/` 根目录，供调试和断点续跑。

论文全文保存在 `outputs/<run_id>/papers/fulltext/`，审阅笔记保存在 `outputs/<run_id>/papers/reviews/`，两者物理分离，互不干扰。

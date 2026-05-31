# paper-catch 架构设计与调试手册

> 本文档系统化记录了项目目录重组方案、各模块职责、数据流依赖关系，以及从端到端测试中积累的调试经验。供后续开发、问题排查和新成员 onboarding 参考。

---

## 一、项目概览

paper-catch 是一个**五阶段自动论文发现与审阅流水线**，核心流程为：

```
用户输入关键词 → [1]参数确认 → [2]多源搜索 → [3]打分排序 → [4]批量下载 → [5]结构化审阅
```

**技术栈**：Python 3.11 + httpx + PyYAML，通过 paper-fetch CLI 下载全文，产出 Obsidian 兼容的 Markdown 笔记。

---

## 二、目录结构

```
paper-catch/
│
├── .venv/                          # Python 虚拟环境
├── requirements.txt                # 顶层依赖 (httpx, pyyaml)
├── .env.example                    # 环境变量模板
├── README.md                       # 项目入口说明
│
├── pipeline/                       # 🔧 核心引擎 —— 阶段 2-3 的实现
│   ├── search.py                   #   多源搜索 + 三级去重
│   └── score.py                    #   预筛 + 综合评分 + Top N 截断
│
├── config/                         # ⚙️ 配置注册表 —— 单一事实源
│   ├── search_sources.yaml         #   搜索源注册表 + 可下载能力矩阵
│   ├── scoring.yaml                #   打分权重、衰减参数
│   ├── journal_tiers.yaml          #   期刊分层映射
│   └── domain_supplement.yaml      #   领域审阅补充模板
│
├── skills/                         # 🤖 Agent Skill —— 阶段 1-5 的编排指令
│   └── auto-paper-pipeline/
│       └── SKILL.md                #   五阶段执行规范（供 AI agent 读取）
│
├── outputs/                        # 📤 运行时输出 —— 每次运行独立子目录
│   └── <run_id>/                   #   例: 20260529-rice-genomics
│       ├── pipeline_params.json    #   阶段 1 输出：用户确认的参数
│       ├── search_results.json     #   阶段 2 输出：去重后论文列表
│       ├── top_n_dois.json         #   阶段 3 输出：打分排序结果
│       └── papers/                 #   阶段 4-5 输出
│           ├── fulltext/           #     阶段 4：下载的论文全文/元数据
│           │   └── 10.xxxx_*.md
│           └── reviews/            #     阶段 5：审阅笔记 + 对比综述
│               ├── review_*.md     #       单篇审阅笔记
│               └── overview_*.md   #       多篇横向对比笔记
│
├── docs/                           # 📚 项目文档
│   ├── PRD.md                      #   产品需求文档
│   ├── architecture.md             #   本文档
│   ├── scoring-rules.md            #   打分公式说明（人类可读）
│   └── search-apis.md              #   API 接口参考
│
└── paper-fetch-skill/              # 📦 外部依赖（pip install -e 安装）
```

### 各目录职责

| 目录 | 类型 | 职责 | 依赖 |
|------|------|------|------|
| `pipeline/` | 源码 | 阶段 2 搜索引擎、阶段 3 打分引擎 | → `config/` (读取注册表) → `outputs/<run>/` (写入) |
| `config/` | 配置 | 搜索源注册、打分参数、期刊分层、领域模板 | 无（被 `pipeline/` 和 `skills/` 读取） |
| `skills/` | 指令 | 五阶段编排流程、阻塞点、降级策略 | 引用 `pipeline/` 脚本路径和 `config/` 文件 |
| `outputs/` | 数据 | 每次运行的完整产物闭环 | 无（纯输出） |
| `docs/` | 文档 | PRD、架构、API 参考 | 无 |

### 设计原则

| 原则 | 体现 |
|------|------|
| **代码配置分离** | `pipeline/` 只放 `.py`，`config/` 只放 `.yaml`，互不交叉 |
| **一次运行一个子目录** | `outputs/<run_id>/` 内四阶段产物自闭环，便于归档、对比、断点续跑 |
| **原文与笔记分离** | `papers/fulltext/` 存下载原文，`papers/reviews/` 存审阅笔记，不同阶段产物不混放 |
| **单一事实源** | `config/search_sources.yaml` 是搜索源+下载能力的唯一注册入口；新增搜索源只改这一个文件 |
| **AI/Human 可读** | `skills/SKILL.md` 供 AI agent 解析执行，`docs/*.md` 供开发者阅读，`config/*.yaml` 被代码程序化读取 |
| **无跨阶段强耦合** | 每个阶段的输出是独立 JSON 文件，下游阶段通过文件路径指定的 `--input` 读取，不依赖内存状态 |

---

## 三、数据流与依赖关系

```
                    ┌──────────────────────────────────────────────┐
                    │              用户输入关键词                    │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │  阶段 1: AI 对话收集参数 → pipeline_params.json │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │  阶段 2: pipeline/search.py                   │
                    │    ├── 读取 config/search_sources.yaml        │
                    │    ├── 并行查询 5 个 API                       │
                    │    ├── 三级去重                                │
                    │    └── → search_results.json                  │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │  阶段 3: pipeline/score.py                    │
                    │    ├── 读取 config/scoring.yaml               │
                    │    ├── 读取 config/journal_tiers.yaml         │
                    │    ├── 读取 config/search_sources.yaml        │
                    │    │     (downloadable_providers 节)          │
                    │    ├── 预筛 → 综合评分 → Top N 截断            │
                    │    └── → top_n_dois.json                      │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │  阶段 4: paper-fetch CLI                      │
                    │    ├── CROSSREF_MAILTO 环境变量               │
                    │    ├── DOI → Provider 路由                    │
                    │    └── → papers/fulltext/*.md                 │
                    └─────────────────┬────────────────────────────┘
                                      │
                    ┌─────────────────▼────────────────────────────┐
                    │  阶段 5: paper-obsidian-review (AI)           │
                    │    ├── 读取 config/domain_supplement.yaml     │
                    │    └── → papers/reviews/*.md                  │
                    └──────────────────────────────────────────────┘
```

### 文件间引用关系

```
pipeline/search.py
  ├── 硬依赖: httpx, yaml
  ├── 读: config/search_sources.yaml (REGISTRY_PATH)
  ├── 环境变量: CROSSREF_MAILTO (Crossref 礼貌池)
  └── 输出: search_results.json

pipeline/score.py
  ├── 硬依赖: yaml
  ├── 读: config/search_sources.yaml (downloadable_providers 节)
  ├── 读: config/scoring.yaml
  ├── 读: config/journal_tiers.yaml
  ├── 读: pipeline_params.json (可选，提取关键词权重)
  └── 输出: top_n_dois.json

skills/auto-paper-pipeline/SKILL.md
  └── 引用: pipeline/search.py, pipeline/score.py, config/*.yaml 路径
```

---

## 四、搜索源注册表设计

`config/search_sources.yaml` 是唯一权威配置入口，包含两个逻辑分区：

### 4.1 `sources` 节 —— 搜索 API 定义

每个搜索源包含：
- `display_name`：人类可读名称
- `api_endpoint`：API 端点 URL
- `rate_limit`：频率限制说明
- `default_enabled`：默认是否启用
- `auto_enable_rules`：按 domain 自动启用条件（如 PubMed 在 `plant_genomics` domain 下自动开启）
- `retry`：重试策略（max_retries, backoff 间隔, retry_on 状态码）
- `provider_hint`：关联的 paper-fetch-skill provider 名称

### 4.2 `downloadable_providers` 节 —— 下载能力矩阵

声明 paper-fetch-skill 支持的全文获取 Provider，每个 provider 包含：
- `doi_prefixes`：DOI 前缀列表（用于路由匹配）
- `reliability`：`high` / `medium` / `low` / `none`
- `note`：备注（如是否需要 API key）

**reliability 评级标准**：

| 级别 | 含义 | 代表 Provider | 条件 |
|------|------|-------------|------|
| `high` | 无需凭证，CLI 直连即可下载 | springer, plos, arxiv, copernicus, oxford_academic, royal_society | 开放获取或通用爬虫绕过 |
| `medium` | 大概率可下载，可能降级 | mdpi, biorxiv | 开放获取或 PDF 直链 |
| `low` | 需要 API key 或 CloakBrowser 且环境受限 | wiley, science, pnas, elsevier | 付费墙 + 需要密钥 |
| `none` | paper-fetch 不支持此平台 | frontiers | 无对应 provider |

### 4.3 扩展新搜索源

只需在 `search_sources.yaml` 的 `sources` 节添加一条记录，在 `pipeline/search.py` 的 `source_handlers` 字典中注册处理函数即可。无需修改常量定义或其他配置文件。

---

## 五、流水线脚本设计

### 5.1 `pipeline/search.py` —— 搜索引擎

**入口**：`main()` → `run_search()`

**核心流程**：
1. 加载注册表 `load_registry(config/search_sources.yaml)`
2. 对每个关键词 × 每个搜索源，调用对应 handler
3. 三级去重（DOI 精确 → arXiv ID 映射 → 标题模糊）
4. 统计摘要覆盖率、来源分布
5. 写入 `search_results.json`，返回退出码

**退出码契约**：
- `0`：全部来源成功，有结果
- `1`：部分来源失败但有结果
- `2`：全部失败或无结果

**HTTP 重试机制**（`_request_with_retry`）：
- 从注册表读取各源的 `retry` 配置
- 429 / 5xx 触发指数退避重试
- 其他 4xx 不重试，直接标记失败

### 5.2 `pipeline/score.py` —— 打分引擎

**入口**：`main()` → `run_scoring()`

**阶段 3a: 预筛**（`prefilter`）：
1. 排除关键词过滤（命中排除词则丢弃）
2. 最低关键词命中数（不足则丢弃）
3. 论文类型过滤（不在白名单则丢弃）

**阶段 3b: 综合评分**（`score_papers`）：
```
total_score = 0.40 × keyword_match     # 关键词匹配度
            + 0.30 × citation_weighted  # 引用数加权（对数归一化）
            + 0.20 × recency_decay      # 时效衰减（指数衰减）
            + 0.10 × journal_quality    # 期刊质量（分层）
            + downloadability_bonus     # 可下载性加分/减分
```

**可下载性评分**（`get_downloadability`）：
- 从注册表 `downloadable_providers` 节读取 DOI 前缀映射
- arXiv ID 优先匹配
- 返回 `(provider_name, reliability)` 元组
- 加到总分中（high +0.15, medium +0.05, low 0.0, none -0.15）

---

## 六、Debug 经验总结

以下为端到端测试中遇到的典型问题及解决方案。

### 6.1 搜索源相关

#### Semantic Scholar 返回 429

**现象**：所有请求返回 HTTP 429，`search_semantic_scholar` 重试耗尽。

**原因**：免费 tier 速率限制极低（~1 req/s），连续多关键词查询立即触发限流。

**排查步骤**：
1. 检查 `_request_with_retry` 日志：`[semantic_scholar] HTTP 429，第 N 次重试`
2. 检查注册表 `retry.backoff` 配置是否足够长

**解决方案**：
- 在关键词循环中增加 `time.sleep(1.0)` 间隔（已实施）
- 降低 `--max-results` 至 10-20
- 若持续被限流，优先使用 Crossref（无频率限制）

#### arXiv 返回 429

**现象**：同 Semantic Scholar。

**原因**：arXiv API 建议间隔 3 秒，连续请求触发限流。

**解决方案**：
- 注册表中 arXiv 的 `retry.backoff` 已设为 `[3, 6, 12]`（较长间隔）
- 若连续多个关键词都查 arXiv，可考虑只查第一个关键词

#### PubMed 返回 302 重定向

**现象**：`[pubmed] HTTP 302，不重试`。

**原因**：NCBI E-utilities 反滥用检测——缺少 `email` 和 `tool` 参数、缺少 User-Agent 头、或请求过于频繁。

**解决方案**（已实施）：
```python
params={"email": "pipeline@example.com", "tool": "auto-paper-pipeline"}
headers={"User-Agent": "AutoPaperPipeline/0.4 (mailto:...)"}
```
- PubMed 在 `auto_enable_rules` 中配置为仅在生命科学 domain 下自动开启
- 默认 `default_enabled: false`，减少不必要的请求

#### bioRxiv 无结果

**现象**：搜索返回 0 篇或极少论文。

**原因**：bioRxiv API 不支持原生关键词搜索，只能按日期范围检索后在本地做关键词匹配。过于专业的查询词（如 "rice genome foundation model"）在预印本标题中匹配不到。

**解决方案**：
- bioRxiv 更适合宽泛的领域检索，不适合精确查询
- 拆分查询词为更短的关键词组合

#### Crossref 最稳定

**现象**：在所有测试中，只有 Crossref 持续返回稳定结果（29 篇/次）。

**总结**：Crossref 是**最可靠的搜索源**，原因：
- 元数据最全（覆盖所有正式发表论文）
- 无频率限制（礼貌池 50 req/s）
- 支持复杂过滤（日期范围、字段选择）
- 只需 `mailto:` User-Agent 即可获得 polite pool

**建议**：在组合搜索源时，始终包含 `crossref`。

### 6.2 下载相关

#### MCP `fetch_paper` 返回空数组 `[]`

**现象**：MCP `paper-fetch:fetch_paper` 对 Wiley/Springer/Elsevier DOI 返回 `[]`，但对 Frontiers 能返回元数据。

**原因**：
- Provider 路由在执行阶段静默失败（抛出异常但被外层 try/except 吞掉）
- 只有不走 Provider 路由的通用 "crossref metadata fallback" 能成功返回
- MCP server 的日志通过 `PaperFetchLogBridge` 桥接到客户端，主对话中不可见

**解决方案**：
- ✅ **改用 CLI 模式**（推荐）：`paper-fetch --query <DOI> --save-markdown --output-dir ...`
- CLI 输出完整错误堆栈，便于定位
- 已验证 5 篇 Springer 论文通过 CLI 成功下载全文

#### Wiley 下载超时（CloakBrowser 二进制下载失败）

**现象**：
```
httpx.ConnectTimeout: timed out
cloakbrowser/download.py: _download_file failed
```

**原因**：Wiley provider 需要 CloakBrowser（Playwright 封装），CloakBrowser 首次运行时会从 GitHub Releases 下载浏览器二进制。当前服务器无法访问 GitHub。

**解决方案**：
- 在能访问 GitHub 的环境中运行一次 `paper-fetch`，让 CloakBrowser 缓存二进制
- 手动从 CDN 下载 chromium headless shell 到 `~/.cache/ms-playwright/`
- 或在 `CLOAKBROWSER_BINARY_PATH` 环境变量中指定已有浏览器路径
- 若无法解决，Wiley/Science/PNAS 等需 CloakBrowser 的 provider 一律降级为元数据

#### bioRxiv 无法下载全文（已修复）

**现象**：`content_kind: "abstract_only"` / `has_fulltext: false`。

**原因**：paper-fetch-skill 2.0 的 provider 列表最初不包含 bioRxiv。bioRxiv/medRxiv 的 DOI 只能通过 Crossref 元数据通道获取摘要。

**已修复**：paper-fetch-skill 已新增 `BiorxivClient` provider，支持 bioRxiv/medRxiv 全文 PDF 获取与 API 元数据检索。`config/search_sources.yaml` 中 bioRxiv 可靠性已从 `none` 升级为 `medium`。

#### Frontiers 无法下载全文

**现象**：`content_kind: "abstract_only"` / `has_fulltext: false`。

**原因**：paper-fetch-skill 不包含 Frontiers Media provider。Frontiers 的 DOI 只能通过 Crossref 元数据通道获取摘要。

**NOTE**：`config/search_sources.yaml` 中 Frontiers 对应的可靠性仍为 `none`。

#### Frontiers 无法下载全文

#### `CROSSREF_MAILTO` 未配置的影响

**现象**：Wiley/Science/PNAS/ACS/IOP/AIP/MDPI provider 全部静默失败。

**原因**：这些 provider 的 `env_requirements` 中声明了 `CROSSREF_MAILTO`。未配置时，provider 在初始化阶段就跳过全文获取。

**解决方案**：
```bash
mkdir -p ~/.config/paper-fetch
echo "CROSSREF_MAILTO=your-email@example.com" > ~/.config/paper-fetch/.env
```
或在命令行中直接 `export CROSSREF_MAILTO="..."`。

### 6.3 路径相关

#### `journal-tiers.yaml` vs `journal_tiers.yaml`

**现象**：打分脚本报告 `WARNING: 期刊分层映射表 journal-tiers.yaml 不存在`。

**原因**：代码中硬编码 `"journal-tiers.yaml"`（连字符），实际文件名为 `journal_tiers.yaml`（下划线）。

**修复**：将 `pipeline/score.py` 中的 `load_journal_tiers` 函数路径改为 `journal_tiers.yaml`。

**教训**：配置文件命名应在整个项目中保持一致。建议统一使用下划线分隔（Python 惯例）。

#### 运行目录 ≠ 脚本目录时的相对路径

**现象**：从 `outputs/<run_id>/` 运行 `../../pipeline/score.py --config ../../config/scoring.yaml` 时，`config_path.parent` 解析取决于 CWD。

**解决方案**：`score.py` 中已处理——未提供 `--config` 时，自动从脚本所在目录的相对路径 (`Path(__file__).parent.parent / "config"`) 查找。

**建议**：始终使用绝对路径或在项目根目录运行脚本。

### 6.4 环境相关

#### `paper-fetch` CLI 未安装

**检查**：`which paper-fetch`

**安装**：
```bash
cd paper-fetch-skill && pip install -e ".[cli]"
```

#### Playwright 浏览器未安装

**检查**：`playwright install --dry-run chromium`

**安装**：`playwright install chromium`

#### 完整环境就绪检查清单

```bash
# 1. Python venv
test -d .venv && echo "✅ venv" || echo "❌ 运行: python3 -m venv .venv"

# 2. 依赖
.venv/bin/pip check 2>&1 | grep -q "No broken" && echo "✅ 依赖" || echo "❌ 运行: .venv/bin/pip install -r requirements.txt"

# 3. paper-fetch CLI
which paper-fetch >/dev/null 2>&1 && echo "✅ paper-fetch CLI" || echo "❌ 运行: pip install -e paper-fetch-skill/"

# 4. CROSSREF_MAILTO
test -n "$CROSSREF_MAILTO" -o -f ~/.config/paper-fetch/.env && echo "✅ CROSSREF_MAILTO" || echo "⚠️  设置: export CROSSREF_MAILTO=..."

# 5. Playwright
python3 -c "from playwright.sync_api import sync_playwright; print('✅ Playwright')" 2>&1 || echo "⚠️  运行: playwright install chromium"
```

---

## 七、常见故障速查表

| 症状 | 根因 | 修复 |
|------|------|------|
| 所有搜索源返回 0 篇 | 关键词太专业/API 全部限流 | 用 Crossref 单源重试；拆分短关键词 |
| `score.py` 报 `journal-tiers.yaml 不存在` | 文件名下划线 vs 连字符 | 检查 `config/journal_tiers.yaml` 是否存在 |
| `score.py` 关键词匹配度全 0 | 多词短语整体匹配未命中 | 已加入逐词回退匹配（partial match）；检查 `keyword_weights` 是否与 `keywords` 对齐 |
| paper-fetch CLI 返回空无输出 | 环境变量未配置 | `export CROSSREF_MAILTO=...` |
| 下载论文仅 10 行元数据 | Provider 路由失败，降级 | 检查 `content_kind` 字段；若是 `abstract_only` 则无法获取全文 |
| `.venv` 找不到解释器 | venv 路径不对 | 从项目根目录运行：`.venv/bin/python` |

---

## 八、开发经验

### [v1.1] 论文原文与审阅笔记目录分离

**核心问题**：阶段 4（下载论文）和阶段 5（审阅笔记）的输出混放在同一 `papers/` 目录下，导致：

1. **文件职责不清**：DOI 前缀的 `.md` 文件（论文原文）与 `review_*.md`/`overview_*.md`（审阅笔记）混在一起，难以区分"原始数据"与"加工产物"
2. **Obsidian 体验差**：vault 中全文和笔记并列显示，阅读时噪音大；笔记中的 `[[]]` 链接指向同目录文件，缺乏层次感
3. **下游处理困难**：如需对全文做批量处理（统计字数、抽取关键词），需先过滤掉审阅笔记文件
4. **架构文档与实现不一致**：`architecture.md` 原先只写了 `papers/` 一层，未区分两种产物的存放位置

**解决思路**：

将 `papers/` 拆分为两个子目录：
- `papers/fulltext/`：阶段 4 下载的论文原文/元数据（只读原料）
- `papers/reviews/`：阶段 5 生成的审阅笔记 + 对比综述（加工产物）

**关键改动点**：

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `docs/architecture.md` | 更新 | 目录结构图改为 `fulltext/` + `reviews/`；数据流图更新路径；设计原则新增"原文与笔记分离" |
| `docs/PRD.md` | 更新 | 阶段 4/5 输出路径；MCP/CLI 示例；`save_dir` 默认值从 `.papers/` 改为 `papers` |
| `skills/auto-paper-pipeline/SKILL.md` | 更新 | 下载目标目录改为 `papers/fulltext/`；审阅笔记保存到 `papers/reviews/`；`save_dir` 参数说明 |
| `README.md` | 更新 | 阶段 4/5 输出列 |
| `pipeline/download.py` | 更新 | CLI 帮助文本和示例路径 |
| 已有运行产物 | 迁移 | `outputs/20260529-rice-genomics/papers/` 下文件按新结构重组，审阅笔记内部链接路径同步更新 |

**经验与教训**：

1. **多阶段流水线的产物目录应按阶段/角色分层，而非扁平堆放**。阶段 4 输出的是"原材料"（论文原文），阶段 5 输出的是"加工品"（审阅笔记），两者消费方不同、生命周期不同、更新频率不同，分层存放是自然选择。

2. **目录结构变更需同步更新所有引用点**。本次涉及 5 个文档 + 1 个 Python 脚本 + 已有运行产物的迁移，任何遗漏都会导致 SKILL.md 指引 AI 写入错误路径或 `download.py` 默认参数过时。**排查方法**：`grep -r "papers/" docs/ skills/ pipeline/ README.md` 全局搜索旧路径。

3. **已有产物的迁移不能只移动文件，还需修正内部引用**。审阅笔记中的 `本地笔记：iRice6mA_LMXGB.md` 链接需更新为 `fulltext/iRice6mA_LMXGB.md`，否则 Obsidian 中点击会 404。

4. **`save_dir` 参数语义需随目录结构演进**。原 `save_dir: ".papers/"` 隐含"所有东西放一个目录"，现改为 `save_dir: "papers"` 并在文档中明确"在 `outputs/<run_id>/` 下分为 `fulltext/` 和 `reviews/` 子目录"。参数本身只指定目录名，子目录结构由流水线约定。

### [v1.1] 新增 bioRxiv/medRxiv Provider

**核心问题**：paper-fetch-skill 缺少 bioRxiv/medRxiv provider，导致 `10.1101/` DOI 前缀的论文只能获取摘要，无法下载全文 PDF。在生命科学领域（如 `plant_genomics`），bioRxiv 是最前沿的搜索源，缺少 provider 严重影响流水线实用性。

**解决思路**：

遵循现有 provider 架构，新增 3 个文件实现 bioRxiv 接入：

| 新增文件 | 职责 |
|---------|------|
| `_biorxiv_api.py` | API 客户端层：封装 bioRxiv/medRxiv REST JSON API，支持按 DOI 查询和按日期范围批量查询（含游标分页），自动处理速率限制 |
| `_biorxiv_metadata.py` | 元数据处理层：API 结果 → `ProviderMetadata` 统一格式，DOI 短路探测（`10.1101/` 前缀），作者/日期/服务器名解析 |
| `biorxiv.py` | Provider 主客户端：`BiorxivClient` 继承 `ProviderClient`，实现 `fetch_metadata` + `fetch_raw_fulltext`（PDF + API 元数据富化），注册 `ProviderBundle` |

**经验与教训**：

1. **新增 provider 的文件组织遵循既有约定**：公开入口类放 `providers/<name>.py`，内部辅助模块用 `_` 前缀。这样 `_discover_provider_entry_modules()` 的自动发现机制无需任何修改即可识别新 provider。

2. **bioRxiv API 的特殊性**：
   - 不支持关键词搜索，只支持按 DOI 精确查询或按日期范围检索——因此它只能作为搜索源（通过 `pipeline/search.py` 的 `search_biorxiv` handler 按日期遍历 + 本地关键词匹配），而非通过 paper-fetch 的 query 入口做关键词检索
   - 返回 JSON 而非 XML（与 arXiv 的 Atom XML 不同），解析更简单
   - 同一 API 端点服务 bioRxiv 和 medRxiv 两个服务器，通过 `server` 参数区分

3. **服务器显示名需正确映射**：`biorxiv` → `bioRxiv`（非 `Biorxiviv`），`medrxiv` → `medRxiv`（非 `Medrxiviv`）。使用 `_SERVER_DISPLAY_NAMES` 映射表集中管理，避免多处硬编码拼接 `server.capitalize() + "iv"`。

4. **配置联动更新**：新增 provider 后，需同步更新：
   - `models/schema.py`：`SourceKind` 新增 `"biorxiv_html"` / `"biorxiv_pdf"`
   - `providers/__init__.py`：`_EXPORTS` 新增 `BiorxivClient`
   - `config/search_sources.yaml`：reliability 从 `none` 升级为 `medium`，note 更新
   - `config/scoring.yaml`：`none` 注释中移除 bioRxiv
   - `docs/architecture.md`：reliability 表中 bioRxiv 从 `none` 行移到 `medium` 行

5. **arXiv provider 已存在**：确认已有 `ArxivClient` 完整实现（Atom XML 解析 + HTML 全文提取 + PDF 回退），无需重复开发。

---

## 九、版本记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-05-30 | 1.1 | 论文原文与审阅笔记目录分离（`fulltext/` + `reviews/`）；新增 bioRxiv/medRxiv provider；更新可靠性矩阵 |
| 2026-05-29 | 1.0 | 初始架构文档；目录重组方案；完整 Debug 经验记录 |

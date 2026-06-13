# paper-catch

新领域快速探索流水线：**多源搜索 → 前沿优先排序 → 主题聚类 → 批量下载 → 结构化审阅 → 单个 Markdown 阅读报告**

## 目录结构

```
paper-catch/
├── pipeline/          # 🔧 流水线脚本（搜索 + 打分引擎）
├── config/            # ⚙️  注册表 & 参数配置（唯一事实源）
├── skills/            # 🤖 Agent Skill 定义（编排指令）
├── outputs/           # 📤 运行时输出（每次运行独立子目录）
├── docs/              # 📚 项目文档
├── .venv/             # Python 虚拟环境
└── requirements.txt   # Python 依赖
```

## 快速开始

```bash
# 1. 安装依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 配置 paper-fetch 环境变量
mkdir -p ~/.config/paper-fetch
cp .env.example ~/.config/paper-fetch/.env
# 编辑 ~/.config/paper-fetch/.env，填入你的 CROSSREF_MAILTO

# 3. 配置 paper-fetch 后端
# 默认后端策略是 auto：优先使用 WorkBuddy 兼容 mcp.json 中的 paper-fetch MCP，
# MCP 不可用时回退 paper-fetch CLI。
#
# MCP 配置默认查找：
#   .workbuddy/mcp.json
#   ~/.workbuddy/mcp.json
#   ~/.config/paper-catch/mcp.json
#
# 如需 CLI 回退，可按上游 Releases 或源码安装 paper-fetch：
mkdir -p external
git clone https://github.com/Dictation354/paper-fetch-skill.git external/paper-fetch-skill
cd external/paper-fetch-skill
./install.sh --lite
# 或者只装进当前 Python 环境：
# python3 -m pip install .
cd ../..

# 4. 验证 paper-fetch 后端
.venv/bin/python pipeline/download.py --check-backend --fetch-backend auto

# 可选：强制验证 CLI
paper-fetch --help
paper-fetch --query "10.1186/1471-2105-11-421" \
  --output-dir /tmp/paper-fetch-smoke \
  --artifact-mode none

# 5. 安装本项目 Codex skill
./scripts/install-codex-skill.sh

# 6. 运行本项目 CLI 契约检查
.venv/bin/python pipeline/search.py --help
.venv/bin/python pipeline/score.py --help
```

`paper-fetch` 不随本仓库自动安装，本项目通过本地 MCP server 或 CLI 获取全文，不复制或重写 `Dictation354/paper-fetch-skill` 的 provider 逻辑。默认 `--fetch-backend auto` 会优先使用 MCP，失败后回退 CLI；也可用 `--fetch-backend mcp|cli` 强制指定。MCP 后端优先使用当前 Python 环境中的 `mcp` SDK；若当前环境没有 SDK，但 mcp.json 的 server command 是带有 SDK 的 Python 解释器，也会借用该解释器执行轻量 MCP client。若需要 Elsevier 全文，需在 `~/.config/paper-fetch/.env` 中配置 `ELSEVIER_API_KEY`。

## 六阶段流水线

| 阶段 | 输入 | 脚本/动作 | 输出 |
|------|------|----------|------|
| 1 参数确认 | 用户关键词 | AI 对话 | `pipeline_params.json` |
| 2 多源搜索 | `pipeline_params.json` | `pipeline/search.py` | `search_results.json` |
| 3 打分排序 | `search_results.json` | `pipeline/score.py` | `top_n_dois.json` |
| 4 主题聚类与阅读路线 | `top_n_dois.json` | AI | `cluster_summary.json` |
| 5 批量下载 | `top_n_dois.json` | `pipeline/download.py` + paper-fetch MCP/CLI | `papers/fulltext/*.md` |
| 6 结构化审阅 | `papers/fulltext/*.md` | AI + `config/review_template.yaml` | `阅读报告.md` |

详见 `skills/auto-paper-pipeline/SKILL.md`

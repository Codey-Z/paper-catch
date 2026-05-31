jian# paper-catch

自动论文发现与审阅流水线：**多源搜索 → 预筛打分 → 批量下载 → 结构化审阅 → Obsidian 笔记**

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

# 2. 配置环境变量
cp .env.example ~/.config/paper-fetch/.env
# 编辑 ~/.config/paper-fetch/.env，填入你的 CROSSREF_MAILTO

# 3. 安装 paper-fetch CLI（用于论文下载）
pip install -e paper-fetch-skill/

# 4. 运行测试
.venv/bin/python pipeline/search.py --help
.venv/bin/python pipeline/score.py --help
```

## 五阶段流水线

| 阶段 | 输入 | 脚本/动作 | 输出 |
|------|------|----------|------|
| 1 参数确认 | 用户关键词 | AI 对话 | `pipeline_params.json` |
| 2 多源搜索 | `pipeline_params.json` | `pipeline/search.py` | `search_results.json` |
| 3 打分排序 | `search_results.json` | `pipeline/score.py` | `top_n_dois.json` |
| 4 批量下载 | `top_n_dois.json` | `paper-fetch` CLI | `papers/fulltext/*.md` |
| 5 结构化审阅 | `papers/fulltext/*.md` | AI (paper-obsidian-review) | `papers/reviews/*.md` |

详见 `skills/auto-paper-pipeline/SKILL.md`

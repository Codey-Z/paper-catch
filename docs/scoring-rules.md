# 打分公式说明

`config/scoring.yaml` 是唯一评分配置入口。当前支持三种模式：

- `frontier`：默认，用于新领域快速探索，优先新近、可下载、方法/benchmark 论文。
- `foundation`：用于建立基础认知，优先高引经典、综述和高质量期刊。
- `balanced`：折中模式，接近旧版权重。

V1.2 新增 `config/relevance_profiles.yaml`，用于定义可配置主题相关性 profile。`frontier` 模式默认应结合 relevance profile 使用，避免仅凭 `plant/genome/model` 等弱匹配词排序。

## 综合评分公式

```text
总分 =
  w_topic × 主题相关性
+ w_kw  × 关键词匹配度
+ w_cit × 引用数加权
+ w_rec × 时效衰减
+ w_jrn × 期刊质量
+ 可下载性加分
+ 论文类型加分
```

可下载性和论文类型是独立加分项，不参与权重归一化。

## 模式差异

| 模式 | 关键词 | 引用 | 时效 | 期刊 | 适用场景 |
| --- | ---: | ---: | ---: | ---: | --- |
| frontier | 0.24 | 0.08 | 0.12 | 0.06 | 找新方向、看前沿趋势；另含 `topic_relevance=0.50` |
| foundation | 0.35 | 0.38 | 0.08 | 0.19 | 找经典论文、综述和权威脉络 |
| balanced | 0.40 | 0.30 | 0.20 | 0.10 | 通用筛选 |

## 输出解释字段

`pipeline/score.py` 不只输出分数，还为每篇 Top N 论文生成：

- `selection_reason`：一句话说明为什么入选。
- `risk_flags`：例如 `abstract_only_before_download`、`no_download_provider`、`low_download_reliability`。
- `evidence_level`：阶段 3 根据元数据/摘要推断，阶段 6 可根据全文提升。
- `download_provider` / `download_reliability`：来自 `config/search_sources.yaml` 的下载能力矩阵。
- `cluster_id`：第一版主题聚类标签，如 `method_route`、`dataset_benchmark`。
- `recommended_reading_order`：按综合分排序后的建议阅读顺序。
- `topic_relevance`：主题相关性分数。
- `matched_concept_groups`：命中的概念组。
- `missing_required_groups`：缺失的必需概念组。
- `relevance_flags`：主题相关性风险，如 `weak_topic_relevance`、`missing_ai_model_group`。

## 主题相关性（V1.2）

`--relevance-profile plant_genome_llm` 会读取 `config/relevance_profiles.yaml` 中的概念组：

- `plant_domain`
- `genomics`
- `ai_model`
- `downstream_task`（可选）

每篇论文按标题和摘要匹配概念组。命中所有 required groups 的论文优先进入 Top N；若严格命中结果少于 `fallback_min_results`，则自动退回 soft ranking，并在输出 config 中记录 `profile_gate_fallback=true`。

概念匹配使用词边界匹配，避免短词误命中。例如 `llm` 不会命中普通单词的一部分。

## 各维度

### 关键词匹配度

```text
关键词匹配度 = 命中关键词权重和 / 关键词总权重
```

- 大小写不敏感。
- 短语优先；短语未命中时允许拆词部分匹配。
- 无摘要论文按 `keyword.no_abstract_penalty` 降权。

### 引用数加权

```text
引用数加权 = log(引用数 + 1) / log(citation_max + 1)
```

`citation_max` 默认 5000。

### 时效衰减

```text
时效衰减 = max(floor, exp(-lambda × 论文年龄))
```

`frontier` 模式使用更快衰减，让近年论文更容易进入 Top N。

### 期刊质量

读取 `config/journal_tiers.yaml`。无法匹配时使用默认分值。

### 可下载性加分

读取 `config/search_sources.yaml` 的 `downloadable_providers`：

- `high`：无需凭证或可靠开放获取。
- `medium`：大概率可下载。
- `low`：需要 API key、浏览器或环境条件。
- `none`：无匹配 provider。

### 论文类型加分

根据标题、摘要和 API 类型字段启发式判断：

- `method`
- `dataset_benchmark`
- `empirical_study`
- `review_survey`
- `unknown`

`frontier` 模式提高方法和 benchmark 论文权重，`foundation` 模式提高综述权重。

## 预筛逻辑

正式打分前执行：

1. 排除关键词过滤。
2. 最低关键词命中数过滤。
3. 论文类型白名单过滤。

无法推断论文类型时默认放行，避免误删潜在相关论文。

# 打分公式说明（人类可读）

## 综合评分公式

```
总分 = w_kw × 关键词匹配度 + w_cit × 引用数加权 + w_rec × 时效衰减 + w_jrn × 期刊质量 + 可下载性加分
```

默认权重：`w_kw=0.40, w_cit=0.30, w_rec=0.20, w_jrn=0.10`

可下载性加分为独立加分项（不参与权重归一化），直接加到总分上。

---

## 各维度计算方式

### 1. 关键词匹配度（权重 0.40）

```
关键词匹配度 = 命中关键词数 / 关键词总数
```

- 大小写不敏感匹配
- 子串匹配：关键词 "genome" 可匹配 "genomics"、"genomic"
- 多词短语整体匹配，不拆词重复计算
- 若 `keyword_weights` 中指定了某关键词权重，命中时按权重计分
- 无摘要论文：匹配度按 `0.5 × 实际值` 降权

### 2. 引用数加权（权重 0.30）

```
引用数加权 = log(引用数 + 1) / log(citation_max + 1)
```

- `citation_max` 默认 5000
- 对数归一化避免超高引论文独占
- 引用数为 0 时该维度得 0

### 3. 时效衰减（权重 0.20）

```
时效衰减 = max(floor, exp(-λ × 论文年龄))
```

- `λ` 默认 0.3，`floor` 默认 0.1
- 论文年龄 = 当前年份 - 发表年份
- 示例：1年→0.74，3年→0.41，5年→0.22，10年→0.05（不低于0.1）

### 4. 期刊质量（权重 0.10）

按 `journal-tiers.yaml` 分层映射：

| 层级 | 分值 | 示例 |
|------|------|------|
| tier1 | 1.0 | Nature, Science, Cell |
| tier1_sub | 0.9 | Nature Communications, Nature Genetics |
| tier2 | 0.6 | PNAS, Bioinformatics, Genome Research |
| tier3 | 0.4 | Frontiers 系列, Scientific Reports |
| default | 0.2 | 其他期刊 / arXiv 预印本 |

### 5. 可下载性加分（独立加分项）

根据论文 DOI 前缀或 arXiv ID 匹配 `search_source_registry.yaml` 中的 `downloadable_providers`，
判断论文是否能被 paper-fetch 成功获取全文：

| 可靠性 | 加分 | 说明 | 示例 Provider |
|--------|------|------|--------------|
| high | +0.3 | 几乎一定能下载全文 | arxiv, plos |
| medium | +0.15 | 大概率能下载 | springer, mdpi, copernicus |
| low | 0.0 | 需密钥/可能失败 | elsevier, wiley, ieee |
| none | -0.2 | 无匹配 Provider，拿不到全文 | Frontiers, 不知名期刊 |

**设计理由**：之前搜索结果大量来自 Frontiers/Wiley/Elsevier，虽然引用数高、期刊质量尚可，
但全文无法获取，导致阶段 4 全部落空。可下载性加分让搜索结果偏向"能实际下载"的论文，
从源头解决搜索源与下载能力的错配问题。

---

## 预筛逻辑（阶段 3a）

在正式打分前执行轻量级预筛：

1. **排除关键词过滤**：标题或摘要命中任一排除关键词 → 直接排除
2. **最低关键词命中数**：命中数低于阈值 → 直接排除
3. **论文类型过滤**：不符合类型白名单 → 直接排除

预筛排除的论文不进入打分，但在 verbose 输出中报告统计。

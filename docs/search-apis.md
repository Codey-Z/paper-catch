# 搜索 API 接口文档

> 本文件描述 `config/search_sources.yaml` 中的搜索源配置。
> 修改搜索源时，以 `config/search_sources.yaml` 为唯一事实源。

## 搜索源概览

| API | 请求频率 | 返回字段 | 覆盖范围 | 前沿性 |
|-----|---------|---------|---------|--------|
| Semantic Scholar | 1 req/s (free), 5 req/s (with key) | 标题、DOI、摘要、引用数、年份、venue、paperType | 综合学术 | 中（索引有 1-3 月延迟） |
| Crossref | 50 req/s (free) | 标题、DOI、作者、期刊、摘要（部分） | 元数据最全 | 低（正式发表后收录） |
| arXiv | 无硬性限制，建议间隔 3 秒 | 标题、arXiv ID、摘要、分类 | CS/物理/定量生物 | 高（预印本首发） |
| bioRxiv | 无硬性限制 | 标题、DOI、摘要、分类、发布日期 | 生命科学预印本 | 极高（比正式发表早 6-12 月） |
| PubMed | 3 req/s (free), 10 req/s (with API key) | 标题、DOI、摘要、期刊、MeSH 词 | 生物医学最全索引 | 中（含 ahead-of-print） |

---

## 各 API 端点与参数

### Semantic Scholar

- **搜索端点**: `https://api.semanticscholar.org/graph/v1/paper/search`
- **方法**: GET
- **参数**:
  - `query` （必填） 类型: string
  - `year` 类型: string 示例: `2022-2026`
  - `limit` 类型: int 默认: 100 最大: 100
  - `fields` 类型: string 默认: title,externalIds,abstract,citationCount,year,venue,authors,paperType
- **速率限制**: 1 req/s (free), 5 req/s (with key)
- **重试策略**: 3 次，退避 [2, 4, 8]，重试状态码 [429, 500, 502, 503]
- **默认启用**: 是

---

### Crossref

- **搜索端点**: `https://api.crossref.org/works`
- **方法**: GET
- **参数**:
  - `query` （必填） 类型: string
  - `filter` 类型: string 示例: `from-pub-date:2022,until-pub-date:2026`
  - `rows` 类型: int 默认: 20 最大: 1000
  - `select` 类型: string
- **速率限制**: 50 req/s (free)
- **重试策略**: 3 次，退避 [2, 4, 8]，重试状态码 [429, 500, 502, 503]
- **默认启用**: 是
- **对应 paper-fetch Provider**: `crossref`

---

### arXiv

- **搜索端点**: `https://export.arxiv.org/api/query`
- **方法**: GET
- **参数**:
  - `search_query` 类型: string 示例: `all:"plant genome foundation model"`
  - `id_list` 类型: string — 精确 ID 查询，逗号分隔
  - `start` 类型: int 默认: 0
  - `max_results` 类型: int 默认: 50
  - `sortBy` 类型: enum 默认: relevance
- **速率限制**: 无硬性限制，建议间隔 3 秒
- **重试策略**: 3 次，退避 [3, 6, 12]，重试状态码 [429, 500, 503]
- **默认启用**: 是
- **对应 paper-fetch Provider**: `arxiv`

---

### bioRxiv

- **搜索端点**: `https://api.biorxiv.org/details/biorxiv`
- **方法**: GET
- **参数**:
  - `cursor` 类型: int 默认: 0
  - `limit` 类型: int 默认: 100 最大: 100
- **速率限制**: 无硬性限制
- **重试策略**: 3 次，退避 [2, 4, 8]，重试状态码 [429, 500, 502, 503]
- **注意**: 无原生关键词搜索，按日期范围检索后在标题/摘要中做关键词匹配
- **默认启用**: 是

---

### PubMed

- **Search端点**: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi`
- **Fetch端点**: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi`
- **方法**: GET
- **esearch 参数**:
  - `db` 固定值: `pubmed`
  - `term` （必填） 类型: string
  - `mindate` 类型: string
  - `maxdate` 类型: string
  - `retmax` 类型: int 默认: 100
  - `retmode` 固定值: `json`
  - `api_key` 类型: string 环境变量: PUBMED_API_KEY
- **efetch 参数**:
  - `db` 固定值: `pubmed`
  - `id` （必填） 类型: string — PMIDs 逗号分隔
  - `retmode` 固定值: `xml`
- **速率限制**: 3 req/s (free), 10 req/s (with API key)
- **重试策略**: 3 次，退避 [2, 4, 8]，重试状态码 [429, 500, 502, 503]
- **自动开启条件**: 当 `domain` 包含 ['plant_genomics', 'single_cell', 'epigenomics', 'biomedicine'] 时自动启用
- **默认启用**: 否

---

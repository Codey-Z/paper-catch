#!/usr/bin/env python3
"""
search_papers.py — 多源论文搜索脚本

并行查询 Semantic Scholar / Crossref / arXiv / bioRxiv / PubMed，
三级去重后输出 search_results.json。

退出码：0=成功（有结果），1=部分来源失败但有结果，2=全部失败或无结果
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

# ──────────────────────────── 常量 ────────────────────────────

CURRENT_YEAR = datetime.now().year

# 搜索源注册表路径（唯一权威配置入口）
REGISTRY_PATH = Path(__file__).parent.parent / "config" / "search_sources.yaml"

# HTTP 超时
HTTP_TIMEOUT = 30.0

# Crossref 礼貌池邮箱（从环境变量读取）
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "pipeline@example.com")

logger = logging.getLogger(__name__)


# ──────────────── 注册表加载函数 ────────────────────────────


def load_registry(registry_path: str | Path | None = None) -> dict:
    """加载搜索源注册表 YAML"""
    path = Path(registry_path) if registry_path else REGISTRY_PATH
    if not path.exists():
        logger.warning("注册文件 %s 不存在，使用内置默认值", path)
        return _fallback_registry()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_default_sources(registry: dict | None = None) -> list[str]:
    """从注册表获取默认启用的搜索源列表"""
    if registry is None:
        registry = load_registry()
    return [k for k, v in registry.get("sources", {}).items() if v.get("default_enabled")]


def get_valid_sources(registry: dict | None = None) -> set[str]:
    """从注册表获取所有合法搜索源名称集合"""
    if registry is None:
        registry = load_registry()
    return set(registry.get("sources", {}).keys())


def get_retry_config(registry: dict, source_name: str) -> tuple[int, list[int], list[int]]:
    """从注册表获取指定搜索源的重试配置 → (max_retries, backoff_delays, retry_on_status_codes)"""
    src_cfg = registry.get("sources", {}).get(source_name, {})
    retry = src_cfg.get("retry", {})
    return (
        retry.get("max_retries", 3),
        retry.get("backoff", [2, 4, 8]),
        retry.get("retry_on", [429, 500, 502, 503]),
    )


def resolve_auto_enable_sources(registry: dict, domain: str) -> set[str]:
    """根据 domain 自动启用条件性搜索源（如 PubMed）"""
    auto = set()
    for name, cfg in registry.get("sources", {}).items():
        for rule in cfg.get("auto_enable_rules", []):
            if rule.get("field") == "domain" and domain in rule.get("contains_any", []):
                auto.add(name)
    return auto


def _fallback_registry() -> dict:
    """注册表不可用时的兜底默认值"""
    return {
        "sources": {
            "semantic_scholar": {"default_enabled": True},
            "crossref": {"default_enabled": True},
            "arxiv": {"default_enabled": True},
            "biorxiv": {"default_enabled": True},
            "pubmed": {"default_enabled": False},
        }
    }


# ──────────────────────── 工具函数 ────────────────────────────


def normalize_title(title: str) -> str:
    """标题归一化：lower + 去标点 + 去连字符 + 去多余空格"""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)  # 去标点
    t = t.replace("-", " ")  # 去连字符
    t = re.sub(r"\s+", " ", t).strip()  # 压缩空格
    return t


def safe_get(data: dict, *keys, default=None):
    """安全嵌套取值"""
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k, default)
        else:
            return default
    return data


def make_paper(
    doi: str | None,
    title: str,
    abstract: str | None,
    citation_count: int | None,
    year: int | None,
    venue: str | None,
    authors: list[str] | None,
    source: str,
    arxiv_id: str | None = None,
    paper_type: str | None = None,
    raw: dict | None = None,
) -> dict:
    """构造统一论文数据结构"""
    return {
        "doi": doi,
        "title": title.strip() if title else "",
        "abstract": (abstract.strip() if abstract else None),
        "citation_count": citation_count or 0,
        "year": year,
        "venue": venue,
        "authors": authors or [],
        "source": [source],
        "arxiv_id": arxiv_id,
        "paper_type": paper_type,
        "has_abstract": abstract is not None and len(abstract.strip()) > 20,
        "raw": raw or {},
    }


# ──────────────────── Semantic Scholar 搜索 ───────────────────


def search_semantic_scholar(
    keywords: str, year_from: int, year_to: int, max_results: int, client: httpx.Client
) -> tuple[list[dict], list[dict]]:
    """
    搜索 Semantic Scholar。
    返回 (论文列表, 错误列表)
    """
    papers = []
    errors = []
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    fields = "title,externalIds,abstract,citationCount,year,venue,authors,paperType"

    try:
        resp = _request_with_retry(
            client,
            "GET",
            url,
            params={
                "query": keywords,
                "year": f"{year_from}-{year_to}",
                "limit": min(max_results, 100),
                "fields": fields,
            },
            source="semantic_scholar",
        )
        if resp is None:
            errors.append({"source": "semantic_scholar", "error": "重试耗尽", "retried": True, "recovered": False})
            return papers, errors

        data = resp.json()
        for item in data.get("data", []):
            ext_ids = item.get("externalIds", {})
            doi = ext_ids.get("DOI")
            arxiv_id = ext_ids.get("ArXiv")
            authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
            papers.append(
                make_paper(
                    doi=doi,
                    title=item.get("title", ""),
                    abstract=item.get("abstract"),
                    citation_count=item.get("citationCount"),
                    year=item.get("year"),
                    venue=item.get("venue"),
                    authors=authors,
                    source="semantic_scholar",
                    arxiv_id=arxiv_id,
                    paper_type=item.get("paperType"),
                    raw=item,
                )
            )
    except Exception as e:
        errors.append({"source": "semantic_scholar", "error": str(e), "retried": False, "recovered": False})

    return papers, errors


# ──────────────────────── Crossref 搜索 ──────────────────────


def search_crossref(
    keywords: str, year_from: int, year_to: int, max_results: int, client: httpx.Client
) -> tuple[list[dict], list[dict]]:
    """
    搜索 Crossref。
    返回 (论文列表, 错误列表)
    """
    papers = []
    errors = []
    url = "https://api.crossref.org/works"

    try:
        resp = _request_with_retry(
            client,
            "GET",
            url,
            params={
                "query": keywords,
                "filter": f"from-pub-date:{year_from},until-pub-date:{year_to}",
                "rows": min(max_results, 100),
                "select": "DOI,title,author,abstract,published-print,container-title,is-referenced-by-count",
            },
            headers={"User-Agent": f"AutoPaperPipeline/0.4 (mailto:{CROSSREF_MAILTO})"},
            source="crossref",
        )
        if resp is None:
            errors.append({"source": "crossref", "error": "重试耗尽", "retried": True, "recovered": False})
            return papers, errors

        data = resp.json()
        items = data.get("message", {}).get("items", [])
        for item in items:
            doi = item.get("DOI")
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""
            abstract = item.get("abstract")
            # Crossref 摘要可能包含 HTML 标签，清理之
            if abstract:
                abstract = re.sub(r"<[^>]+>", "", abstract)

            # 提取作者
            authors = []
            for a in item.get("author", []):
                given = a.get("given", "")
                family = a.get("family", "")
                authors.append(f"{given} {family}".strip())

            # 提取年份
            date_parts = item.get("published-print", {}).get("date-parts", [[]])
            year = date_parts[0][0] if date_parts and date_parts[0] else None

            venue_list = item.get("container-title", [])
            venue = venue_list[0] if venue_list else None

            citation_count = item.get("is-referenced-by-count", 0)

            papers.append(
                make_paper(
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    citation_count=citation_count,
                    year=year,
                    venue=venue,
                    authors=authors,
                    source="crossref",
                    raw=item,
                )
            )
    except Exception as e:
        errors.append({"source": "crossref", "error": str(e), "retried": False, "recovered": False})

    return papers, errors


# ────────────────────────── arXiv 搜索 ───────────────────────


def search_arxiv(
    keywords: str, year_from: int, year_to: int, max_results: int, client: httpx.Client
) -> tuple[list[dict], list[dict]]:
    """
    搜索 arXiv（Atom XML 格式）。
    返回 (论文列表, 错误列表)
    """
    papers = []
    errors = []
    url = "https://export.arxiv.org/api/query"

    # arXiv 搜索表达式：多词用 AND 连接
    search_query = 'all:"' + keywords + '"'

    try:
        resp = _request_with_retry(
            client,
            "GET",
            url,
            params={
                "search_query": search_query,
                "start": 0,
                "max_results": min(max_results, 100),
                "sortBy": "relevance",
            },
            source="arxiv",
        )
        if resp is None:
            errors.append({"source": "arxiv", "error": "重试耗尽", "retried": True, "recovered": False})
            return papers, errors

        # 解析 Atom XML
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

        for entry in root.findall("atom:entry", ns):
            title = entry.find("atom:title", ns)
            title_text = title.text.strip() if title is not None and title.text else ""

            summary = entry.find("atom:summary", ns)
            abstract = summary.text.strip() if summary is not None and summary.text else None

            # arXiv ID 从 URL 中提取
            id_elem = entry.find("atom:id", ns)
            arxiv_url = id_elem.text if id_elem is not None else ""
            arxiv_id = arxiv_url.split("/abs/")[-1] if "/abs/" in arxiv_url else None

            # 发布日期提取年份
            published = entry.find("atom:published", ns)
            year = None
            if published is not None and published.text:
                try:
                    year = int(published.text[:4])
                except ValueError:
                    pass

            # 作者列表
            authors = []
            for author_elem in entry.findall("atom:author", ns):
                name = author_elem.find("atom:name", ns)
                if name is not None and name.text:
                    authors.append(name.text.strip())

            # arXiv 分类
            categories = []
            for cat in entry.findall("atom:category", ns):
                term = cat.get("term")
                if term:
                    categories.append(term)

            # DOI（可能在外部链接中）
            doi = None
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "doi":
                    doi_link = link.get("href", "")
                    if "doi.org/" in doi_link:
                        doi = doi_link.split("doi.org/")[-1]

            # 通过年份过滤（arXiv API 年份过滤不够精确）
            if year and (year < year_from or year > year_to):
                continue

            papers.append(
                make_paper(
                    doi=doi,
                    title=title_text,
                    abstract=abstract,
                    citation_count=0,  # arXiv 不提供引用数
                    year=year,
                    venue=None,  # arXiv 预印本无 venue
                    authors=authors,
                    source="arxiv",
                    arxiv_id=arxiv_id,
                    raw={"arxiv_id": arxiv_id, "categories": categories},
                )
            )
    except ET.ParseError as e:
        errors.append({"source": "arxiv", "error": f"XML 解析失败: {e}", "retried": False, "recovered": False})
    except Exception as e:
        errors.append({"source": "arxiv", "error": str(e), "retried": False, "recovered": False})

    return papers, errors


# ───────────────────────── bioRxiv 搜索 ──────────────────────


def search_biorxiv(
    keywords: str, year_from: int, year_to: int, max_results: int, client: httpx.Client
) -> tuple[list[dict], list[dict]]:
    """
    搜索 bioRxiv。
    bioRxiv API 按日期范围检索，再在标题/摘要中做关键词匹配。
    返回 (论文列表, 错误列表)
    """
    papers = []
    errors = []
    url = "https://api.biorxiv.org/details/biorxiv"

    # 将关键词拆分为独立词用于本地匹配
    kw_parts = [w.lower().strip() for w in re.split(r"[,\s]+", keywords) if w.strip()]

    try:
        cursor = 0
        batch_size = 100
        total_fetched = 0

        while total_fetched < max_results:
            resp = _request_with_retry(
                client,
                "GET",
                url,
                params={
                    "begin_date": f"{year_from}-01-01",
                    "end_date": f"{year_to}-12-31",
                    "cursor": cursor,
                },
                source="biorxiv",
            )
            if resp is None:
                errors.append({"source": "biorxiv", "error": "重试耗尽", "retried": True, "recovered": False})
                break

            data = resp.json()
            items = data.get("collection", [])
            if not items:
                break

            for item in items:
                title = item.get("title", "")
                abstract = item.get("abstract", "")

                # 在标题/摘要中做关键词匹配（任一词命中即可）
                text_to_match = f"{title} {abstract}".lower()
                if not any(kw in text_to_match for kw in kw_parts):
                    continue

                # 提取 DOI
                doi = item.get("doi")
                if doi and not doi.startswith("10."):
                    doi = None

                # 提取日期
                date_str = item.get("date", "")
                year = None
                if date_str:
                    try:
                        year = int(date_str[:4])
                    except ValueError:
                        pass

                # 提取作者（bioRxiv API 有时返回字符串）
                authors_raw = item.get("authors", "")
                if isinstance(authors_raw, str):
                    authors = [a.strip() for a in authors_raw.split(";") if a.strip()]
                elif isinstance(authors_raw, list):
                    authors = authors_raw
                else:
                    authors = []

                papers.append(
                    make_paper(
                        doi=doi,
                        title=title,
                        abstract=abstract,
                        citation_count=0,  # bioRxiv 不提供引用数
                        year=year,
                        venue="bioRxiv",
                        authors=authors,
                        source="biorxiv",
                        raw=item,
                    )
                )
                total_fetched += 1
                if total_fetched >= max_results:
                    break

            # 移动游标
            cursor += len(items)
            # 防止无限循环：如果本批次未匹配到任何论文，仍继续下一批
            messages = data.get("messages", [])
            if messages and messages[0].get("status") == "no results":
                break

    except Exception as e:
        errors.append({"source": "biorxiv", "error": str(e), "retried": False, "recovered": False})

    return papers, errors


# ───────────────────────── PubMed 搜索 ───────────────────────


def search_pubmed(
    keywords: str, year_from: int, year_to: int, max_results: int, client: httpx.Client
) -> tuple[list[dict], list[dict]]:
    """
    搜索 PubMed（esearch + efetch 两步）。
    返回 (论文列表, 错误列表)
    """
    papers = []
    errors = []
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    try:
        # 第 1 步：esearch 获取 PMIDs
        resp = _request_with_retry(
            client,
            "GET",
            esearch_url,
            params={
                "db": "pubmed",
                "term": f"{keywords} AND {year_from}:{year_to}[pdat]",
                "retmax": min(max_results, 100),
                "retmode": "json",
                "email": "pipeline@example.com",
                "tool": "auto-paper-pipeline",
            },
            source="pubmed",
            headers={
                "User-Agent": f"AutoPaperPipeline/0.4 (mailto:{CROSSREF_MAILTO})"
            },
        )
        if resp is None:
            errors.append({"source": "pubmed", "error": "esearch 重试耗尽", "retried": True, "recovered": False})
            return papers, errors

        search_data = resp.json()
        id_list = search_data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return papers, errors

        # 第 2 步：efetch 获取论文详情
        # 分批获取，每批最多 50 个
        for i in range(0, len(id_list), 50):
            batch_ids = id_list[i : i + 50]
            resp = _request_with_retry(
                client,
                "GET",
                efetch_url,
                params={
                    "db": "pubmed",
                    "id": ",".join(batch_ids),
                    "retmode": "xml",
                    "email": "pipeline@example.com",
                    "tool": "auto-paper-pipeline",
                },
                source="pubmed",
                headers={
                    "User-Agent": "AutoPaperPipeline/0.3 (mailto:pipeline@example.com)"
                },
            )
            if resp is None:
                errors.append(
                    {"source": "pubmed", "error": "efetch 重试耗尽", "retried": True, "recovered": False}
                )
                continue

            # 解析 XML
            try:
                root = ET.fromstring(resp.text)
                for article in root.findall(".//PubmedArticle"):
                    medline = article.find(".//MedlineCitation")
                    if medline is None:
                        continue

                    article_data = medline.find("Article")
                    if article_data is None:
                        continue

                    # 标题
                    title_elem = article_data.find(".//ArticleTitle")
                    title = title_elem.text if title_elem is not None and title_elem.text else ""

                    # 摘要
                    abstract_parts = []
                    for abs_text in article_data.findall(".//AbstractText"):
                        if abs_text.text:
                            label = abs_text.get("Label")
                            if label:
                                abstract_parts.append(f"{label}: {abs_text.text}")
                            else:
                                abstract_parts.append(abs_text.text)
                    abstract = " ".join(abstract_parts) if abstract_parts else None

                    # DOI
                    doi = None
                    for aid in article.findall(".//ArticleId"):
                        if aid.get("IdType") == "doi":
                            doi = aid.text
                            break

                    # 作者
                    authors = []
                    for author in article_data.findall(".//Author"):
                        last = author.find("LastName")
                        fore = author.find("ForeName")
                        name_parts = []
                        if fore is not None and fore.text:
                            name_parts.append(fore.text)
                        if last is not None and last.text:
                            name_parts.append(last.text)
                        if name_parts:
                            authors.append(" ".join(name_parts))

                    # 期刊
                    journal = article_data.find(".//Journal/Title")
                    venue = journal.text if journal is not None and journal.text else None

                    # 年份
                    pub_date = article_data.find(".//PubDate")
                    year = None
                    if pub_date is not None:
                        year_elem = pub_date.find("Year")
                        if year_elem is not None and year_elem.text:
                            try:
                                year = int(year_elem.text)
                            except ValueError:
                                pass

                    papers.append(
                        make_paper(
                            doi=doi,
                            title=title,
                            abstract=abstract,
                            citation_count=0,  # PubMed 不直接提供引用数
                            year=year,
                            venue=venue,
                            authors=authors,
                            source="pubmed",
                            raw={"pmid": medline.findtext("PMID")},
                        )
                    )
            except ET.ParseError as e:
                errors.append({"source": "pubmed", "error": f"XML 解析失败: {e}", "retried": False, "recovered": False})

    except Exception as e:
        errors.append({"source": "pubmed", "error": str(e), "retried": False, "recovered": False})

    return papers, errors


# ──────────────────── 带重试的 HTTP 请求 ──────────────────────


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    source: str,
    params: dict | None = None,
    headers: dict | None = None,
    _registry: dict | None = None,
) -> httpx.Response | None:
    """
    带指数退避重试的 HTTP 请求。
    重试参数从注册表读取（fallback 到内置默认值）。
    返回 Response 或 None（重试耗尽）。
    """
    reg = _registry or load_registry()
    max_retries, retry_delays, retry_on = get_retry_config(reg, source)

    last_exception = None
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                resp = client.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            else:
                resp = client.post(url, json=params, headers=headers, timeout=HTTP_TIMEOUT)

            if resp.status_code in retry_on or resp.status_code == 504:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1]
                logger.warning("[%s] HTTP %d，第 %d 次重试（等待 %ds）", source, resp.status_code, attempt + 1, delay)
                time.sleep(delay)
                continue

            # 其他错误状态码不重试
            if resp.status_code >= 400:
                logger.error("[%s] HTTP %d，不重试", source, resp.status_code)
                return None

            return resp

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exception = e
            delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1]
            logger.warning("[%s] 连接异常：%s，第 %d 次重试（等待 %ds）", source, e, attempt + 1, delay)
            time.sleep(delay)

    logger.error("[%s] 重试耗尽：%s", source, last_exception)
    return None


# ──────────────────────── 三级去重 ────────────────────────────


def deduplicate(papers: list[dict]) -> tuple[list[dict], dict]:
    """
    三级去重：
    1. DOI 精确去重
    2. arXiv ID ↔ DOI 映射去重
    3. 标题模糊去重

    返回 (去重后论文列表, 去重统计)
    """
    stats = {
        "doi_dedup": 0,
        "arxiv_id_dedup": 0,
        "title_dedup": 0,
    }

    # ── 第 1 级：DOI 精确去重 ──
    doi_map: dict[str, dict] = {}  # DOI → 论文
    no_doi_papers: list[dict] = []  # 无 DOI 的论文

    for p in papers:
        doi = p.get("doi")
        if doi:
            doi_lower = doi.lower()
            if doi_lower in doi_map:
                # 合并：保留最完整的摘要，合并 source
                existing = doi_map[doi_lower]
                _merge_paper(existing, p)
                stats["doi_dedup"] += 1
            else:
                doi_map[doi_lower] = p
        else:
            no_doi_papers.append(p)

    # ── 第 2 级：arXiv ID ↔ DOI 映射 ──
    arxiv_only: list[dict] = []  # 仍然没有匹配到 DOI 的 arXiv 论文

    for p in no_doi_papers:
        arxiv_id = p.get("arxiv_id")
        if arxiv_id:
            # 尝试在已有的 DOI 论文中查找是否有相同 arXiv ID
            matched = False
            for doi, existing in doi_map.items():
                if existing.get("arxiv_id") == arxiv_id:
                    _merge_paper(existing, p)
                    stats["arxiv_id_dedup"] += 1
                    matched = True
                    break
            if not matched:
                p["arxiv_only"] = True
                arxiv_only.append(p)
        else:
            arxiv_only.append(p)

    # ── 第 3 级：标题模糊去重 ──
    all_papers = list(doi_map.values()) + arxiv_only
    title_map: dict[str, dict] = {}  # 归一化标题 → 论文

    for p in all_papers:
        norm_title = normalize_title(p.get("title", ""))
        if not norm_title:
            continue

        if norm_title in title_map:
            existing = title_map[norm_title]
            # 保留有 DOI 的版本
            if p.get("doi") and not existing.get("doi"):
                title_map[norm_title] = p
                _merge_paper(p, existing)
            else:
                _merge_paper(existing, p)
            stats["title_dedup"] += 1
        else:
            title_map[norm_title] = p

    deduped = list(title_map.values())
    return deduped, stats


def _merge_paper(target: dict, source: dict) -> None:
    """
    将 source 论文信息合并到 target。
    - 合并 source 列表
    - 保留更完整的摘要
    - 保留更高的引用数
    """
    # 合并来源
    existing_sources = set(target.get("source", []))
    for s in source.get("source", []):
        if s not in existing_sources:
            target["source"].append(s)
            existing_sources.add(s)

    # 保留更完整的摘要
    if not target.get("abstract") and source.get("abstract"):
        target["abstract"] = source["abstract"]
        target["has_abstract"] = source["has_abstract"]

    # 保留更高的引用数
    if (source.get("citation_count") or 0) > (target.get("citation_count") or 0):
        target["citation_count"] = source["citation_count"]

    # 补充 DOI
    if not target.get("doi") and source.get("doi"):
        target["doi"] = source["doi"]

    # 补充 arXiv ID
    if not target.get("arxiv_id") and source.get("arxiv_id"):
        target["arxiv_id"] = source["arxiv_id"]


# ──────────────────────── 主流程 ──────────────────────────────


def load_pipeline_params(params_path: str | None) -> dict:
    """Load stage 1 pipeline params when available."""
    if not params_path:
        return {}

    path = Path(params_path)
    if not path.exists():
        raise FileNotFoundError(f"pipeline params not found: {params_path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_run_id(keywords: list[str]) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d") + "-" + re.sub(r"[^a-zA-Z0-9]", "-", keywords[0])[:30]


def run_search(
    keywords: list[str],
    sources: list[str],
    year_from: int,
    year_to: int,
    max_results: int,
    output_path: str,
    params_path: str | None = None,
    verbose: bool = False,
    registry: dict | None = None,
) -> int:
    """
    主搜索流程。
    返回退出码：0=成功，1=部分失败，2=全部失败
    """
    # 配置日志
    log_level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    # 加载注册表
    if registry is None:
        registry = load_registry()

    # 读取阶段 1 参数，优先透传 run_id，避免跨阶段产物分裂
    now = datetime.now(timezone.utc)
    try:
        params = load_pipeline_params(params_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误：pipeline_params.json 读取失败: {e}", file=sys.stderr)
        return 1
    run_id = params.get("run_id") or generate_run_id(keywords)

    all_papers: list[dict] = []
    all_errors: list[dict] = []
    source_counts: dict[str, int] = {}

    # 搜索源分发映射
    source_handlers = {
        "semantic_scholar": search_semantic_scholar,
        "crossref": search_crossref,
        "arxiv": search_arxiv,
        "biorxiv": search_biorxiv,
        "pubmed": search_pubmed,
    }

    with httpx.Client(follow_redirects=True) as client:
        kw_count = 0
        # 对每个关键词组合，在每个搜索源上查询
        for kw in keywords:
            for src in sources:
                handler = source_handlers.get(src)
                if handler is None:
                    logger.warning("未知搜索源: %s，跳过", src)
                    continue

                # 每切换关键词/源时增加间隔，避免触发 API 限流
                if kw_count > 0:
                    time.sleep(1.0)
                kw_count += 1

                logger.info("搜索 %s @ %s ...", kw, src)
                papers, errors = handler(kw, year_from, year_to, max_results, client)

                source_counts[src] = source_counts.get(src, 0) + len(papers)
                all_papers.extend(papers)
                all_errors.extend(errors)

    # ── 去重 ──
    deduped_papers, dedup_stats = deduplicate(all_papers)

    # ── 统计 ──
    total_before = len(all_papers)
    total_after = len(deduped_papers)
    duplicates_removed = total_before - total_after
    abstract_count = sum(1 for p in deduped_papers if p.get("has_abstract"))
    abstract_coverage = round(abstract_count / total_after, 2) if total_after > 0 else 0.0

    stats = {
        **{k: v for k, v in source_counts.items()},
        "total_before_dedup": total_before,
        "total_after_dedup": total_after,
        "duplicates_removed": duplicates_removed,
        "abstract_coverage": abstract_coverage,
    }

    # ── 构造输出 ──
    result = {
        "run_id": run_id,
        "created_at": now.isoformat(),
        "keywords": keywords,
        "stats": stats,
        "errors": all_errors,
        "papers": deduped_papers,
    }

    # 写入输出文件
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ── 报告 ──
    if verbose:
        print("搜索结果统计：")
        for src in sources:
            count = source_counts.get(src, 0)
            print(f"  {src}: {count} 篇")
        print(f"  去重前总计: {total_before} 篇")
        print(f"  DOI 去重: -{dedup_stats['doi_dedup']} 篇")
        print(f"  arXiv/bioRxiv ID 映射去重: -{dedup_stats['arxiv_id_dedup']} 篇")
        print(f"  标题模糊去重: -{dedup_stats['title_dedup']} 篇")
        print(f"  去重后总计: {total_after} 篇")
        print(f"  摘要覆盖率: {abstract_coverage:.0%}")
        if all_errors:
            print(f"  错误: {len(all_errors)} 个")

    # ── 退出码 ──
    failed_sources = {e["source"] for e in all_errors if not e.get("recovered", False)}
    if total_after == 0:
        return 2
    elif failed_sources:
        return 1
    else:
        return 0


# ──────────────────────── CLI 入口 ────────────────────────────


def main():
    # 预加载注册表
    registry = load_registry()
    default_sources = ",".join(get_default_sources(registry))
    valid_sources = get_valid_sources(registry)

    parser = argparse.ArgumentParser(
        description="多源论文搜索脚本（Semantic Scholar / Crossref / arXiv / bioRxiv / PubMed）"
    )
    parser.add_argument(
        "--keywords",
        required=True,
        help='逗号分隔的关键词列表（应为英文），如 "plant genome foundation model,plant LLM"',
    )
    parser.add_argument(
        "--sources",
        default=default_sources,
        help=f"启用的搜索源，逗号分隔；可选值: {', '.join(sorted(valid_sources))}（默认: {default_sources}）",
    )
    parser.add_argument(
        "--year-from",
        type=int,
        default=2018,
        help="起始年份（默认: 2018）",
    )
    parser.add_argument(
        "--year-to",
        type=int,
        default=CURRENT_YEAR,
        help=f"截止年份（默认: {CURRENT_YEAR}）",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="每个 API 返回上限（默认: 100）",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出 JSON 路径",
    )
    parser.add_argument(
        "--params",
        default=None,
        help="阶段 1 pipeline_params.json 路径；提供后优先透传其中的 run_id",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="打印各来源请求数和去重统计",
    )

    args = parser.parse_args()

    # 解析关键词
    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    if not keywords:
        print("错误：关键词不能为空", file=sys.stderr)
        sys.exit(2)

    # 解析搜索源（从注册表校验合法性）
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    invalid = set(sources) - valid_sources
    if invalid:
        print(f"错误：未知搜索源 {invalid}，可选值: {sorted(valid_sources)}", file=sys.stderr)
        sys.exit(2)

    exit_code = run_search(
        keywords=keywords,
        sources=sources,
        year_from=args.year_from,
        year_to=args.year_to,
        max_results=args.max_results,
        output_path=args.output,
        params_path=args.params,
        verbose=args.verbose,
        registry=registry,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

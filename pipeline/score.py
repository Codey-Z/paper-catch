#!/usr/bin/env python3
"""
score_papers.py — 论文打分排序脚本

阶段 3a: 预筛（排除关键词 / 最低关键词命中数 / 论文类型）
阶段 3b: 综合评分 + 排序 + 截断 Top N

退出码：0=成功，1=输入文件读取失败或为空，2=配置文件格式错误
"""

import argparse
import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ──────────────────────────── 常量 ────────────────────────────

CURRENT_YEAR = datetime.now().year

# 默认打分权重（与 scoring-rules.yaml 一致）
DEFAULT_WEIGHTS = {
    "keyword_match": 0.40,
    "citation_weighted": 0.30,
    "recency_decay": 0.20,
    "journal_quality": 0.10,
}

# 默认可下载性加分
DEFAULT_DOWNLOADABILITY_BONUS = {
    "high": 0.3,
    "medium": 0.15,
    "low": 0.0,
    "none": -0.2,
}

# 搜索源注册表路径
REGISTRY_PATH = Path(__file__).parent.parent / "config" / "search_sources.yaml"

# 默认参数
DEFAULT_CITATION_MAX = 5000
DEFAULT_LAMBDA = 0.3
DEFAULT_FLOOR = 0.1
DEFAULT_NO_ABSTRACT_PENALTY = 0.5

# 论文类型映射（启发式判断）
REVIEW_KEYWORDS = {"review", "survey", "systematic review", "meta-analysis", "advances in"}
METHOD_KEYWORDS = {"method", "framework", "approach", "algorithm", "novel", "proposed"}
EMPIRICAL_KEYWORDS = {"empirical", "experiment", "evaluation", "benchmark", "case study"}
DATASET_KEYWORDS = {"dataset", "benchmark", "corpus", "resource", "database"}

# 合法的论文类型
VALID_PAPER_TYPES = {"method", "empirical_study", "review_survey", "dataset_benchmark"}

logger = logging.getLogger(__name__)


# ──────────────────────── 工具函数 ────────────────────────────


def normalize_text(text: str) -> str:
    """文本归一化：lower + 去标点"""
    t = text.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def infer_paper_type(title: str, abstract: str | None, paper_type_field: str | None = None) -> str | None:
    """
    启发式推断论文类型。
    优先使用 Semantic Scholar 的 paperType 字段，
    其次从标题推断。
    返回: "method" / "empirical_study" / "review_survey" / "dataset_benchmark" / None
    """
    # 优先使用 API 提供的类型
    if paper_type_field:
        pt = paper_type_field.lower()
        if "review" in pt or "survey" in pt:
            return "review_survey"
        if "dataset" in pt or "benchmark" in pt:
            return "dataset_benchmark"
        if "empirical" in pt or "experimental" in pt:
            return "empirical_study"
        if "method" in pt:
            return "method"

    # 从标题启发式判断
    text = normalize_text(title)
    for kw in REVIEW_KEYWORDS:
        if kw in text:
            return "review_survey"
    for kw in DATASET_KEYWORDS:
        if kw in text:
            return "dataset_benchmark"
    for kw in EMPIRICAL_KEYWORDS:
        if kw in text:
            return "empirical_study"
    for kw in METHOD_KEYWORDS:
        if kw in text:
            return "method"

    return None


# ──────────────────── 阶段 3a: 预筛 ──────────────────────────


def prefilter(
    papers: list[dict],
    keywords: list[str],
    exclude_keywords: list[str] | None = None,
    min_keyword_hits: int = 0,
    paper_types: list[str] | None = None,
    keyword_weights: dict[str, float] | None = None,
) -> tuple[list[dict], dict]:
    """
    预筛：排除明显不相关的论文。

    返回 (通过预筛的论文列表, 预筛统计)
    """
    exclude_keywords = exclude_keywords or []
    paper_types = paper_types or []
    keyword_weights = keyword_weights or {}

    stats = {
        "total_input": len(papers),
        "excluded_by_keyword": 0,
        "excluded_by_min_hits": 0,
        "excluded_by_type": 0,
        "total_passed": 0,
    }

    # 预处理关键词（归一化）
    norm_keywords = [normalize_text(kw) for kw in keywords]
    norm_excludes = [normalize_text(ek) for ek in exclude_keywords]

    passed = []
    for p in papers:
        title = p.get("title", "")
        abstract = p.get("abstract") or ""
        text = normalize_text(f"{title} {abstract}")

        # 规则 1: 排除关键词过滤
        excluded = False
        for ek in norm_excludes:
            if ek in text:
                stats["excluded_by_keyword"] += 1
                excluded = True
                break
        if excluded:
            continue

        # 规则 2: 最低关键词命中数
        hit_count = 0
        for i, kw in enumerate(norm_keywords):
            if kw in text:
                hit_count += 1

        if min_keyword_hits > 0 and hit_count < min_keyword_hits:
            stats["excluded_by_min_hits"] += 1
            continue

        # 规则 3: 论文类型过滤
        if paper_types:
            inferred_type = infer_paper_type(
                title, abstract, p.get("paper_type")
            )
            if inferred_type and inferred_type not in paper_types:
                stats["excluded_by_type"] += 1
                continue
            # 若无法推断类型，放行（不因类型未知而排除）

        passed.append(p)

    stats["total_passed"] = len(passed)
    return passed, stats


# ──────────────── 阶段 3b: 综合评分 ──────────────────────────


def score_papers(
    papers: list[dict],
    keywords: list[str],
    config: dict,
    journal_tiers: dict,
    keyword_weights: dict[str, float] | None = None,
    downloadable_providers: list[dict] | None = None,
) -> list[dict]:
    """
    对论文列表进行综合评分。

    返回带分数的论文列表（已排序）。
    """
    keyword_weights = keyword_weights or {}
    downloadable_providers = downloadable_providers or []

    # 读取配置
    weights = config.get("weights", DEFAULT_WEIGHTS)
    w_kw = weights.get("keyword_match", DEFAULT_WEIGHTS["keyword_match"])
    w_cit = weights.get("citation_weighted", DEFAULT_WEIGHTS["citation_weighted"])
    w_rec = weights.get("recency_decay", DEFAULT_WEIGHTS["recency_decay"])
    w_jrn = weights.get("journal_quality", DEFAULT_WEIGHTS["journal_quality"])

    # 可下载性加分配置
    dl_bonus_config = config.get("downloadability_bonus", DEFAULT_DOWNLOADABILITY_BONUS)

    # 关键词匹配参数
    kw_config = config.get("keyword", {})
    no_abstract_penalty = kw_config.get("no_abstract_penalty", DEFAULT_NO_ABSTRACT_PENALTY)

    # 引用数参数
    cit_config = config.get("citation", {})
    citation_max = cit_config.get("citation_max", DEFAULT_CITATION_MAX)

    # 时效衰减参数
    rec_config = config.get("recency", {})
    decay_lambda = rec_config.get("lambda", DEFAULT_LAMBDA)
    decay_floor = rec_config.get("floor", DEFAULT_FLOOR)

    # 归一化关键词
    norm_keywords = [normalize_text(kw) for kw in keywords]

    scored = []
    for p in papers:
        breakdown = {}

        # ── 1. 关键词匹配度 ──
        title = p.get("title", "")
        abstract = p.get("abstract") or ""
        has_abstract = p.get("has_abstract", False)
        text = normalize_text(f"{title} {abstract}")

        hit_count = 0.0
        for i, kw in enumerate(norm_keywords):
            # 短语优先：多词短语整体匹配
            if kw in text:
                weight = keyword_weights.get(keywords[i], 1.0)
                hit_count += weight
            else:
                # 回退：短语未命中时拆分逐词匹配，
                # 每个命中词计 weight / 词数（部分匹配降权）
                kw_words = kw.split()
                if len(kw_words) > 1:
                    word_hits = sum(1 for w in kw_words if len(w) > 1 and w in text)
                    if word_hits > 0:
                        weight = keyword_weights.get(keywords[i], 1.0)
                        hit_count += weight * (word_hits / len(kw_words))

        total_kw_weight = sum(keyword_weights.get(keywords[i], 1.0) for i in range(len(keywords)))
        keyword_match = hit_count / total_kw_weight if total_kw_weight > 0 else 0.0

        # 无摘要降权
        if not has_abstract:
            keyword_match *= no_abstract_penalty

        breakdown["keyword_match"] = round(keyword_match, 4)

        # ── 2. 引用数加权 ──
        citation_count = p.get("citation_count", 0) or 0
        if citation_count > 0 and citation_max > 0:
            citation_weighted = math.log(citation_count + 1) / math.log(citation_max + 1)
        else:
            citation_weighted = 0.0
        breakdown["citation_weighted"] = round(citation_weighted, 4)

        # ── 3. 时效衰减 ──
        year = p.get("year")
        if year and isinstance(year, int):
            age = CURRENT_YEAR - year
            recency_decay = max(decay_floor, math.exp(-decay_lambda * age))
        else:
            recency_decay = decay_floor  # 无年份信息时使用下限值
        breakdown["recency_decay"] = round(recency_decay, 4)

        # ── 4. 期刊质量 ──
        venue = p.get("venue") or ""
        journal_quality = _score_journal(venue, journal_tiers)
        breakdown["journal_quality"] = round(journal_quality, 4)

        # ── 5. 可下载性加分 ──
        doi = p.get("doi")
        arxiv_id = p.get("arxiv_id")
        dl_provider, dl_reliability = get_downloadability(doi, arxiv_id, downloadable_providers)
        dl_bonus = dl_bonus_config.get(dl_reliability, 0.0)
        breakdown["downloadability"] = round(dl_bonus, 4)
        breakdown["download_provider"] = dl_provider

        # ── 综合评分 ──
        total_score = (
            w_kw * keyword_match
            + w_cit * citation_weighted
            + w_rec * recency_decay
            + w_jrn * journal_quality
            + dl_bonus
        )

        scored_paper = {
            **p,
            "score": round(total_score, 4),
            "score_breakdown": breakdown,
        }
        scored.append(scored_paper)

    # 按分数降序排序
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def _score_journal(venue: str, journal_tiers: dict) -> float:
    """
    按期刊分层映射表计算期刊质量分。
    匹配流程：exact 精确匹配 → keywords 子串匹配 → default
    """
    tiers = journal_tiers.get("tiers", {})
    default_score = journal_tiers.get("default", 0.2)

    if not venue:
        return default_score

    venue_lower = normalize_text(venue)

    for tier_name, tier_data in tiers.items():
        # 精确匹配
        exact_list = tier_data.get("exact", [])
        for e in exact_list:
            if venue == e:
                return tier_data.get("score", default_score)

        # 子串匹配
        keywords = tier_data.get("keywords", [])
        for kw in keywords:
            if normalize_text(kw) in venue_lower:
                return tier_data.get("score", default_score)

    return default_score


# ──────────────────── 配置文件读取 ────────────────────────────


def load_scoring_config(config_path: str) -> dict:
    """读取打分配置 YAML"""
    path = Path(config_path)
    if not path.exists():
        logger.warning("配置文件 %s 不存在，使用默认参数", config_path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式错误: {e}", file=sys.stderr)
        sys.exit(2)


def load_journal_tiers(references_dir: str) -> dict:
    """读取期刊分层映射表"""
    path = Path(references_dir) / "journal_tiers.yaml"
    if not path.exists():
        logger.warning("期刊分层映射表 %s 不存在，使用默认分层", path)
        return {
            "tiers": {},
            "default": 0.2,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"错误：期刊分层映射表格式错误: {e}", file=sys.stderr)
        sys.exit(2)


def load_params(params_path: str | None) -> dict:
    """读取 pipeline_params.json（可选）"""
    if not params_path:
        return {}

    path = Path(params_path)
    if not path.exists():
        logger.warning("参数文件 %s 不存在", params_path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("参数文件读取失败: %s", e)
        return {}


def load_downloadable_providers(registry_path: str | Path | None = None) -> list[dict]:
    """从注册表加载可下载能力矩阵"""
    path = Path(registry_path) if registry_path else REGISTRY_PATH
    if not path.exists():
        logger.warning("注册表 %s 不存在，跳过可下载性评分", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            registry = yaml.safe_load(f) or {}
        return registry.get("downloadable_providers", [])
    except (yaml.YAMLError, OSError) as e:
        logger.warning("注册表读取失败: %s", e)
        return []


def get_downloadability(doi: str | None, arxiv_id: str | None, downloadable_providers: list[dict]) -> tuple[str | None, str]:
    """
    根据DOI前缀或arXiv ID判断论文可下载性。
    返回 (provider_name, reliability) 或 (None, "none")
    """
    # arXiv ID 优先匹配
    if arxiv_id:
        for dp in downloadable_providers:
            if dp.get("provider") == "arxiv":
                return "arxiv", dp.get("reliability", "high")

    # DOI 前缀匹配
    if doi:
        for dp in downloadable_providers:
            for prefix in dp.get("doi_prefixes", []):
                if doi.startswith(prefix):
                    return dp["provider"], dp.get("reliability", "medium")

    return None, "none"


# ──────────────────────── 主流程 ──────────────────────────────


def run_scoring(
    input_path: str,
    config_path: str,
    top_n: int,
    output_path: str,
    exclude_keywords: list[str] | None = None,
    min_keyword_hits: int = 0,
    paper_types: list[str] | None = None,
    params_path: str | None = None,
    verbose: bool = False,
) -> int:
    """
    主打分流程。
    返回退出码：0=成功，1=输入读取失败，2=配置错误
    """
    # 配置日志
    log_level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    # ── 读取输入 ──
    input_file = Path(input_path)
    if not input_file.exists():
        print(f"错误：输入文件 {input_path} 不存在", file=sys.stderr)
        return 1

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误：输入文件读取失败: {e}", file=sys.stderr)
        return 1

    papers = data.get("papers", [])
    if not papers:
        print("错误：输入文件中无论文数据", file=sys.stderr)
        return 1

    run_id = data.get("run_id", "unknown")

    # ── 读取配置 ──
    config = load_scoring_config(config_path)
    references_dir = str(Path(config_path).parent)
    journal_tiers = load_journal_tiers(references_dir)

    # ── 加载可下载能力矩阵 ──
    downloadable_providers = load_downloadable_providers()

    # ── 读取 pipeline_params.json ──
    params = load_params(params_path)

    # 从 params 合并参数（CLI 参数优先）
    if params:
        if not exclude_keywords:
            ek = params.get("exclude_keywords", [])
            if ek:
                exclude_keywords = ek
        if not paper_types:
            pt = params.get("paper_types", [])
            if pt:
                paper_types = pt
        if min_keyword_hits == 0:
            min_keyword_hits = params.get("min_keyword_hits", 0)

    # ── 提取关键词 ──
    keywords = data.get("keywords", [])
    # 兼容：从 search_results.json 中提取时可能没有 keywords 字段，从 run_id 或 params 推断
    if not keywords and params:
        keywords = params.get("search_queries", params.get("keywords", []))
    if not keywords:
        # 从论文标题中提取高频词作为兜底
        logger.warning("未找到关键词，将从论文标题提取")
        keywords = _extract_keywords_from_papers(papers)

    keyword_weights = params.get("keyword_weights", {})

    # ── 阶段 3a: 预筛 ──
    filtered, prefilter_stats = prefilter(
        papers=papers,
        keywords=keywords,
        exclude_keywords=exclude_keywords,
        min_keyword_hits=min_keyword_hits,
        paper_types=paper_types,
        keyword_weights=keyword_weights,
    )

    if verbose:
        print(f"预筛统计：")
        print(f"  输入论文数: {prefilter_stats['total_input']}")
        print(f"  排除关键词过滤: {prefilter_stats['excluded_by_keyword']} 篇")
        print(f"  排除最低关键词命中数: {prefilter_stats['excluded_by_min_hits']} 篇")
        print(f"  排除论文类型: {prefilter_stats['excluded_by_type']} 篇")
        print(f"  通过预筛: {prefilter_stats['total_passed']} 篇")

    if not filtered:
        print("警告：预筛后无论文进入打分", file=sys.stderr)

    # ── 阶段 3b: 综合评分 ──
    scored = score_papers(
        papers=filtered,
        keywords=keywords,
        config=config,
        journal_tiers=journal_tiers,
        keyword_weights=keyword_weights,
        downloadable_providers=downloadable_providers,
    )

    # ── 截断 Top N ──
    if top_n > 0:
        scored = scored[:top_n]

    # ── 构造输出 ──
    now = datetime.now(timezone.utc)
    result = {
        "run_id": run_id,
        "created_at": now.isoformat(),
        "config": {
            "keywords": keywords,
            "top_n": top_n,
            "scoring_config": config_path,
            "prefilter": {
                "exclude_keywords": exclude_keywords or [],
                "min_keyword_hits": min_keyword_hits,
                "paper_types": paper_types or [],
            },
        },
        "papers": scored,
    }

    # 写入输出文件
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ── verbose 输出 ──
    if verbose and scored:
        print(f"\nTop {len(scored)} 论文排序：")
        for i, p in enumerate(scored, 1):
            print(f"  {i}. [{p['score']:.4f}] {p.get('title', 'N/A')} ({p.get('year', '?')})")
            bd = p.get("score_breakdown", {})
            dl_prov = bd.get("download_provider", "—")
            print(f"     关键词={bd.get('keyword_match', 0):.3f}  引用={bd.get('citation_weighted', 0):.3f}  时效={bd.get('recency_decay', 0):.3f}  期刊={bd.get('journal_quality', 0):.3f}  可下载性={bd.get('downloadability', 0):+.2f} [{dl_prov}]")

    return 0


def _extract_keywords_from_papers(papers: list[dict], top_k: int = 5) -> list[str]:
    """从论文标题中提取高频词作为兜底关键词"""
    from collections import Counter

    word_counts = Counter()
    stop_words = {
        "a", "an", "the", "of", "in", "for", "and", "to", "with", "on",
        "by", "from", "at", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "can", "this",
        "that", "these", "those", "it", "its", "we", "our", "their",
        "not", "no", "but", "or", "as", "if", "than", "then", "so",
    }

    for p in papers:
        title = normalize_text(p.get("title", ""))
        words = [w for w in title.split() if len(w) > 2 and w not in stop_words]
        word_counts.update(words)

    return [w for w, _ in word_counts.most_common(top_k)]


# ──────────────────────── CLI 入口 ────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="论文打分排序脚本（预筛 + 综合评分 + Top N 截断）"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="search_papers.py 输出的 JSON 路径",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="打分参数 YAML 路径（默认: config/scoring.yaml）",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="截断数量，0 表示不截断（默认: 10）",
    )
    parser.add_argument(
        "--exclude-keywords",
        default=None,
        help="排除关键词，逗号分隔；标题或摘要命中任一词则预筛排除",
    )
    parser.add_argument(
        "--min-keyword-hits",
        type=int,
        default=0,
        help="最低关键词命中数，低于此值的论文在预筛阶段排除（默认: 0）",
    )
    parser.add_argument(
        "--paper-types",
        default=None,
        help="论文类型白名单，逗号分隔；可选: method, empirical_study, review_survey, dataset_benchmark",
    )
    parser.add_argument(
        "--params",
        default=None,
        help="pipeline_params.json 路径；若提供，自动从中读取 exclude_keywords、keyword_weights、paper_types",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出 JSON 路径",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="打印每篇论文的分数明细 + 预筛统计",
    )

    args = parser.parse_args()

    # 解析排除关键词
    exclude_keywords = None
    if args.exclude_keywords:
        exclude_keywords = [ek.strip() for ek in args.exclude_keywords.split(",") if ek.strip()]

    # 解析论文类型
    paper_types = None
    if args.paper_types:
        paper_types = [pt.strip() for pt in args.paper_types.split(",") if pt.strip()]
        invalid = set(paper_types) - VALID_PAPER_TYPES
        if invalid:
            print(f"错误：未知论文类型 {invalid}，可选值: {sorted(VALID_PAPER_TYPES)}", file=sys.stderr)
            sys.exit(2)

    # 配置文件默认路径
    config_path = args.config
    if config_path is None:
        # 尝试从脚本所在目录的 config/ 下查找
        script_dir = Path(__file__).parent
        default_config = script_dir.parent / "config" / "scoring.yaml"
        if default_config.exists():
            config_path = str(default_config)
        else:
            config_path = "config/scoring.yaml"

    exit_code = run_scoring(
        input_path=args.input,
        config_path=config_path,
        top_n=args.top_n,
        output_path=args.output,
        exclude_keywords=exclude_keywords,
        min_keyword_hits=args.min_keyword_hits,
        paper_types=paper_types,
        params_path=args.params,
        verbose=args.verbose,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
download.py — 论文批量下载脚本

封装 paper-fetch CLI，提供自动重试、详细错误日志和下载状态追踪。
作为流水线阶段 4 的执行引擎。

退出码：0=全部成功，1=部分失败，2=全部失败
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ──────────────────────────── 常量 ────────────────────────────

# 重试配置
MAX_RETRIES = 3                       # 最大重试次数
RETRY_DELAYS = [2, 4, 8]              # 指数退避延迟（秒）

# 可重试的错误类型（按子串匹配 paper-fetch 的输出/退出码）
RETRYABLE_PATTERNS = [
    "ConnectTimeout",                   # 网络连接超时
    "ReadTimeout",                      # 读取超时
    "timed out",                        # 通用超时
    "ConnectionError",                  # 连接错误
    "RemoteDisconnected",               # 远程断开
    "503",                              # 服务不可用
    "429",                              # 限流
    "Too Many Requests",                # 限流
    "Service Unavailable",              # 服务不可用
]

# 不可重试的错误（永久失败，重试无意义）
PERMANENT_FAILURE_PATTERNS = [
    "abstract_only",                    # 无全文 provider
    "metadata_only",                    # 连元数据都没有
    "no_fulltext",                      # paper-fetch 明确无全文
    "not found",                        # DOI 不存在
    "404",                              # 资源不存在
    "403",                              # 无权限
    "401",                              # 需认证
]

logger = logging.getLogger(__name__)


# ──────────────────────── 错误分类 ────────────────────────────

def classify_error(error_output: str, exit_code: int) -> tuple[str, bool]:
    """
    分类下载错误。

    返回 (error_category, is_retryable)
    - is_retryable: True = 可重试，False = 永久失败

    分类逻辑：
    1. exit_code == 0 → 成功
    2. 输出中包含永久失败关键词 → 不可重试
    3. 输出中包含可重试关键词 → 可重试
    4. exit_code != 0 且无法归类 → 默认可重试（保守策略）
    """
    if exit_code == 0:
        return "success", False

    output_lower = error_output.lower()

    # 先检查永久失败（优先级更高）
    for pattern in PERMANENT_FAILURE_PATTERNS:
        if pattern.lower() in output_lower:
            return pattern, False

    # 再检查可重试失败
    for pattern in RETRYABLE_PATTERNS:
        if pattern.lower() in output_lower:
            return pattern, True

    # 无法归类：默认视为可重试（保守策略，最多重试 MAX_RETRIES 次）
    return "unknown_error", True


def check_download_result(output_dir: str, doi: str) -> tuple[bool, str]:
    """
    验证下载结果文件的质量。

    返回 (success, detail)
    - success: True = 成功获取全文
    - detail: "fulltext" / "abstract_only" / "metadata_only" / "empty" / "no_file"

    检查逻辑：
    1. 文件是否存在
    2. frontmatter 中的 content_kind 和 has_fulltext 字段
    3. 文件大小（< 1KB 视为空）
    """
    # 查找匹配的 Markdown 文件（DOI 中的特殊字符被替换为 _）
    safe_doi = doi.replace("/", "_").replace(".", "_").replace(":", "_")
    p = Path(output_dir)

    candidates = list(p.glob(f"*{safe_doi}*.md"))
    if not candidates:
        # 也尝试按 DOI 前缀匹配
        candidates = list(p.glob("*.md"))

    target = None
    for c in candidates:
        content = c.read_text(encoding="utf-8", errors="replace")
        if doi in content[:500]:  # 在前 500 字符内检查 DOI
            target = c
            break

    if target is None:
        return False, "no_file"

    content = target.read_text(encoding="utf-8", errors="replace")

    # 检查 frontmatter
    has_fulltext = re.search(r'has_fulltext:\s*true', content, re.IGNORECASE)
    content_kind = re.search(r'content_kind:\s*"(\w+)"', content)
    kind = content_kind.group(1) if content_kind else "unknown"
    source = re.search(r'source:\s*"(\w+)"', content)
    src = source.group(1) if source else "unknown"

    # 文件大小检查
    size = target.stat().st_size
    if size < 1024:  # < 1KB
        return False, f"empty_file ({size}B)"

    if has_fulltext and kind == "fulltext":
        return True, f"fulltext ({src}, {size//1024}KB)"
    elif kind == "abstract_only":
        return False, f"abstract_only ({src})"
    elif kind == "metadata_only":
        return False, f"metadata_only ({src})"
    else:
        return False, f"partial ({src}, kind={kind}, {size//1024}KB)"


# ──────────────────────── 下载执行 ────────────────────────────

def download_one(
    query: str,
    output_dir: str,
    timeout: int = 120,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """
    下载单篇论文，带自动重试。

    query: DOI 或 arXiv ID
    output_dir: 保存目录
    timeout: paper-fetch CLI 超时（秒）
    extra_env: 额外的环境变量（如 CROSSREF_MAILTO）

    返回:
    {
        "query": "...",
        "success": True/False,
        "detail": "fulltext (springer_html, 60KB)",
        "attempts": 2,
        "errors": ["attempt 1: ConnectTimeout", "attempt 2: success"],
        "output_file": "xxx.md" | None,
        "elapsed_seconds": 12.3,
    }
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    # 确保 CROSSREF_MAILTO 存在
    if "CROSSREF_MAILTO" not in env:
        env["CROSSREF_MAILTO"] = "pipeline@example.com"

    errors_log = []
    start_time = time.time()

    for attempt in range(MAX_RETRIES + 1):  # 1 次初始 + MAX_RETRIES 次重试
        if attempt > 0:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            logger.info("  [%s] 第 %d 次重试，等待 %ds ...", query[:40], attempt, delay)
            time.sleep(delay)

        try:
            result = subprocess.run(
                [
                    "paper-fetch",
                    "--query", query,
                    "--output-dir", output_dir,
                    "--save-markdown",
                    "--artifact-mode", "none",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            errors_log.append({
                "attempt": attempt + 1,
                "error": f"CLI 超时（>{timeout}s）",
                "type": "CLI Timeout",
                "retryable": True,
            })
            if attempt < MAX_RETRIES:
                continue  # 重试
            else:
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "CLI timeout after max retries",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                }

        # 分类错误
        stderr_output = result.stderr + result.stdout
        error_category, is_retryable = classify_error(stderr_output, result.returncode)

        if result.returncode == 0:
            # CLI 退出 0，但还需验证文件内容
            success, detail = check_download_result(output_dir, query)
            elapsed = time.time() - start_time

            if success:
                # 找到输出文件
                p = Path(output_dir)
                safe_doi = query.replace("/", "_").replace(".", "_")
                candidates = list(p.glob(f"*{safe_doi}*.md"))
                output_file = str(candidates[0].relative_to(p)) if candidates else None

                return {
                    "query": query,
                    "success": True,
                    "detail": detail,
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": output_file,
                    "elapsed_seconds": round(elapsed, 1),
                }
            else:
                # CLI 成功但文件无全文（meta/abstract only）
                errors_log.append({
                    "attempt": attempt + 1,
                    "error": detail,
                    "type": error_category,
                    "retryable": False,
                })
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": detail,
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                }

        # CLI 返回非 0 退出码
        errors_log.append({
            "attempt": attempt + 1,
            "error": error_category,
            "type": "CLI Failure",
            "retryable": is_retryable,
            "exit_code": result.returncode,
            "stderr_snippet": stderr_output[:200],
        })

        if not is_retryable:
            # 永久失败，不再重试
            elapsed = time.time() - start_time
            return {
                "query": query,
                "success": False,
                "detail": f"permanent failure: {error_category}",
                "attempts": attempt + 1,
                "errors": errors_log,
                "output_file": None,
                "elapsed_seconds": round(elapsed, 1),
            }

        # 可重试失败，继续循环
        if attempt < MAX_RETRIES:
            logger.warning(
                "  [%s] 失败: %s（可重试），准备重试 ...",
                query[:40], error_category,
            )

    # 重试耗尽
    elapsed = time.time() - start_time
    return {
        "query": query,
        "success": False,
        "detail": "retry exhausted",
        "attempts": MAX_RETRIES + 1,
        "errors": errors_log,
        "output_file": None,
        "elapsed_seconds": round(elapsed, 1),
    }


# ──────────────────────── 批量下载 ────────────────────────────

def batch_download(
    queries: list[str],
    output_dir: str,
    timeout: int = 120,
    extra_env: dict[str, str] | None = None,
    verbose: bool = False,
) -> dict:
    """
    批量下载论文。

    queries: DOI/arXiv ID 列表
    output_dir: 保存目录
    timeout: 每篇超时（秒）
    extra_env: 额外环境变量

    返回批量下载结果字典。
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    results = []
    succeeded = []
    failed_with_detail = []

    total = len(queries)
    for i, query in enumerate(queries):
        logger.info("[%d/%d] 下载 %s ...", i + 1, total, query[:50])
        result = download_one(query, output_dir, timeout=timeout, extra_env=extra_env)
        results.append(result)

        if result["success"]:
            succeeded.append(result)
            logger.info("  ✅ 成功: %s（%d 次尝试, %.1fs）",
                        result["detail"], result["attempts"], result["elapsed_seconds"])
        else:
            failed_with_detail.append(result)
            # 提取最关键的错误信息
            last_error = result["errors"][-1] if result["errors"] else {"error": "unknown"}
            logger.warning("  ❌ 失败: %s（%d 次尝试）→ %s",
                          result["detail"], result["attempts"], last_error.get("error", "?"))

    # 汇总
    summary = {
        "total": total,
        "succeeded": len(succeeded),
        "failed": len(failed_with_detail),
        "details": results,
        "succeeded_list": [r["query"] for r in succeeded],
        "failed_list": [
            {
                "query": r["query"],
                "detail": r["detail"],
                "attempts": r["attempts"],
                "last_error": (r["errors"][-1] if r["errors"] else {}),
            }
            for r in failed_with_detail
        ],
    }

    # 失败原因分类统计
    fail_categories = {}
    for r in failed_with_detail:
        cat = "permanent" if any(
            p.lower() in r["detail"].lower() for p in PERMANENT_FAILURE_PATTERNS
        ) else "transient"
        fail_categories[cat] = fail_categories.get(cat, 0) + 1

    summary["fail_categories"] = fail_categories

    if verbose:
        print(f"\n===== 下载汇总 =====")
        print(f"总计: {total} 篇")
        print(f"成功: {len(succeeded)} 篇")
        print(f"失败: {len(failed_with_detail)} 篇")
        if fail_categories:
            print(f"  - 永久失败 (无 provider/无权限): {fail_categories.get('permanent', 0)}")
            print(f"  - 临时失败 (网络/超时): {fail_categories.get('transient', 0)}")

    return summary


# ──────────────────────── 错误诊断 ────────────────────────────

def diagnose_failure(result: dict) -> str:
    """
    根据下载结果生成人类可读的诊断信息。

    用于在 SKILL.md 阶段 4 结束后向用户报告每篇失败的原因。

    返回单行诊断字符串。
    """
    detail = result.get("detail", "unknown")

    # 按失败类型生成建议
    diagnostics = {
        "abstract_only": "无对应 provider（如 Frontiers），只能获取摘要。手动下载或换源。",
        "metadata_only": "DOI 可能失效或论文已撤稿。到 Crossref/metadata 页面手动验证。",
        "no_file": "CLI 未生成输出文件。检查 paper-fetch 是否已正确安装。",
        "empty_file": "输出文件为空或极短。检查网络连接和 paper-fetch 版本。",
        "retry exhausted": f"网络问题导致 {result.get('attempts', '?')} 次重试全部失败。检查网络和 GitHub Reachability。",
        "ConnectTimeout": "网络连接超时。目标服务器可能不可达（如 GitHub 被墙）或需配置代理。",
        "ReadTimeout": "下载超时。论文太大或服务器响应慢，增加 --timeout 或稍后重试。",
    }

    for key, msg in diagnostics.items():
        if key.lower() in detail.lower():
            return msg

    # 默认诊断
    last_error = (result.get("errors", [{}])[-1] or {}).get("error", "?")
    return f"未分类失败: {detail}（最后一次错误: {last_error}）"


# ──────────────────────── CLI 入口 ────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="论文批量下载脚本（封装 paper-fetch CLI，带自动重试与错误诊断）",
        epilog="示例: python pipeline/download.py --input top_n_dois.json --output-dir outputs/.../papers/fulltext/ --verbose"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="top_n_dois.json 路径（阶段 3 输出）",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="论文保存目录（如 outputs/<run_id>/papers/fulltext/）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="每篇论文 CLI 下载超时（秒），默认 120",
    )
    parser.add_argument(
        "--crossref-mailto",
        default=None,
        help="CROSSREF_MAILTO 邮箱（可从环境变量读取）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="详细输出每篇论文的下载状态",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        default=False,
        help="仅分析已有下载结果，不实际下载",
    )

    args = parser.parse_args()

    # 读取输入
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"错误：读取输入文件失败: {e}", file=sys.stderr)
        sys.exit(1)

    papers = data.get("papers", [])
    if not papers:
        print("错误：输入文件中无论文数据", file=sys.stderr)
        sys.exit(1)

    # 提取 DOI / arXiv ID 列表
    queries = []
    for p in papers:
        q = p.get("doi") or p.get("arxiv_id")
        if q:
            queries.append(q)
        else:
            # 兜底：用标题作为查询（paper-fetch 也支持标题查询）
            title = p.get("title", "")
            if title:
                queries.append(title)

    if not queries:
        print("错误：无可用查询（DOI 或 arXiv ID）", file=sys.stderr)
        sys.exit(1)

    # 准备环境变量
    extra_env = {}
    if args.crossref_mailto:
        extra_env["CROSSREF_MAILTO"] = args.crossref_mailto
    else:
        mailto = os.environ.get("CROSSREF_MAILTO", "pipeline@example.com")
        extra_env["CROSSREF_MAILTO"] = mailto

    # 如果仅诊断模式, 分析已有文件
    if args.diagnose_only:
        print("=== 下载结果诊断 ===")
        for q in queries:
            success, detail = check_download_result(args.output_dir, q)
            status = "✅" if success else "❌"
            print(f"  {status} {q[:50]}: {detail}")
        sys.exit(0)

    # 批量下载
    logger.info("开始批量下载，共 %d 篇论文", len(queries))
    logger.info("输出目录: %s", args.output_dir)
    logger.info("CROSSREF_MAILTO: %s", extra_env["CROSSREF_MAILTO"])

    summary = batch_download(
        queries=queries,
        output_dir=args.output_dir,
        timeout=args.timeout,
        extra_env=extra_env,
        verbose=args.verbose,
    )

    # 打印诊断
    if args.verbose and summary["failed"] > 0:
        print(f"\n===== 失败诊断 =====")
        for f in summary["failed_list"]:
            q = f["query"]
            detail = f["detail"]
            print(f"  ❌ {q[:50]}")
            print(f"     原因: {detail}")
            # 临时失败的诊断建议
            diag_result = {"detail": detail, "attempts": f["attempts"], "errors": [f.get("last_error", {})]}
            print(f"     建议: {diagnose_failure(diag_result)}")

    # 退出码
    if summary["succeeded"] == summary["total"]:
        sys.exit(0)
    elif summary["succeeded"] > 0:
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()

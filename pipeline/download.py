#!/usr/bin/env python3
"""
download.py — 论文批量下载脚本

封装 paper-fetch MCP/CLI 后端，提供自动重试、详细错误日志和下载状态追踪。
作为流水线阶段 5 的执行引擎。

退出码：0=全部成功，1=部分失败，2=全部失败
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
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

MCP_CLIENT_HELPER = r'''
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload_from_result(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


async def main():
    request = json.loads(sys.stdin.read())
    server = request["server"]
    params = StdioServerParameters(
        command=server["command"],
        args=server.get("args", []),
        env=server.get("env"),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if request["mode"] == "list_tools":
                result = await session.list_tools()
                print(json.dumps({"ok": True, "tools": [tool.name for tool in result.tools]}))
                return
            result = await session.call_tool(request["tool"], request.get("arguments", {}))
            print(json.dumps({
                "ok": True,
                "is_error": bool(getattr(result, "isError", False)),
                "payload": payload_from_result(result),
            }))


try:
    asyncio.run(main())
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
    raise
'''


class BackendUnavailableError(Exception):
    """Raised when a configured paper-fetch backend cannot be used."""


class PermanentFetchError(Exception):
    """Raised when one paper cannot be resolved or fetched and retrying will not help."""


class PaperFetchBackend:
    name = "unknown"

    def probe(self) -> tuple[bool, str]:
        return True, "available"

    def download_one(
        self,
        query: str,
        output_dir: str,
        timeout: int = 120,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        raise NotImplementedError


class UnavailablePaperFetchBackend(PaperFetchBackend):
    def __init__(self, name: str, errors: list[str]) -> None:
        self.name = name
        self.errors = errors

    def probe(self) -> tuple[bool, str]:
        return False, "; ".join(self.errors) if self.errors else "backend unavailable"

    def download_one(
        self,
        query: str,
        output_dir: str,
        timeout: int = 120,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        detail = "fetch backend unavailable"
        return {
            "query": query,
            "success": False,
            "detail": detail,
            "attempts": 1,
            "errors": [
                {
                    "attempt": 1,
                    "error": "; ".join(self.errors) if self.errors else detail,
                    "type": "Backend Unavailable",
                    "retryable": False,
                }
            ],
            "output_file": None,
            "elapsed_seconds": 0.0,
            "backend": self.name,
        }


# ──────────────────────── 文件定位 ────────────────────────────

def safe_query_stem(query: str) -> str:
    """Return the filename stem convention used by paper-fetch for DOI-like queries."""
    return query.replace("/", "_").replace(".", "_").replace(":", "_")


def locate_downloaded_markdown(output_dir: str, query: str) -> Path | None:
    """Find the Markdown file generated for a DOI/arXiv/title query."""
    p = Path(output_dir)
    safe_query = safe_query_stem(query)

    candidates = list(p.glob(f"*{safe_query}*.md"))
    if not candidates:
        candidates = list(p.glob("*.md"))

    for candidate in candidates:
        content = candidate.read_text(encoding="utf-8", errors="replace")
        if query in content[:500]:
            return candidate

    return None


def evaluate_markdown_file(target: Path) -> tuple[bool, str]:
    """Validate one downloaded Markdown file and describe its evidence level."""
    content = target.read_text(encoding="utf-8", errors="replace")

    has_fulltext = re.search(r'has_fulltext:\s*true', content, re.IGNORECASE)
    content_kind = re.search(r'content_kind:\s*"(\w+)"', content)
    kind = content_kind.group(1) if content_kind else "unknown"
    source = re.search(r'source:\s*"(\w+)"', content)
    src = source.group(1) if source else "unknown"

    size = target.stat().st_size
    if size < 1024:
        return False, f"empty_file ({size}B)"

    if has_fulltext and kind == "fulltext":
        return True, f"fulltext ({src}, {size//1024}KB)"
    if kind == "abstract_only":
        return False, f"abstract_only ({src})"
    if kind == "metadata_only":
        return False, f"metadata_only ({src})"
    return False, f"partial ({src}, kind={kind}, {size//1024}KB)"


def relative_output_file(target: Path, output_dir: str) -> str:
    """Return a stable output path string for state summaries."""
    try:
        return str(target.relative_to(Path(output_dir)))
    except ValueError:
        return str(target)


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
    target = locate_downloaded_markdown(output_dir, doi)

    if target is None:
        return False, "no_file"

    return evaluate_markdown_file(target)


# ──────────────────────── 后端配置与选择 ───────────────────────

def normalize_fetch_backend(value: str | None) -> str:
    backend = (value or os.environ.get("PAPER_CATCH_FETCH_BACKEND") or "auto").strip().lower()
    if backend not in {"auto", "mcp", "cli"}:
        raise ValueError(f"Unsupported fetch backend: {backend}")
    return backend


def mcp_config_candidates(explicit_config: str | None = None) -> list[Path]:
    if explicit_config:
        return [Path(explicit_config).expanduser()]

    env_config = os.environ.get("PAPER_CATCH_MCP_CONFIG")
    if env_config:
        return [Path(env_config).expanduser()]

    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / ".workbuddy" / "mcp.json",
        Path.cwd() / ".workbuddy" / "mcp.json",
        Path.home() / ".workbuddy" / "mcp.json",
        Path.home() / ".config" / "paper-catch" / "mcp.json",
    ]
    deduped: list[Path] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def load_mcp_server_config(
    *,
    config_path: str | None = None,
    server_name: str = "paper-fetch",
) -> tuple[dict[str, Any], Path]:
    checked: list[str] = []
    for candidate in mcp_config_candidates(config_path):
        checked.append(str(candidate))
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise BackendUnavailableError(f"cannot read MCP config {candidate}: {e}") from e

        server = data.get("mcpServers", {}).get(server_name)
        if not server:
            continue
        if server.get("disabled"):
            raise BackendUnavailableError(f"MCP server `{server_name}` is disabled in {candidate}")
        if server.get("type") and server.get("type") not in {"stdio", "command"}:
            raise BackendUnavailableError(
                f"MCP server `{server_name}` in {candidate} is not stdio; HTTP transport is not supported in v1"
            )
        if not server.get("command"):
            raise BackendUnavailableError(f"MCP server `{server_name}` in {candidate} has no command")
        return server, candidate

    raise BackendUnavailableError(
        f"MCP server `{server_name}` not found; checked: {', '.join(checked)}"
    )


def is_identifier_query(query: str) -> bool:
    q = query.strip()
    if q.startswith(("http://", "https://")):
        return True
    if re.search(r"\b10\.\d{4,9}/\S+", q, re.IGNORECASE):
        return True
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", q, re.IGNORECASE):
        return True
    if re.fullmatch(r"[a-z-]+(\.[A-Z]{2})?/\d{7}(v\d+)?", q, re.IGNORECASE):
        return True
    return False


def first_json_payload_from_mcp_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content_items = getattr(result, "content", []) or []
    for item in content_items:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def extract_saved_markdown_path(payload: dict[str, Any], output_dir: str) -> Path | None:
    candidates = [
        payload.get("saved_markdown_path"),
        payload.get("markdown_path"),
        payload.get("path"),
    ]
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidates.extend([
        metadata.get("saved_markdown_path"),
        metadata.get("markdown_path"),
    ])

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        if not path.is_absolute():
            path = Path(output_dir) / path
        if path.exists():
            return path
    return None


def extract_resolved_query(payload: dict[str, Any]) -> str | None:
    for key in ("doi", "url", "resolved_query", "canonical_query", "query"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for container_key in ("paper", "metadata", "result"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in ("doi", "url", "resolved_query", "canonical_query"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    candidates = payload.get("candidates") or payload.get("matches")
    if isinstance(candidates, list) and len(candidates) == 1 and isinstance(candidates[0], dict):
        candidate = candidates[0]
        for key in ("doi", "url", "query"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def mcp_payload_error(payload: dict[str, Any]) -> str | None:
    for key in ("error", "detail", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"ambiguous", "not_found", "no_match"}:
        return status
    return None


# ──────────────────────── 下载执行 ────────────────────────────

class CliPaperFetchBackend(PaperFetchBackend):
    name = "cli"

    def probe(self) -> tuple[bool, str]:
        executable = shutil.which("paper-fetch")
        if not executable:
            return False, "paper-fetch CLI not found on PATH"
        return True, executable

    def download_one(
        self,
        query: str,
        output_dir: str,
        timeout: int = 120,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        if "CROSSREF_MAILTO" not in env:
            env["CROSSREF_MAILTO"] = "pipeline@example.com"

        errors_log = []
        start_time = time.time()

        for attempt in range(MAX_RETRIES + 1):
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
                    continue
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "CLI timeout after max retries",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }
            except FileNotFoundError:
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "paper-fetch CLI not found",
                    "attempts": attempt + 1,
                    "errors": [
                        *errors_log,
                        {
                            "attempt": attempt + 1,
                            "error": "paper-fetch executable not found on PATH",
                            "type": "CLI Missing",
                            "retryable": False,
                        },
                    ],
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            stderr_output = result.stderr + result.stdout
            error_category, is_retryable = classify_error(stderr_output, result.returncode)

            if result.returncode == 0:
                success, detail = check_download_result(output_dir, query)
                elapsed = time.time() - start_time

                if success:
                    target = locate_downloaded_markdown(output_dir, query)
                    return {
                        "query": query,
                        "success": True,
                        "detail": detail,
                        "attempts": attempt + 1,
                        "errors": errors_log,
                        "output_file": relative_output_file(target, output_dir) if target else None,
                        "elapsed_seconds": round(elapsed, 1),
                        "backend": self.name,
                    }

                errors_log.append({
                    "attempt": attempt + 1,
                    "error": detail,
                    "type": error_category,
                    "retryable": False,
                })
                return {
                    "query": query,
                    "success": False,
                    "detail": detail,
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            errors_log.append({
                "attempt": attempt + 1,
                "error": error_category,
                "type": "CLI Failure",
                "retryable": is_retryable,
                "exit_code": result.returncode,
                "stderr_snippet": stderr_output[:200],
            })

            if not is_retryable:
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": f"permanent failure: {error_category}",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            if attempt < MAX_RETRIES:
                logger.warning(
                    "  [%s] 失败: %s（可重试），准备重试 ...",
                    query[:40], error_category,
                )

        elapsed = time.time() - start_time
        return {
            "query": query,
            "success": False,
            "detail": "retry exhausted",
            "attempts": MAX_RETRIES + 1,
            "errors": errors_log,
            "output_file": None,
            "elapsed_seconds": round(elapsed, 1),
            "backend": self.name,
        }


class McpPaperFetchBackend(PaperFetchBackend):
    name = "mcp"

    def __init__(
        self,
        *,
        config_path: str | None = None,
        server_name: str = "paper-fetch",
    ) -> None:
        self.config_path = config_path
        self.server_name = server_name

    def _server_config(self) -> tuple[dict[str, Any], Path]:
        return load_mcp_server_config(config_path=self.config_path, server_name=self.server_name)

    def probe(self) -> tuple[bool, str]:
        try:
            tools = self._list_tools_sync(timeout=30)
        except Exception as e:
            return False, f"paper-fetch MCP unavailable: {e}"
        if "fetch_paper" not in tools:
            return False, "paper-fetch MCP server does not expose fetch_paper"
        return True, f"paper-fetch MCP server `{self.server_name}` available"

    @staticmethod
    def _run_async(coro: Any, timeout: int) -> Any:
        import asyncio

        async def with_timeout() -> Any:
            return await asyncio.wait_for(coro, timeout=timeout)

        return asyncio.run(with_timeout())

    def _server_parameters(self, extra_env: dict[str, str] | None = None) -> Any:
        try:
            from mcp import StdioServerParameters
        except ImportError as e:
            raise BackendUnavailableError("Python package `mcp` is not installed") from e

        server, _ = self._server_config()
        env = os.environ.copy()
        configured_env = server.get("env")
        if isinstance(configured_env, dict):
            env.update({str(k): str(v) for k, v in configured_env.items()})
        if extra_env:
            env.update(extra_env)
        if "CROSSREF_MAILTO" not in env:
            env["CROSSREF_MAILTO"] = "pipeline@example.com"

        return StdioServerParameters(
            command=str(server["command"]),
            args=[str(arg) for arg in server.get("args", [])],
            env=env,
        )

    def _server_process_payload(self, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        server, _ = self._server_config()
        env = os.environ.copy()
        configured_env = server.get("env")
        if isinstance(configured_env, dict):
            env.update({str(k): str(v) for k, v in configured_env.items()})
        if extra_env:
            env.update(extra_env)
        if "CROSSREF_MAILTO" not in env:
            env["CROSSREF_MAILTO"] = "pipeline@example.com"
        return {
            "command": str(server["command"]),
            "args": [str(arg) for arg in server.get("args", [])],
            "env": env,
        }

    @staticmethod
    def _current_python_has_mcp() -> bool:
        try:
            import mcp  # noqa: F401
        except ImportError:
            return False
        return True

    def _helper_request(
        self,
        request: dict[str, Any],
        *,
        timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        server_payload = self._server_process_payload(extra_env=extra_env)
        helper_python = server_payload["command"]
        if "python" not in Path(helper_python).name.lower():
            raise BackendUnavailableError(
                "Python package `mcp` is not installed and MCP server command is not a Python interpreter"
            )
        helper_input = {
            **request,
            "server": server_payload,
        }
        try:
            result = subprocess.run(
                [helper_python, "-c", MCP_CLIENT_HELPER],
                input=json.dumps(helper_input),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=server_payload["env"],
            )
        except FileNotFoundError as e:
            raise BackendUnavailableError(f"MCP helper Python not found: {helper_python}") from e
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(str(e)) from e

        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "MCP helper failed")[:500])

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MCP helper returned non-JSON output: {result.stdout[:500]}") from e
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", "MCP helper failed"))
        return payload

    def _list_tools_sync(self, *, timeout: int) -> set[str]:
        if self._current_python_has_mcp():
            return self._run_async(self._list_tools(), timeout=timeout)
        payload = self._helper_request({"mode": "list_tools"}, timeout=timeout)
        return set(payload.get("tools", []))

    def _call_tool_sync(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: int,
        extra_env: dict[str, str] | None,
    ) -> tuple[dict[str, Any], bool]:
        if self._current_python_has_mcp():
            result = self._run_async(
                self._session_call(tool_name, arguments, extra_env),
                timeout=timeout,
            )
            return first_json_payload_from_mcp_result(result), bool(getattr(result, "isError", False))

        payload = self._helper_request(
            {
                "mode": "call_tool",
                "tool": tool_name,
                "arguments": arguments,
            },
            timeout=timeout,
            extra_env=extra_env,
        )
        return payload.get("payload", {}), bool(payload.get("is_error", False))

    async def _session_call(self, tool_name: str, arguments: dict[str, Any], extra_env: dict[str, str] | None) -> Any:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise BackendUnavailableError("Python package `mcp` is not installed") from e

        params = self._server_parameters(extra_env=extra_env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    async def _list_tools(self) -> set[str]:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise BackendUnavailableError("Python package `mcp` is not installed") from e

        params = self._server_parameters()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return {tool.name for tool in result.tools}

    def _resolve_query(
        self,
        query: str,
        *,
        timeout: int,
        extra_env: dict[str, str] | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if is_identifier_query(query):
            return query, None

        payload, _ = self._call_tool_sync(
            "resolve_paper",
            {"query": query},
            timeout=timeout,
            extra_env=extra_env,
        )
        resolved = extract_resolved_query(payload)
        return resolved, payload

    def _fetch_once(
        self,
        query: str,
        *,
        output_dir: str,
        timeout: int,
        extra_env: dict[str, str] | None,
    ) -> dict[str, Any]:
        resolved_query, resolve_payload = self._resolve_query(
            query,
            timeout=timeout,
            extra_env=extra_env,
        )
        if not resolved_query:
            reason = mcp_payload_error(resolve_payload or {}) or "ambiguous_or_unresolved_title"
            raise PermanentFetchError(f"resolve_paper failed: {reason}")

        payload, is_error = self._call_tool_sync(
            "fetch_paper",
            {
                "query": resolved_query,
                "modes": ["article", "markdown"],
                "strategy": {
                    "allow_metadata_only_fallback": True,
                    "asset_profile": "none",
                },
                "include_refs": None,
                "max_tokens": "full_text",
                "prefer_cache": False,
                "no_download": False,
                "artifact_mode": "none",
                "save_markdown": True,
                "markdown_output_dir": output_dir,
                "download_dir": output_dir,
            },
            timeout=timeout,
            extra_env=extra_env,
        )
        if is_error:
            raise RuntimeError(mcp_payload_error(payload) or "MCP fetch_paper failed")

        payload["_resolved_query"] = resolved_query
        return payload

    def download_one(
        self,
        query: str,
        output_dir: str,
        timeout: int = 120,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        errors_log = []
        start_time = time.time()

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                logger.info("  [%s] 第 %d 次 MCP 重试，等待 %ds ...", query[:40], attempt, delay)
                time.sleep(delay)

            try:
                payload = self._fetch_once(
                    query,
                    output_dir=output_dir,
                    timeout=timeout,
                    extra_env=extra_env,
                )
            except BackendUnavailableError as e:
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "paper-fetch MCP unavailable",
                    "attempts": attempt + 1,
                    "errors": [
                        *errors_log,
                        {
                            "attempt": attempt + 1,
                            "error": str(e),
                            "type": "MCP Missing",
                            "retryable": False,
                        },
                    ],
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }
            except PermanentFetchError as e:
                elapsed = time.time() - start_time
                errors_log.append({
                    "attempt": attempt + 1,
                    "error": str(e),
                    "type": "MCP Permanent Failure",
                    "retryable": False,
                })
                return {
                    "query": query,
                    "success": False,
                    "detail": "permanent failure: ambiguous_or_unresolved_title",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }
            except TimeoutError:
                errors_log.append({
                    "attempt": attempt + 1,
                    "error": f"MCP 超时（>{timeout}s）",
                    "type": "MCP Timeout",
                    "retryable": True,
                })
                if attempt < MAX_RETRIES:
                    continue
                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "MCP timeout after max retries",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }
            except Exception as e:
                error_text = str(e)
                error_category, is_retryable = classify_error(error_text, 1)
                errors_log.append({
                    "attempt": attempt + 1,
                    "error": error_category,
                    "type": "MCP Failure",
                    "retryable": is_retryable,
                    "stderr_snippet": error_text[:200],
                })
                if not is_retryable:
                    elapsed = time.time() - start_time
                    return {
                        "query": query,
                        "success": False,
                        "detail": f"permanent failure: {error_category}",
                        "attempts": attempt + 1,
                        "errors": errors_log,
                        "output_file": None,
                        "elapsed_seconds": round(elapsed, 1),
                        "backend": self.name,
                    }
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "  [%s] MCP 失败: %s（可重试），准备重试 ...",
                        query[:40], error_category,
                    )
                    continue

                elapsed = time.time() - start_time
                return {
                    "query": query,
                    "success": False,
                    "detail": "retry exhausted",
                    "attempts": MAX_RETRIES + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            target = extract_saved_markdown_path(payload, output_dir)
            effective_query = payload.get("_resolved_query") or query
            if target is None:
                target = locate_downloaded_markdown(output_dir, str(effective_query))
            if target is None:
                target = locate_downloaded_markdown(output_dir, query)

            elapsed = time.time() - start_time
            if target is None:
                errors_log.append({
                    "attempt": attempt + 1,
                    "error": "no_file",
                    "type": "success",
                    "retryable": False,
                })
                return {
                    "query": query,
                    "success": False,
                    "detail": "no_file",
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": None,
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            success, detail = evaluate_markdown_file(target)
            if success:
                return {
                    "query": query,
                    "success": True,
                    "detail": detail,
                    "attempts": attempt + 1,
                    "errors": errors_log,
                    "output_file": relative_output_file(target, output_dir),
                    "elapsed_seconds": round(elapsed, 1),
                    "backend": self.name,
                }

            errors_log.append({
                "attempt": attempt + 1,
                "error": detail,
                "type": "success",
                "retryable": False,
            })
            return {
                "query": query,
                "success": False,
                "detail": detail,
                "attempts": attempt + 1,
                "errors": errors_log,
                "output_file": None,
                "elapsed_seconds": round(elapsed, 1),
                "backend": self.name,
            }

        elapsed = time.time() - start_time
        return {
            "query": query,
            "success": False,
            "detail": "retry exhausted",
            "attempts": MAX_RETRIES + 1,
            "errors": errors_log,
            "output_file": None,
            "elapsed_seconds": round(elapsed, 1),
            "backend": self.name,
        }


def select_fetch_backend(
    *,
    requested: str | None = None,
    mcp_config: str | None = None,
    mcp_server_name: str | None = None,
) -> tuple[PaperFetchBackend, list[str], str]:
    backend_name = normalize_fetch_backend(requested)
    server_name = mcp_server_name or os.environ.get("PAPER_CATCH_MCP_SERVER") or "paper-fetch"
    probe_errors: list[str] = []

    if backend_name == "cli":
        return CliPaperFetchBackend(), probe_errors, backend_name

    if backend_name in {"auto", "mcp"}:
        mcp_backend = McpPaperFetchBackend(config_path=mcp_config, server_name=server_name)
        ok, detail = mcp_backend.probe()
        if ok:
            return mcp_backend, probe_errors, backend_name
        probe_errors.append(detail)
        if backend_name == "mcp":
            return UnavailablePaperFetchBackend("mcp", probe_errors), probe_errors, backend_name

    cli_backend = CliPaperFetchBackend()
    ok, detail = cli_backend.probe()
    if ok:
        return cli_backend, probe_errors, backend_name
    probe_errors.append(detail)
    return UnavailablePaperFetchBackend("auto", probe_errors), probe_errors, backend_name


def download_one(
    query: str,
    output_dir: str,
    timeout: int = 120,
    extra_env: dict[str, str] | None = None,
    backend: PaperFetchBackend | None = None,
) -> dict:
    """
    下载单篇论文，带自动重试。

    默认使用 CLI 后端以保留函数级兼容性；阶段 5 批量入口默认使用 auto 后端。
    """
    selected_backend = backend or CliPaperFetchBackend()
    result = selected_backend.download_one(
        query,
        output_dir,
        timeout=timeout,
        extra_env=extra_env,
    )
    result.setdefault("backend", selected_backend.name)
    return result


# ──────────────────────── 批量下载 ────────────────────────────

def batch_download(
    queries: list[str],
    output_dir: str,
    timeout: int = 120,
    extra_env: dict[str, str] | None = None,
    verbose: bool = False,
    fetch_backend: str | None = None,
    mcp_config: str | None = None,
    mcp_server_name: str | None = None,
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

    backend, probe_errors, requested_backend = select_fetch_backend(
        requested=fetch_backend,
        mcp_config=mcp_config,
        mcp_server_name=mcp_server_name,
    )
    logger.info("paper-fetch 后端: requested=%s, selected=%s", requested_backend, backend.name)
    for error in probe_errors:
        logger.debug("后端探测: %s", error)

    results = []
    succeeded = []
    failed_with_detail = []

    total = len(queries)
    for i, query in enumerate(queries):
        logger.info("[%d/%d] 下载 %s ...", i + 1, total, query[:50])
        result = download_one(
            query,
            output_dir,
            timeout=timeout,
            extra_env=extra_env,
            backend=backend,
        )
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
        "backend_requested": requested_backend,
        "backend_used": backend.name,
        "backend_probe_errors": probe_errors,
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
        if any((err or {}).get("type") == "Backend Unavailable" for err in r.get("errors", [])):
            cat = "backend_unavailable"
        elif any((err or {}).get("type") == "CLI Missing" for err in r.get("errors", [])):
            cat = "cli_missing"
        elif any((err or {}).get("type") == "MCP Missing" for err in r.get("errors", [])):
            cat = "mcp_missing"
        elif r["detail"].lower().startswith("permanent failure") or any(
            p.lower() in r["detail"].lower() for p in PERMANENT_FAILURE_PATTERNS
        ):
            cat = "permanent"
        else:
            cat = "transient"
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


# ──────────────────────── 状态持久化 ──────────────────────────

def summarize_download_status(summary: dict) -> str:
    """Map a download summary to the pipeline state status."""
    if summary.get("total", 0) == 0:
        return "failed"
    if summary.get("succeeded") == summary.get("total"):
        return "completed"
    if summary.get("fail_categories", {}).get("backend_unavailable"):
        return "blocked"
    if summary.get("failed") == summary.get("total"):
        details = summary.get("details", [])
        if details and all(
            any(
                (err or {}).get("type") in {"CLI Missing", "MCP Missing", "Backend Unavailable"}
                for err in item.get("errors", [])
            )
            for item in details
        ):
            return "blocked"
        return "failed"
    return "partial_failed"


def build_pipeline_state(
    *,
    run_id: str,
    input_path: str,
    output_dir: str,
    summary: dict,
) -> dict:
    status = summarize_download_status(summary)
    state = {
        "run_id": run_id,
        "stage": 5,
        "stage_name": "download",
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "input": input_path,
        "output_dir": output_dir,
        "summary": summary,
    }
    if status == "blocked":
        probe_errors = summary.get("backend_probe_errors") or []
        backend_used = summary.get("backend_used", "unknown")
        details = summary.get("details", [])
        all_cli_missing = bool(details) and all(
            any((err or {}).get("type") == "CLI Missing" for err in item.get("errors", []))
            for item in details
        )
        if probe_errors:
            state["blocking_reason"] = "No usable paper-fetch backend: " + "; ".join(probe_errors)
        elif backend_used == "cli" or all_cli_missing:
            state["blocking_reason"] = "paper-fetch CLI not found on PATH"
        elif backend_used == "mcp":
            state["blocking_reason"] = "paper-fetch MCP unavailable"
        else:
            state["blocking_reason"] = "paper-fetch backend unavailable"
        state["next_action"] = (
            "Configure paper-fetch MCP in WorkBuddy-compatible mcp.json or install paper-fetch CLI, "
            "then rerun stage 5."
        )
    return state


def write_pipeline_state(state_file: str | Path, state: dict) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ──────────────────────── 错误诊断 ────────────────────────────

def diagnose_failure(result: dict) -> str:
    """
    根据下载结果生成人类可读的诊断信息。

    用于在 SKILL.md 阶段 5 结束后向用户报告每篇失败的原因。

    返回单行诊断字符串。
    """
    detail = result.get("detail", "unknown")

    # 按失败类型生成建议
    diagnostics = {
        "abstract_only": "无对应 provider（如 Frontiers），只能获取摘要。手动下载或换源。",
        "metadata_only": "DOI 可能失效或论文已撤稿。到 Crossref/metadata 页面手动验证。",
        "no_file": "CLI 未生成输出文件。检查 paper-fetch 是否已正确安装。",
        "empty_file": "输出文件为空或极短。检查网络连接和 paper-fetch 版本。",
        "fetch backend unavailable": "没有可用的 paper-fetch 后端。配置 MCP 或安装 CLI 后重试。",
        "paper-fetch MCP unavailable": "paper-fetch MCP 不可用。检查 mcp.json、server 启动命令和 Python mcp 依赖。",
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
        description="论文批量下载脚本（封装 paper-fetch MCP/CLI，带自动重试与错误诊断）",
        epilog="示例: python pipeline/download.py --input top_n_dois.json --output-dir outputs/.../papers/fulltext/ --verbose"
    )
    parser.add_argument(
        "--input",
        default=None,
        help="top_n_dois.json 路径（阶段 3 输出）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
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
    parser.add_argument(
        "--state-file",
        default=None,
        help="pipeline_state.json 输出路径；默认写入输入文件同目录",
    )
    parser.add_argument(
        "--fetch-backend",
        choices=["auto", "mcp", "cli"],
        default=None,
        help="paper-fetch 后端：auto 优先 MCP 后回退 CLI；默认读取 PAPER_CATCH_FETCH_BACKEND 或 auto",
    )
    parser.add_argument(
        "--mcp-config",
        default=None,
        help="WorkBuddy 兼容 mcp.json；默认读取 PAPER_CATCH_MCP_CONFIG 或自动查找",
    )
    parser.add_argument(
        "--mcp-server-name",
        default=None,
        help="mcpServers 中的 paper-fetch server 名称；默认 PAPER_CATCH_MCP_SERVER 或 paper-fetch",
    )
    parser.add_argument(
        "--check-backend",
        action="store_true",
        default=False,
        help="只探测将使用的 paper-fetch 后端，不读取输入、不执行下载",
    )

    args = parser.parse_args()

    if args.check_backend:
        backend, probe_errors, requested_backend = select_fetch_backend(
            requested=args.fetch_backend,
            mcp_config=args.mcp_config,
            mcp_server_name=args.mcp_server_name,
        )
        ok, detail = backend.probe()
        print(json.dumps(
            {
                "backend_requested": requested_backend,
                "backend_used": backend.name,
                "available": ok,
                "detail": detail,
                "backend_probe_errors": probe_errors,
            },
            ensure_ascii=False,
            indent=2,
        ))
        sys.exit(0 if ok else 2)

    if not args.input:
        parser.error("--input is required unless --check-backend is used")
    if not args.output_dir:
        parser.error("--output-dir is required unless --check-backend is used")

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
        fetch_backend=args.fetch_backend,
        mcp_config=args.mcp_config,
        mcp_server_name=args.mcp_server_name,
    )

    state_file = args.state_file or str(Path(args.input).resolve().parent / "pipeline_state.json")
    state = build_pipeline_state(
        run_id=data.get("run_id", "unknown"),
        input_path=args.input,
        output_dir=args.output_dir,
        summary=summary,
    )
    write_pipeline_state(state_file, state)
    logger.info("pipeline_state 写入: %s", state_file)

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

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.download import (
    McpPaperFetchBackend,
    batch_download,
    build_pipeline_state,
    check_download_result,
    download_one,
    safe_query_stem,
    write_pipeline_state,
)


class DownloadPipelineTest(unittest.TestCase):
    def test_missing_paper_fetch_returns_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.download.subprocess.run", side_effect=FileNotFoundError()):
                result = download_one("10.1186/1471-2105-11-421", tmp, timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(result["detail"], "paper-fetch CLI not found")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["backend"], "cli")
        self.assertEqual(result["errors"][-1]["type"], "CLI Missing")
        self.assertFalse(result["errors"][-1]["retryable"])

    def test_batch_download_classifies_missing_cli_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.download.subprocess.run", side_effect=FileNotFoundError()):
                summary = batch_download(["10.1186/1471-2105-11-421"], tmp, fetch_backend="cli")

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["backend_requested"], "cli")
        self.assertEqual(summary["backend_used"], "cli")
        self.assertEqual(summary["fail_categories"], {"cli_missing": 1})

    def test_download_filename_normalization_handles_colon(self) -> None:
        query = "10.1234/abc:def"
        self.assertEqual(safe_query_stem(query), "10_1234_abc_def")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            output_file = output_dir / "10_1234_abc_def.md"
            output_file.write_text(
                "---\n"
                "doi: 10.1234/abc:def\n"
                'content_kind: "fulltext"\n'
                "has_fulltext: true\n"
                'source: "test"\n'
                "---\n\n"
                + ("body " * 300),
                encoding="utf-8",
            )

            success, detail = check_download_result(tmp, query)
            self.assertTrue(success)
            self.assertIn("fulltext", detail)

            with patch(
                "pipeline.download.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ):
                result = download_one(query, tmp, timeout=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "cli")
        self.assertEqual(result["output_file"], "10_1234_abc_def.md")

    def test_auto_backend_prefers_available_mcp_without_calling_cli(self) -> None:
        class DummyMcp:
            name = "mcp"

            def __init__(self, **kwargs) -> None:
                pass

            def probe(self):
                return True, "mcp ok"

            def download_one(self, query, output_dir, timeout=120, extra_env=None):
                return {
                    "query": query,
                    "success": True,
                    "detail": "fulltext (mcp, 1KB)",
                    "attempts": 1,
                    "errors": [],
                    "output_file": "paper.md",
                    "elapsed_seconds": 0.1,
                    "backend": "mcp",
                }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.download.McpPaperFetchBackend", DummyMcp), patch(
                "pipeline.download.CliPaperFetchBackend"
            ) as cli_cls:
                summary = batch_download(["10.1/example"], tmp, fetch_backend="auto")

        self.assertEqual(summary["backend_requested"], "auto")
        self.assertEqual(summary["backend_used"], "mcp")
        self.assertEqual(summary["succeeded"], 1)
        cli_cls.assert_not_called()

    def test_auto_backend_falls_back_to_cli_when_mcp_probe_fails(self) -> None:
        class DummyMcp:
            name = "mcp"

            def __init__(self, **kwargs) -> None:
                pass

            def probe(self):
                return False, "mcp unavailable"

        class DummyCli:
            name = "cli"

            def probe(self):
                return True, "/usr/local/bin/paper-fetch"

            def download_one(self, query, output_dir, timeout=120, extra_env=None):
                return {
                    "query": query,
                    "success": True,
                    "detail": "fulltext (cli, 1KB)",
                    "attempts": 1,
                    "errors": [],
                    "output_file": "paper.md",
                    "elapsed_seconds": 0.1,
                    "backend": "cli",
                }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.download.McpPaperFetchBackend", DummyMcp), patch(
                "pipeline.download.CliPaperFetchBackend", DummyCli
            ):
                summary = batch_download(["10.1/example"], tmp, fetch_backend="auto")

        self.assertEqual(summary["backend_used"], "cli")
        self.assertEqual(summary["backend_probe_errors"], ["mcp unavailable"])
        self.assertEqual(summary["succeeded"], 1)

    def test_forced_mcp_unavailable_blocks_without_cli_fallback(self) -> None:
        class DummyMcp:
            name = "mcp"

            def __init__(self, **kwargs) -> None:
                pass

            def probe(self):
                return False, "mcp unavailable"

        with tempfile.TemporaryDirectory() as tmp:
            with patch("pipeline.download.McpPaperFetchBackend", DummyMcp), patch(
                "pipeline.download.CliPaperFetchBackend"
            ) as cli_cls:
                summary = batch_download(["10.1/example"], tmp, fetch_backend="mcp")

        self.assertEqual(summary["backend_used"], "mcp")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["fail_categories"], {"backend_unavailable": 1})
        cli_cls.assert_not_called()

        state = build_pipeline_state(
            run_id="run-123",
            input_path="outputs/run-123/top_n_dois.json",
            output_dir="outputs/run-123/papers/fulltext",
            summary=summary,
        )
        self.assertEqual(state["status"], "blocked")
        self.assertIn("No usable paper-fetch backend", state["blocking_reason"])

    def test_mcp_fetch_saved_markdown_path_uses_existing_result_validation(self) -> None:
        query = "10.1234/mcp"

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "mcp.md"
            output_file.write_text(
                "---\n"
                "doi: 10.1234/mcp\n"
                'content_kind: "fulltext"\n'
                "has_fulltext: true\n"
                'source: "mcp"\n'
                "---\n\n"
                + ("body " * 300),
                encoding="utf-8",
            )

            backend = McpPaperFetchBackend()
            with patch.object(
                backend,
                "_fetch_once",
                return_value={
                    "saved_markdown_path": str(output_file),
                    "_resolved_query": query,
                },
            ):
                result = download_one(query, tmp, timeout=1, backend=backend)

        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "mcp")
        self.assertEqual(result["output_file"], "mcp.md")

    def test_mcp_title_query_must_resolve_before_fetch(self) -> None:
        class AmbiguousMcp(McpPaperFetchBackend):
            def __init__(self) -> None:
                super().__init__()
                self.resolved = False

            def _resolve_query(self, query, *, timeout, extra_env):
                self.resolved = True
                return None, {"status": "ambiguous"}

        backend = AmbiguousMcp()
        with tempfile.TemporaryDirectory() as tmp:
            result = download_one("A paper title", tmp, timeout=1, backend=backend)

        self.assertTrue(backend.resolved)
        self.assertFalse(result["success"])
        self.assertEqual(result["detail"], "permanent failure: ambiguous_or_unresolved_title")
        self.assertEqual(result["errors"][-1]["type"], "MCP Permanent Failure")

    def test_pipeline_state_persists_blocked_download_summary(self) -> None:
        summary = {
            "total": 1,
            "succeeded": 0,
            "failed": 1,
            "details": [
                {
                    "query": "10.1/missing",
                    "success": False,
                    "errors": [{"type": "CLI Missing"}],
                }
            ],
        }

        state = build_pipeline_state(
            run_id="run-123",
            input_path="outputs/run-123/top_n_dois.json",
            output_dir="outputs/run-123/papers/fulltext",
            summary=summary,
        )

        self.assertEqual(state["stage"], 5)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["blocking_reason"], "paper-fetch CLI not found on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "pipeline_state.json"
            write_pipeline_state(state_file, state)
            saved = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(saved["run_id"], "run-123")
        self.assertEqual(saved["status"], "blocked")


if __name__ == "__main__":
    unittest.main()

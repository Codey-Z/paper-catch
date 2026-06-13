from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.search import run_search


class SearchRunIdTest(unittest.TestCase):
    def test_run_search_uses_pipeline_params_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = tmp_path / "pipeline_params.json"
            output = tmp_path / "search_results.json"
            params.write_text(
                json.dumps({"run_id": "fixed-run-id"}, ensure_ascii=False),
                encoding="utf-8",
            )

            def fake_search(keyword, year_from, year_to, max_results, client):
                return (
                    [
                        {
                            "title": "Example Paper",
                            "doi": "10.1234/example",
                            "source": ["crossref"],
                            "abstract": "example",
                            "has_abstract": True,
                        }
                    ],
                    [],
                )

            with patch("pipeline.search.search_crossref", side_effect=fake_search):
                exit_code = run_search(
                    keywords=["example"],
                    sources=["crossref"],
                    year_from=2024,
                    year_to=2026,
                    max_results=5,
                    output_path=str(output),
                    params_path=str(params),
                    registry={"sources": {}},
                )

            self.assertEqual(exit_code, 0)
            data = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(data["run_id"], "fixed-run-id")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.score import run_scoring


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "plant_genome_llm_search_results.json"


class RelevanceScoringTest(unittest.TestCase):
    def test_plant_genome_llm_profile_prioritizes_core_papers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "top_n.json"
            exit_code = run_scoring(
                input_path=str(FIXTURE),
                config_path=str(ROOT / "config" / "scoring.yaml"),
                top_n=20,
                output_path=str(output),
                scoring_mode="frontier",
                relevance_profile_name="plant_genome_llm",
            )
            self.assertEqual(exit_code, 0)

            data = json.loads(output.read_text(encoding="utf-8"))
            titles = [paper["title"] for paper in data["papers"]]

            self.assertLess(titles.index("PlantGFM: A Genomic Foundation Model for Discovery and Creation of Plant Genes."), 3)
            self.assertLess(titles.index("PlantBiMoE: A Bidirectional Foundation Model with SparseMoE for Plant Genomes"), 5)
            self.assertLess(
                titles.index("Genomic language models with k-mer tokenization strategies for plant genome annotation and regulatory element strength prediction"),
                5,
            )

            plantgfm_rank = titles.index("PlantGFM: A Genomic Foundation Model for Discovery and Creation of Plant Genes.")
            phenotyping_rank = titles.index("A conversational multi-agent AI system for automated plant phenotyping.")
            self.assertGreater(phenotyping_rank, plantgfm_rank)

            first = data["papers"][0]
            for field in [
                "topic_relevance",
                "matched_concept_groups",
                "missing_required_groups",
                "relevance_flags",
            ]:
                self.assertIn(field, first)

            weak = next(
                paper for paper in data["papers"]
                if paper["title"] == "A conversational multi-agent AI system for automated plant phenotyping."
            )
            self.assertIn("missing_ai_model_group", weak["risk_flags"])


if __name__ == "__main__":
    unittest.main()

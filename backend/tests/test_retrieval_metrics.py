"""Hand-calculated scoring checks independent of retriever implementations."""

import importlib.util
import math
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "retest_metrics", Path(__file__).parents[1] / "scripts/retest_retrieval.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MetricTests(unittest.TestCase):
    def test_two_labels_ranked_second_and_fourth(self):
        r = module.score_hits(["a", "b", "c", "d"], ["b", "d"], 4)
        self.assertEqual(r["recall"], 1)
        self.assertEqual(r["precision"], 0.5)
        self.assertEqual(r["mrr"], 0.5)
        self.assertAlmostEqual(
            r["ndcg"], (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
        )

    def test_no_hits(self):
        self.assertEqual(
            module.score_hits([], ["a"], 5), dict.fromkeys(module.METRICS, 0.0)
        )

    def test_partial_recall_is_not_complete_evidence(self):
        r = module.score_hits(["b"], ["a", "b"], 5)
        self.assertEqual(r["recall"], 0.5)
        self.assertEqual(r["hit_rate"], 1)
        self.assertEqual(r["complete_evidence"], 0)

    def test_invalid_inputs(self):
        for args in [(["a", "a"], ["a"], 5), ([], [], 5), ([], ["a"], 0)]:
            with self.assertRaises(ValueError):
                module.score_hits(*args)

    def test_percentile_interpolates(self):
        self.assertEqual(module.percentile([10, 20, 30], 0.95), 29)
        self.assertEqual(module.percentile([12], 0.95), 12)

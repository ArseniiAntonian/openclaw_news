from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_regression import (  # noqa: E402
    DEFAULT_MIN_POSITIVES,
    load_truth,
    recall,
)


def _write_labels(rows: list[dict]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for row in rows:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.close()
    return Path(handle.name)


class LoadTruthMinPositivesTests(unittest.TestCase):
    """Грабля 2026-08-07: фильтр --min-positives был потерян при переносе
    из experiments/retrieval_eval/run_eval.py, и объект с 4 положительными
    попал в замер, дав "recall 75%" на трёх документах из четырёх."""

    def test_object_below_min_positives_is_dropped(self) -> None:
        # объект 1 -- 3 положительных (ниже порога), объект 2 -- 6 (выше)
        rows = [{"id_clean_post": i, "label_objects": [1]} for i in range(3)]
        rows += [{"id_clean_post": 100 + i, "label_objects": [2]} for i in range(6)]
        path = _write_labels(rows)
        try:
            truth = load_truth(path, min_positives=5)
        finally:
            path.unlink()

        self.assertNotIn(1, truth)  # шумный объект не меряем
        self.assertIn(2, truth)
        self.assertEqual(len(truth[2]), 6)

    def test_default_min_positives_matches_original_measurement(self) -> None:
        self.assertEqual(DEFAULT_MIN_POSITIVES, 5)

    def test_min_positives_zero_keeps_everything(self) -> None:
        rows = [{"id_clean_post": 1, "label_objects": [1]}]
        path = _write_labels(rows)
        try:
            truth = load_truth(path, min_positives=0)
        finally:
            path.unlink()
        self.assertIn(1, truth)

    def test_relation_any_includes_mentions(self) -> None:
        rows = [
            {"id_clean_post": i, "label_objects": [1], "mention_objects": [2]}
            for i in range(5)
        ]
        path = _write_labels(rows)
        try:
            events_only = load_truth(path, min_positives=5, relation="event")
            with_mentions = load_truth(path, min_positives=5, relation="any")
        finally:
            path.unlink()

        self.assertEqual(set(events_only), {1})
        self.assertEqual(set(with_mentions), {1, 2})


class RecallTests(unittest.TestCase):
    def test_partial_recall(self) -> None:
        self.assertEqual(recall({1, 2}, {1, 2, 3, 4}), 0.5)

    def test_empty_positives_is_zero_not_crash(self) -> None:
        self.assertEqual(recall({1}, set()), 0.0)

    def test_extra_selected_docs_do_not_inflate_recall(self) -> None:
        # отобрали лишнего -- recall считается только по положительным
        self.assertEqual(recall({1, 2, 99}, {1, 2}), 1.0)


if __name__ == "__main__":
    unittest.main()
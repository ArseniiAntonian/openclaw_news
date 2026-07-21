from __future__ import annotations

import os
import unittest

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1 import preprocess_worker as pw  # noqa: E402
from agent_1.preprocess_v5 import (  # noqa: E402
    DedupEntry,
    DedupState,
    Verdict,
    build_junk_state_from_rows,
    compute_verdict,
    process_batch,
    write_verdicts,
)

JUNK = pw.JUNK_PATTERNS
GUARD = pw.PROTECTED_BUSINESS_CONTEXT_RE


def raw(id_raw_post: int, title: str, content: str, time_post: str = "2026-07-21T00:00:00Z"):
    return {
        "id_raw_post": id_raw_post,
        "time_post": time_post,
        "title": title,
        "content": content,
    }


class FakeCursor:
    """Records executed statements; returns queued rows for RETURNING inserts."""

    def __init__(self, returning_rows=None):
        self.executed: list[tuple[str, list]] = []
        self._returning = list(returning_rows or [])

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, list(params) if params is not None else None))

    def fetchall(self):
        return self._returning.pop(0) if self._returning else []


class JunkStateTests(unittest.TestCase):
    def test_splits_guard_and_categories(self) -> None:
        rows = [
            {"category": "weather", "patterns": ["погод\\w*", "снег\\w*", "мороз\\w*"], "is_business_guard": False},
            {"category": "sports", "patterns": ["футбол\\w*", "матч\\w*"], "is_business_guard": False},
            {"category": "protected_business_context", "patterns": ["сбер\\w*", "банк\\w*"], "is_business_guard": True},
        ]
        junk_patterns, guard_re = build_junk_state_from_rows(rows)
        self.assertEqual([c for c, _ in junk_patterns], ["weather", "sports"])
        self.assertTrue(guard_re.search("сбербанк объявил"))
        self.assertTrue(junk_patterns[0][1].search("сильный снегопад"))

    def test_missing_guard_falls_back(self) -> None:
        rows = [{"category": "weather", "patterns": ["погод\\w*"], "is_business_guard": False}]
        _, guard_re = build_junk_state_from_rows(rows)
        self.assertIs(guard_re, pw.PROTECTED_BUSINESS_CONTEXT_RE)


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dedup = DedupState()

    def _v(self, r) -> Verdict:
        return compute_verdict(r, junk_patterns=JUNK, guard_re=GUARD, dedup=self.dedup)

    def test_kept_russian_business_doc(self) -> None:
        v = self._v(raw(1, "Сбербанк", "Сбербанк запустил новый сервис для клиентов в мобильном приложении сегодня утром сообщила компания"))
        self.assertIsNone(v.drop_reason)
        self.assertFalse(v.is_duplicate)
        self.assertTrue(v.clean_content)
        self.assertTrue(v.content_hash)

    def test_non_russian_dropped(self) -> None:
        v = self._v(raw(1, "Bank", "The company launched a new banking service for its customers today the bank announced in a statement"))
        self.assertEqual(v.drop_reason, "non_russian")

    def test_junk_weather_dropped(self) -> None:
        v = self._v(raw(1, "Погода", "Синоптики предупредили о сильном снегопаде и морозе в выходные погода резко ухудшится ожидаются осадки"))
        self.assertEqual(v.drop_reason, "junk:weather")

    def test_empty_dropped(self) -> None:
        self.assertEqual(self._v(raw(1, "", "   ")).drop_reason, "empty_clean_text")

    def test_exact_duplicate_within_batch(self) -> None:
        text = "Сбербанк запустил новый сервис для клиентов в мобильном приложении сегодня утром сообщила компания"
        first = self._v(raw(1, "Сбербанк", text))
        second = self._v(raw(2, "Сбербанк", text))
        self.assertIsNone(first.drop_reason)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(second.drop_reason, "duplicate")
        self.assertEqual(second.dup_score, 1.0)
        self.assertEqual(second.canonical.id_raw_post, 1)

    def test_near_duplicate_within_batch(self) -> None:
        base = "Сбербанк открыл новый крупный офис в центре Казани и объявил о запуске обновлённой линии обслуживания клиентов сегодня днём"
        first = self._v(raw(1, "Сбербанк", base))
        second = self._v(raw(2, "Сбербанк", base + " утром для всех"))
        self.assertIsNone(first.drop_reason)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(second.canonical.id_raw_post, 1)

    def test_distinct_docs_both_kept(self) -> None:
        v1 = self._v(raw(1, "Сбербанк", "Сбербанк повысил ставки по вкладам для розничных клиентов в новом сезоне сообщила пресс служба банка"))
        v2 = self._v(raw(2, "ВТБ", "ВТБ представил обновлённое мобильное приложение с поддержкой платежей по биометрии для корпоративных клиентов сегодня"))
        self.assertIsNone(v1.drop_reason)
        self.assertIsNone(v2.drop_reason)
        self.assertFalse(v2.is_duplicate)

    def test_process_batch_returns_one_verdict_per_row(self) -> None:
        rows = [raw(i, "T", f"Сбербанк новость номер {i} про сервис для клиентов в мобильном приложении банка сегодня") for i in range(5)]
        verdicts = process_batch(rows, junk_patterns=JUNK, guard_re=GUARD, dedup=self.dedup)
        self.assertEqual(len(verdicts), 5)


class WriteVerdictsTests(unittest.TestCase):
    def test_two_phase_resolves_within_batch_canonical(self) -> None:
        canonical = DedupEntry(id_raw_post=1, id_clean_post=None, content_hash="h1", near_doc="x", band_keys=frozenset())
        verdicts = [
            Verdict(id_raw_post=1, time_post="t", clean_content="c1", content_hash="h1"),
            Verdict(id_raw_post=2, time_post="t", drop_reason="duplicate", is_duplicate=True, dup_score=0.9, canonical=canonical),
            Verdict(id_raw_post=3, time_post="t", drop_reason="non_russian"),
        ]
        cur = FakeCursor(returning_rows=[[(1, 5001)]])
        kept_ids = write_verdicts(cur, verdicts)
        self.assertEqual(kept_ids, {1: 5001})
        self.assertEqual(len(cur.executed), 3)  # kept, dropped, dups
        dup_sql, dup_params = cur.executed[2]
        self.assertIn("id_canonical_post", dup_sql)
        self.assertIn(5001, dup_params)  # within-batch canonical -> new id

    def test_existing_canonical_uses_stored_id(self) -> None:
        canonical = DedupEntry(id_raw_post=1, id_clean_post=4242, content_hash="h1", near_doc="x", band_keys=frozenset())
        verdicts = [
            Verdict(id_raw_post=9, time_post="t", drop_reason="duplicate", is_duplicate=True, dup_score=0.8, canonical=canonical),
        ]
        cur = FakeCursor()
        write_verdicts(cur, verdicts)
        self.assertEqual(len(cur.executed), 1)  # only dups
        _, dup_params = cur.executed[0]
        self.assertIn(4242, dup_params)


if __name__ == "__main__":
    unittest.main()
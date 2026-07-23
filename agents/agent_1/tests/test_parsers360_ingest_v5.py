from __future__ import annotations

import os
import unittest

os.environ.setdefault("PARSERS360_API_URL", "https://example.com")
os.environ.setdefault("PARSERS360_TOKEN", "x")
os.environ.setdefault("PARSERS360_BASIC_USER", "x")
os.environ.setdefault("PARSERS360_BASIC_PASSWORD", "x")
os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.parsers360_ingest_v5 import (  # noqa: E402
    insert_raw_post,
    resolve_source_id,
)


class FakeCursor:
    """Records executed statements; returns queued fetchone() results in order."""

    def __init__(self, fetchone_results=None):
        self.executed: list[tuple[str, tuple]] = []
        self._results = list(fetchone_results or [])

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self._results.pop(0)


def item(**overrides):
    base = {
        "id": 12345,
        "url": "https://example.com/news/1",
        "content": "Полный текст новости про сбербанк и клиентов сегодня",
        "title": "Заголовок",
        "created_at": "1753142400",  # arbitrary valid unix ts
        "source": "РИА Новости",
        "summary": "краткое содержание",
        "companies": ["Сбербанк"],
        "is_duplicated": False,
        "original_id": "orig-1",
    }
    base.update(overrides)
    return base


class InsertRawPostSkipTests(unittest.TestCase):
    def test_missing_url_is_skipped(self) -> None:
        cur = FakeCursor()
        result = insert_raw_post(cur, item(url=None), {})
        self.assertEqual(result, "no_url")
        self.assertEqual(cur.executed, [])  # no DB call attempted at all

    def test_missing_content_is_skipped(self) -> None:
        cur = FakeCursor()
        result = insert_raw_post(cur, item(content=""), {})
        self.assertEqual(result, "no_content")
        self.assertEqual(cur.executed, [])

    def test_missing_created_at_is_skipped(self) -> None:
        cur = FakeCursor()
        result = insert_raw_post(cur, item(created_at=None), {})
        self.assertEqual(result, "no_time_post")
        self.assertEqual(cur.executed, [])

    def test_unparseable_created_at_is_skipped(self) -> None:
        cur = FakeCursor()
        result = insert_raw_post(cur, item(created_at="not-a-timestamp"), {})
        self.assertEqual(result, "no_time_post")


class InsertRawPostSuccessTests(unittest.TestCase):
    def test_valid_item_inserts_and_resolves_source(self) -> None:
        # first execute() is the source upsert (RETURNING id_source),
        # second is the raw_posts insert (no RETURNING, no fetchone call)
        cur = FakeCursor(fetchone_results=[(4242,)])
        result = insert_raw_post(cur, item(), {})
        self.assertEqual(result, "inserted")
        self.assertEqual(len(cur.executed), 2)

        source_sql, source_params = cur.executed[0]
        self.assertIn("agent_1_v5.source", source_sql)
        self.assertEqual(source_params, ("РИА Новости",))

        raw_sql, raw_params = cur.executed[1]
        self.assertIn("agent_1_v5.raw_posts", raw_sql)
        self.assertEqual(raw_params[0], 4242)  # id_source from the upsert
        self.assertEqual(raw_params[1], "parsers360")  # parser, not the outlet name
        self.assertEqual(raw_params[3], "https://example.com/news/1")  # url
        self.assertIn('"external_id": "12345"', raw_params[6])  # metadata jsonb

    def test_second_call_with_same_source_uses_cache(self) -> None:
        cache: dict[str, int] = {}
        cur1 = FakeCursor(fetchone_results=[(99,)])
        insert_raw_post(cur1, item(id=1), cache)
        self.assertEqual(cache["риа новости"], 99)

        cur2 = FakeCursor()  # no queued fetchone result -- must not be called
        result = insert_raw_post(cur2, item(id=2), cache)
        self.assertEqual(result, "inserted")
        self.assertEqual(len(cur2.executed), 1)  # only the raw_posts insert
        self.assertIn("agent_1_v5.raw_posts", cur2.executed[0][0])


class ResolveSourceIdTests(unittest.TestCase):
    def test_upserts_and_caches_by_casefold(self) -> None:
        cur = FakeCursor(fetchone_results=[(7,)])
        cache: dict[str, int] = {}
        first = resolve_source_id(cur, cache, "Ведомости")
        self.assertEqual(first, 7)
        self.assertEqual(len(cur.executed), 1)

        second = resolve_source_id(cur, cache, "ведомости")  # different case
        self.assertEqual(second, 7)
        self.assertEqual(len(cur.executed), 1)  # no new query


if __name__ == "__main__":
    unittest.main()
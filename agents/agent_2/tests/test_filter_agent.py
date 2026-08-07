from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_2.catalog import ContractError, _row_to_object  # noqa: E402
from agent_2.channels import union_candidates  # noqa: E402
from agent_2.db import to_postgres  # noqa: E402
from agent_2.llm_scoring import (  # noqa: E402
    ScoringConfig,
    ScoringError,
    _is_capacity_error_detail,
    _parse_retry_after_seconds,
    call_openclaw,
    fetch_cached_scores,
    parse_batch_score_response,
    parse_score_response,
    score_candidates,
    store_score,
)


class ToPostgresTests(unittest.TestCase):
    def test_word_boundary_translated(self) -> None:
        self.assertEqual(to_postgres(r"\bgigachat\b"), r"\ygigachat\y")

    def test_leaves_other_syntax_untouched(self) -> None:
        pattern = r"открыт\w+\s+модел\w+"
        self.assertEqual(to_postgres(pattern), pattern)


class UnionCandidatesTests(unittest.TestCase):
    def test_union_not_intersection(self) -> None:
        # design D1: объединение, не пересечение -- документ, найденный
        # только одним каналом, всё равно должен войти в кандидаты.
        vector_scored = [(1, 0.9), (2, 0.5)]
        keyword_docs = {2, 3}
        result = union_candidates(vector_scored, keyword_docs)
        self.assertEqual(result, {1, 2, 3})

    def test_empty_channels(self) -> None:
        self.assertEqual(union_candidates([], set()), set())


class RowToObjectTests(unittest.TestCase):
    def test_missing_search_description_raises_contract_error(self) -> None:
        row = {
            "id_object": 5,
            "label": "Доверие к ИИ",
            "aliases": [],
            "keywords": None,
            "negative_filter": None,
            "search_description": None,
            "query_embedding": None,
        }
        with self.assertRaises(ContractError):
            _row_to_object(row)

    def test_blank_search_description_raises_contract_error(self) -> None:
        row = {
            "id_object": 5,
            "label": "Доверие к ИИ",
            "aliases": [],
            "keywords": None,
            "negative_filter": None,
            "search_description": "   ",
            "query_embedding": None,
        }
        with self.assertRaises(ContractError):
            _row_to_object(row)

    def test_valid_row_parses(self) -> None:
        row = {
            "id_object": 1,
            "label": "GigaChat",
            "aliases": ["GigaChat", "ГигаЧат"],
            "keywords": "gigachat|гигачат",
            "negative_filter": "гороскоп",
            "search_description": "Новости о GigaChat.",
            "query_embedding": None,
        }
        obj = _row_to_object(row)
        self.assertEqual(obj.id_object, 1)
        self.assertEqual(obj.label, "GigaChat")
        self.assertEqual(obj.search_description, "Новости о GigaChat.")


class ScoringResponseTests(unittest.TestCase):
    def test_direct_json_payload(self) -> None:
        raw = json.dumps({"score": 8.5, "reason": "новость целиком об объекте"})
        score, reason = parse_score_response(raw)
        self.assertEqual(score, 8.5)
        self.assertEqual(reason, "новость целиком об объекте")

    def test_wrapped_in_markdown_fence(self) -> None:
        raw = '```json\n{"score": 3, "reason": "упомянут вскользь"}\n```'
        score, _ = parse_score_response(raw)
        self.assertEqual(score, 3.0)

    def test_wrapped_in_openclaw_envelope(self) -> None:
        raw = json.dumps(
            {
                "result": {
                    "meta": {
                        "finalAssistantVisibleText": json.dumps(
                            {"score": 0, "reason": "не относится"}
                        )
                    }
                }
            }
        )
        score, reason = parse_score_response(raw)
        self.assertEqual(score, 0.0)
        self.assertEqual(reason, "не относится")

    def test_score_out_of_range_rejected(self) -> None:
        raw = json.dumps({"score": 11, "reason": "x"})
        with self.assertRaises(Exception):
            parse_score_response(raw)

    def test_batch_response_requires_exact_document_set(self) -> None:
        raw = json.dumps({"scores": [
            {"id_clean_post": 101, "score": 8},
            {"id_clean_post": 102, "score": 2.5},
        ]})
        self.assertEqual(
            parse_batch_score_response(raw, {101, 102}),
            {101: 8.0, 102: 2.5},
        )

    def test_batch_response_rejects_missing_document(self) -> None:
        raw = json.dumps({"scores": [{"id_clean_post": 101, "score": 8}]})
        with self.assertRaises(Exception):
            parse_batch_score_response(raw, {101, 102})


class CallOpenclawTimeoutTests(unittest.TestCase):
    """Найдено при разборе прода 2026-08-07: subprocess.run(timeout=...)
    поднимает TimeoutExpired -- отдельный от ScoringError тип, который
    ничем не ловился и уронил бы весь прогон, если бы openclaw завис."""

    def test_timeout_expired_becomes_scoring_error(self) -> None:
        config = ScoringConfig(openclaw_cmd="openclaw", agent_id="agent_2", agent_timeout=1)
        with patch(
            "agent_2.llm_scoring.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=61),
        ):
            with self.assertRaises(ScoringError):
                call_openclaw("prompt", "session-key", config=config)


class CapacityErrorDetectionTests(unittest.TestCase):
    def test_detects_429(self) -> None:
        self.assertTrue(_is_capacity_error_detail("HTTP 429 Too Many Requests"))

    def test_detects_rate_limit_phrase(self) -> None:
        self.assertTrue(_is_capacity_error_detail("rate limit exceeded, try later"))

    def test_normal_error_not_capacity(self) -> None:
        self.assertFalse(_is_capacity_error_detail("connection refused"))

    def test_parses_retry_after_seconds(self) -> None:
        detail = 'error 429: {"retry_after": "12.5"}'
        self.assertEqual(_parse_retry_after_seconds(detail), 12.5)

    def test_no_retry_after_returns_none(self) -> None:
        self.assertIsNone(_parse_retry_after_seconds("429 too many requests"))


class _FakeCursor:
    """Достаточно cursor-подобного поведения, чтобы проверить SQL и
    параметры без реальной БД -- tasks.md 12.2: кэш решающей оценки не
    должен путать модели."""

    def __init__(self, fetch_rows: list[tuple[Any, Any]] | None = None) -> None:
        self.fetch_rows = fetch_rows or []
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))

    def fetchall(self):
        return self.fetch_rows


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self) -> None:
        self.committed = True


class CacheModelIsolationTests(unittest.TestCase):
    """design D6: кэш без модели в ключе молча переиспользует чужие
    оценки -- ключ ОБЯЗАН быть тройкой (id_object, id_clean_post, model)."""

    def test_fetch_cached_scores_filters_by_model(self) -> None:
        cursor = _FakeCursor(fetch_rows=[(101, 8.0)])
        conn = _FakeConn(cursor)

        result = fetch_cached_scores(conn, id_object=1, doc_ids=[101, 102], model="opus")

        self.assertEqual(result, {101: 8.0})
        sql, params = cursor.executed[0]
        self.assertIn("model = %s", sql)
        self.assertIn("opus", params)

    def test_different_models_are_independent_queries(self) -> None:
        cursor_opus = _FakeCursor(fetch_rows=[(101, 9.0)])
        cursor_sonnet = _FakeCursor(fetch_rows=[])  # sonnet ещё не оценивал этот документ
        conn_opus = _FakeConn(cursor_opus)
        conn_sonnet = _FakeConn(cursor_sonnet)

        opus_scores = fetch_cached_scores(conn_opus, id_object=1, doc_ids=[101], model="opus")
        sonnet_scores = fetch_cached_scores(conn_sonnet, id_object=1, doc_ids=[101], model="sonnet")

        # Смена модели НЕ должна молча вернуть оценку другой модели.
        self.assertEqual(opus_scores, {101: 9.0})
        self.assertEqual(sonnet_scores, {})

    def test_store_score_writes_model_in_key(self) -> None:
        cursor = _FakeCursor()
        conn = _FakeConn(cursor)

        store_score(conn, id_object=1, id_clean_post=101, model="opus", score=7.5)

        sql, params = cursor.executed[0]
        self.assertIn("model", sql)
        self.assertIn("ON CONFLICT (id_object, id_clean_post, model)", sql)
        self.assertIn("opus", params)
        self.assertTrue(conn.committed)


class ScoreCandidatesResilienceTests(unittest.TestCase):
    """design D7 + грабля прода 2026-08-06: сбой на одном кандидате
    ронял ВЕСЬ прогон (включая объекты, которые ещё не начинались) --
    не было try/except вокруг вызова оценки."""

    def test_one_failing_candidate_does_not_abort_the_rest(self) -> None:
        cursor = _FakeCursor(fetch_rows=[])  # кэш пуст -- все 3 кандидата новые
        conn = _FakeConn(cursor)
        config = ScoringConfig(
            openclaw_cmd="openclaw", agent_id="agent_2", rate_limit_per_minute=0,
            batch_size=1,
        )
        candidates = [
            {"id_clean_post": 1, "title": "t1", "text": "x1"},
            {"id_clean_post": 2, "title": "t2", "text": "x2"},
            {"id_clean_post": 3, "title": "t3", "text": "x3"},
        ]

        def fake_call(prompt: str, session_key: str, *, config: ScoringConfig) -> str:
            if "batch-1" in session_key:
                raise ScoringError("boom: провайдер вернул мусор")
            doc_id = 1 if "batch-0" in session_key else 3
            return json.dumps({"scores": [{"id_clean_post": doc_id, "score": 8.0, "reason": "ok"}]})

        with patch("agent_2.llm_scoring.call_openclaw_with_backoff", side_effect=fake_call):
            scores = score_candidates(
                conn,
                id_object=1,
                label="GigaChat",
                search_description="Новости о GigaChat.",
                candidates=candidates,
                config=config,
            )

        # doc 1 и 3 оценены несмотря на падение doc 2 -- сбой одного
        # кандидата НЕ прервал обработку остальных (D7).
        self.assertEqual(set(scores), {1, 3})
        self.assertNotIn(2, scores)  # ...и не пропущен молча (assert ниже это подтверждает)

    def test_all_candidates_failing_does_not_raise(self) -> None:
        cursor = _FakeCursor(fetch_rows=[])
        conn = _FakeConn(cursor)
        config = ScoringConfig(
            openclaw_cmd="openclaw", agent_id="agent_2", rate_limit_per_minute=0
        )
        candidates = [{"id_clean_post": 1, "title": "t1", "text": "x1"}]

        with patch(
            "agent_2.llm_scoring.call_openclaw_with_backoff",
            side_effect=ScoringError("провайдер недоступен"),
        ):
            scores = score_candidates(
                conn,
                id_object=1,
                label="GigaChat",
                search_description="Новости о GigaChat.",
                candidates=candidates,
                config=config,
            )

        self.assertEqual(scores, {})  # ни одного исключения наружу, пустой результат


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.label_kr_worker import (  # noqa: E402
    KrLabel,
    LabelValidationError,
    build_impact_prompt,
    build_relevance_prompt,
    build_session_key,
    build_sber_paid_news_prompt,
    extract_kr_source_rankings,
    get_document_source_keys,
    label_clean_item,
    parse_agent_label_payload,
    replace_document_kr_labels,
    source_rankings_allow_label,
    validate_entity_tonality_payload,
    validate_impact_payload,
    validate_relevance_payload,
    validate_sber_paid_news_payload,
)


def impact_payload(impact: str = "positive") -> dict[str, object]:
    return {
        "impact": impact,
        "signal_strength": "direct",
        "theme": "Рост Сбера",
        "dashboard_description": "Новость подтверждает прогресс по цели.",
        "why_for_goal": "Факт из новости повышает вероятность достижения цели.",
        "evidence": ["Сбер улучшил продукт"],
        "reasoning_steps": ["факт", "влияние", "связь с целью"],
        "uncertainty": "Не доказан долгосрочный эффект.",
        "confidence": 0.8,
    }


def relevance_payload(*kr_ids: int) -> dict[str, object]:
    return {
        "matches": [
            {
                "kr_id": kr_id,
                "why_related": f"Новость относится к KR {kr_id}.",
                "evidence": ["Сбер улучшил продукт"],
            }
            for kr_id in kr_ids
        ]
    }


class LabelKrWorkerTests(unittest.TestCase):
    def test_parse_direct_impact_payload(self) -> None:
        payload = parse_agent_label_payload(json.dumps(impact_payload()))
        self.assertEqual(payload["impact"], "positive")

    def test_parse_openclaw_payload_text(self) -> None:
        response = {
            "status": "ok",
            "result": {
                "payloads": [
                    {
                        "text": "```json\n"
                        + json.dumps(impact_payload("neutral"), ensure_ascii=False)
                        + "\n```"
                    }
                ]
            },
        }
        payload = parse_agent_label_payload(json.dumps(response, ensure_ascii=False))
        self.assertEqual(payload["impact"], "neutral")

    def test_validate_accepts_grounded_impact_payload(self) -> None:
        label = validate_impact_payload(
            impact_payload(),
            evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
            kr_id=7,
        )

        self.assertEqual(label.kr_id, 7)
        self.assertEqual(label.impact, "positive")
        self.assertEqual(label.confidence, 0.8)
        self.assertEqual(label.evidence, ("Сбер улучшил продукт",))

    def test_validate_relevance_payload_accepts_grounded_matches(self) -> None:
        matches = validate_relevance_payload(
            relevance_payload(1, 3),
            evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
            candidate_kr_ids={1, 2, 3},
        )

        self.assertEqual([match.kr_id for match in matches], [1, 3])
        self.assertEqual(matches[0].evidence, ("Сбер улучшил продукт",))

    def test_validate_relevance_payload_rejects_unknown_kr(self) -> None:
        with self.assertRaisesRegex(LabelValidationError, "candidate KR set"):
            validate_relevance_payload(
                relevance_payload(99),
                evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
                candidate_kr_ids={1, 2, 3},
            )

    def test_validate_rejects_bad_confidence_step(self) -> None:
        payload = impact_payload()
        payload["confidence"] = 0.85

        with self.assertRaisesRegex(LabelValidationError, "0.1 step"):
            validate_impact_payload(
                payload,
                evidence_text="Сбер улучшил продукт",
                kr_id=7,
            )

    def test_validate_rejects_ungrounded_evidence(self) -> None:
        with self.assertRaisesRegex(LabelValidationError, "not present"):
            validate_impact_payload(
                impact_payload(),
                evidence_text="Другой текст",
                kr_id=7,
            )

    def test_validate_accepts_evidence_with_wrapping_quotes(self) -> None:
        payload = impact_payload()
        payload["evidence"] = ['"Сбер улучшил продукт"']

        label = validate_impact_payload(
            payload,
            evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
            kr_id=7,
        )

        self.assertEqual(label.evidence, ("Сбер улучшил продукт",))

    def test_validate_sber_paid_news_payload_accepts_only_zero_or_one(self) -> None:
        self.assertEqual(validate_sber_paid_news_payload({"is_sber_paid_news": 1}), 1)
        self.assertEqual(validate_sber_paid_news_payload({"is_sber_paid_news": 0}), 0)

        with self.assertRaisesRegex(LabelValidationError, "0 or 1"):
            validate_sber_paid_news_payload({"is_sber_paid_news": True})

    def test_validate_entity_tonality_payload_accepts_grounded_mentions(self) -> None:
        payload = validate_entity_tonality_payload(
            {
                "mentions": [
                    {
                        "text": "Сбер улучшил продукт",
                        "sentiment": "positive",
                        "justification": "Фрагмент описывает явное улучшение.",
                        "confidence": 0.9,
                    }
                ]
            },
            evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
        )

        self.assertEqual(
            payload,
            {
                "mentions": [
                    {
                        "text": "Сбер улучшил продукт",
                        "sentiment": "positive",
                        "justification": "Фрагмент описывает явное улучшение.",
                        "confidence": 0.9,
                    }
                ]
            },
        )

    def test_validate_entity_tonality_payload_rejects_ungrounded_mentions(self) -> None:
        with self.assertRaisesRegex(LabelValidationError, "not present"):
            validate_entity_tonality_payload(
                {
                    "mentions": [
                        {
                            "text": "Сбер запустил сервис",
                            "sentiment": "neutral",
                            "justification": "Факт без оценки.",
                            "confidence": 0.6,
                        }
                    ]
                },
                evidence_text="Другой текст",
            )

    def test_validate_entity_tonality_payload_strips_wrapping_quotes(self) -> None:
        payload = validate_entity_tonality_payload(
            {
                "mentions": [
                    {
                        "text": "«Сбер улучшил продукт»",
                        "sentiment": "positive",
                        "justification": "Фрагмент описывает явное улучшение.",
                        "confidence": 0.9,
                    }
                ]
            },
            evidence_text="Заголовок\nСбер улучшил продукт для клиентов.",
        )

        self.assertEqual(payload["mentions"][0]["text"], "Сбер улучшил продукт")

    def test_build_session_key_scopes_to_agent_and_step(self) -> None:
        self.assertEqual(
            build_session_key("agent_1", "label kr", 10, 20, "impact kr 7"),
            "agent:agent_1:label-kr-clean-10-job-20-impact-kr-7",
        )

    def test_build_impact_prompt_uses_exact_template_inputs(self) -> None:
        clean_item = {
            "clean_title": "Заголовок",
            "clean_text": "Сбер улучшил продукт",
            "source": "parsers360",
            "source_metadata": {"source": "Ведомости"},
            "url": "https://example.com/news/1",
        }
        kr = {"title": "Цель", "description": "Описание цели"}

        prompt = build_impact_prompt(clean_item, kr)

        self.assertTrue(prompt.startswith("Ты — senior business analyst"))
        self.assertIn("GOAL: Цель\n\nОписание цели", prompt)
        self.assertIn("SOURCE: Ведомости", prompt)
        self.assertIn("URL_DOMAIN: example.com", prompt)
        self.assertIn("TITLE: Заголовок", prompt)
        self.assertIn("TEXT: Сбер улучшил продукт", prompt)

    def test_build_relevance_prompt_uses_goal_catalog_and_exact_inputs(self) -> None:
        clean_item = {
            "clean_title": "Заголовок",
            "clean_text": "Сбер улучшил продукт",
            "source": "parsers360",
            "source_metadata": {"source": "Ведомости"},
            "url": "https://example.com/news/1",
        }
        candidate_krs = [
            {
                "id": 5,
                "title": "Цель",
                "description": "Описание цели",
                "enrichment": {"ключевые_слова": ["GenAI", "зарплатный проект"]},
            }
        ]

        prompt = build_relevance_prompt(clean_item, candidate_krs)

        self.assertTrue(prompt.startswith("Ты — senior business analyst"))
        self.assertIn("KR_ID: 5", prompt)
        self.assertIn("GOAL: Цель Описание цели", prompt)
        self.assertIn("KEYWORDS: GenAI, зарплатный проект", prompt)
        self.assertIn("SOURCE: Ведомости", prompt)
        self.assertIn("URL_DOMAIN: example.com", prompt)
        self.assertIn("TITLE: Заголовок", prompt)
        self.assertIn("TEXT: Сбер улучшил продукт", prompt)

    def test_label_clean_item_runs_prompts_in_order(self) -> None:
        clean_item = {
            "id": 10,
            "raw_item_id": 100,
            "clean_title": "Сбер улучшил продукт",
            "clean_text": "Сбер улучшил продукт для клиентов.",
            "source": "parsers360",
            "source_metadata": {"source": "РБК"},
            "url": "https://example.com/news/1",
        }
        active_krs = [
            {"id": 1, "title": "KR 1", "description": "Описание 1"},
            {"id": 2, "title": "KR 2", "description": "Описание 2"},
            {"id": 3, "title": "KR 3", "description": "Описание 3"},
        ]
        calls: list[str] = []

        def runner(
            prompt: str,
            item: dict[str, object],
            job_id: int,
            step_name: str,
        ) -> str:
            self.assertEqual(item["id"], 10)
            self.assertEqual(job_id, 99)
            calls.append(step_name)
            if step_name == "relevance":
                return json.dumps(relevance_payload(1, 2, 3), ensure_ascii=False)
            if step_name == "impact-kr-1":
                return json.dumps(impact_payload("positive"), ensure_ascii=False)
            if step_name == "sber-paid-kr-1":
                return '{"is_sber_paid_news":1}'
            if step_name == "entity-tonality-kr-1":
                return json.dumps(
                    {
                        "mentions": [
                            {
                                "text": "Сбер улучшил продукт",
                                "sentiment": "positive",
                                "justification": "Фрагмент описывает явное улучшение.",
                                "confidence": 0.9,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if step_name == "impact-kr-2":
                return json.dumps(impact_payload("negative"), ensure_ascii=False)
            if step_name == "sber-paid-kr-2":
                return '{"is_sber_paid_news":0}'
            if step_name == "entity-tonality-kr-2":
                return json.dumps({"mentions": []}, ensure_ascii=False)
            if step_name == "impact-kr-3":
                return json.dumps(impact_payload("neutral"), ensure_ascii=False)
            raise AssertionError(f"unexpected step {step_name} for prompt {prompt[:80]}")

        result = label_clean_item(clean_item, active_krs, 99, agent_runner=runner)
        labels = result.labels

        self.assertEqual(
            calls,
            [
                "relevance",
                "impact-kr-1",
                "sber-paid-kr-1",
                "entity-tonality-kr-1",
                "impact-kr-2",
                "sber-paid-kr-2",
                "entity-tonality-kr-2",
                "impact-kr-3",
            ],
        )
        self.assertEqual(result.skipped_kr_ids, [])
        self.assertEqual(len(labels), 3)
        self.assertEqual(labels[0].impact, "positive")
        self.assertEqual(labels[0].is_sber_paid_news, 1)
        self.assertEqual(
            labels[0].prompt3_payload,
            {
                "mentions": [
                    {
                        "text": "Сбер улучшил продукт",
                        "sentiment": "positive",
                        "justification": "Фрагмент описывает явное улучшение.",
                        "confidence": 0.9,
                    }
                ]
            },
        )
        self.assertEqual(labels[1].impact, "negative")
        self.assertEqual(labels[1].is_sber_paid_news, 0)
        self.assertEqual(labels[1].prompt3_payload, {"mentions": []})
        self.assertEqual(labels[2].impact, "neutral")
        self.assertIsNone(labels[2].prompt2_payload)

    def test_sber_prompt_is_rendered_without_old_pipeline_wrapper(self) -> None:
        clean_item = {
            "clean_title": "Новость",
            "clean_text": "Текст",
            "source": "parsers360",
            "url": "https://sber.ru/a",
        }

        prompt = build_sber_paid_news_prompt(clean_item)

        self.assertTrue(prompt.startswith("Ты — senior business analyst"))
        self.assertNotIn("Pipeline stage", prompt)
        self.assertIn("URL_DOMAIN: sber.ru", prompt)

    def test_source_rankings_match_included_source(self) -> None:
        clean_item = {
            "source_metadata": {"source": "РБК"},
            "raw_payload": {},
            "url": "https://www.rbc.ru/news/1",
            "source": "parsers360",
        }
        decision = source_rankings_allow_label(
            clean_item,
            [
                {
                    "source_kind": "source",
                    "source_value": "РБК",
                    "source_key": "рбк",
                    "include": True,
                }
            ],
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "included_by_source_ranking")
        self.assertEqual(decision["document_keys"]["domain"], ("rbc.ru",))

    def test_source_rankings_fail_open_when_kr_has_no_rankings(self) -> None:
        decision = source_rankings_allow_label({"source_metadata": {}, "raw_payload": {}}, [])

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], "no_rankings_for_kr")

    def test_source_rankings_skip_unmatched_and_excluded_sources(self) -> None:
        clean_item = {
            "source_metadata": {"source": "РБК"},
            "raw_payload": {},
            "url": "https://rbc.ru/news/1",
            "source": "parsers360",
        }

        unmatched = source_rankings_allow_label(
            clean_item,
            [
                {
                    "source_kind": "source",
                    "source_value": "Коммерсант",
                    "source_key": "коммерсант",
                    "include": True,
                }
            ],
        )
        excluded = source_rankings_allow_label(
            clean_item,
            [
                {
                    "source_kind": "source",
                    "source_value": "РБК",
                    "source_key": "рбк",
                    "include": False,
                }
            ],
        )

        self.assertFalse(unmatched["allowed"])
        self.assertEqual(unmatched["reason"], "no_matching_source_ranking")
        self.assertFalse(excluded["allowed"])
        self.assertEqual(excluded["reason"], "excluded_by_source_ranking")

    def test_label_clean_item_skips_kr_when_rankings_do_not_match(self) -> None:
        clean_item = {
            "id": 10,
            "raw_item_id": 100,
            "clean_title": "Сбер улучшил продукт",
            "clean_text": "Сбер улучшил продукт для клиентов.",
            "source": "parsers360",
            "source_metadata": {"source": "РБК"},
            "url": "https://rbc.ru/news/1",
        }
        active_krs = [
            {
                "id": 1,
                "title": "KR 1",
                "description": "Описание 1",
                "source_rankings": [
                    {
                        "source_kind": "source",
                        "source_value": "РБК",
                        "source_key": "рбк",
                        "include": True,
                    }
                ],
            },
            {
                "id": 2,
                "title": "KR 2",
                "description": "Описание 2",
                "source_rankings": [
                    {
                        "source_kind": "source",
                        "source_value": "Коммерсант",
                        "source_key": "коммерсант",
                        "include": True,
                    }
                ],
            },
        ]
        calls: list[str] = []

        def runner(
            prompt: str,
            item: dict[str, object],
            job_id: int,
            step_name: str,
        ) -> str:
            calls.append(step_name)
            if step_name == "relevance":
                return json.dumps(relevance_payload(1), ensure_ascii=False)
            return json.dumps(impact_payload("neutral"), ensure_ascii=False)

        result = label_clean_item(clean_item, active_krs, 99, agent_runner=runner)

        self.assertEqual(calls, ["relevance", "impact-kr-1"])
        self.assertEqual([label.kr_id for label in result.labels], [1])
        self.assertEqual(result.skipped_kr_ids, [2])

    def test_extracts_ranked_source_types_from_agent_2_enrichment(self) -> None:
        kr = {
            "id": 1,
            "enrichment": {
                "тема": "финансы",
                "ключевые_слова": ["банк", "кредит"],
                "типы_источников": [
                    {"тип": "СМИ", "важность": "3", "причина": "основной источник"},
                    {"тип": "Блоги", "важность": "2", "причина": "важный контекст"},
                    {"тип": "Видеохостинг", "важность": "1", "причина": "слабый сигнал"},
                ],
            },
        }

        rankings = extract_kr_source_rankings(kr)

        self.assertEqual(
            [(row["source_kind"], row["source_key"], row["include"]) for row in rankings],
            [
                ("source_type", "сми", True),
                ("source_type", "блог", True),
                ("source_type", "видеохостинг", False),
            ],
        )

    def test_label_clean_item_uses_agent_2_enriched_source_types(self) -> None:
        clean_item = {
            "id": 10,
            "raw_item_id": 100,
            "clean_title": "Сбер улучшил продукт",
            "clean_text": "Сбер улучшил продукт для клиентов.",
            "source": "parsers360",
            "source_metadata": {"source": "РБК", "source_type": "СМИ"},
            "url": "https://rbc.ru/news/1",
        }
        active_krs = [
            {
                "id": 1,
                "title": "KR 1",
                "description": "Описание 1",
                "enrichment": {
                    "тема": "финансы",
                    "ключевые_слова": ["банк"],
                    "типы_источников": [
                        {"тип": "СМИ", "важность": "3", "причина": "основной источник"}
                    ],
                },
            },
            {
                "id": 2,
                "title": "KR 2",
                "description": "Описание 2",
                "enrichment": {
                    "тема": "видео",
                    "ключевые_слова": ["ролик"],
                    "типы_источников": [
                        {"тип": "Видеохостинг", "важность": "3", "причина": "основной источник"}
                    ],
                },
            },
        ]
        calls: list[str] = []

        def runner(
            prompt: str,
            item: dict[str, object],
            job_id: int,
            step_name: str,
        ) -> str:
            calls.append(step_name)
            if step_name == "relevance":
                return json.dumps(relevance_payload(1), ensure_ascii=False)
            return json.dumps(impact_payload("neutral"), ensure_ascii=False)

        result = label_clean_item(clean_item, active_krs, 99, agent_runner=runner)

        self.assertEqual(calls, ["relevance", "impact-kr-1"])
        self.assertEqual([label.kr_id for label in result.labels], [1])
        self.assertEqual(result.skipped_kr_ids, [2])

    def test_label_clean_item_filters_irrelevant_krs_before_impact(self) -> None:
        clean_item = {
            "id": 10,
            "raw_item_id": 100,
            "clean_title": "Сбер улучшил продукт",
            "clean_text": "Сбер улучшил продукт для клиентов.",
            "source": "parsers360",
            "source_metadata": {"source": "РБК", "source_type": "СМИ"},
            "url": "https://rbc.ru/news/1",
        }
        active_krs = [
            {"id": 1, "title": "KR 1", "description": "Описание 1"},
            {"id": 2, "title": "KR 2", "description": "Описание 2"},
        ]
        calls: list[str] = []

        def runner(
            prompt: str,
            item: dict[str, object],
            job_id: int,
            step_name: str,
        ) -> str:
            calls.append(step_name)
            if step_name == "relevance":
                return json.dumps(relevance_payload(1), ensure_ascii=False)
            if step_name == "impact-kr-1":
                return json.dumps(impact_payload("neutral"), ensure_ascii=False)
            raise AssertionError(f"unexpected step {step_name} for prompt {prompt[:80]}")

        result = label_clean_item(clean_item, active_krs, 99, agent_runner=runner)

        self.assertEqual(calls, ["relevance", "impact-kr-1"])
        self.assertEqual([label.kr_id for label in result.labels], [1])
        self.assertEqual(result.relevant_kr_ids, [1])
        self.assertEqual(result.irrelevant_kr_ids, [2])

    def test_document_source_keys_use_only_explicit_source_type(self) -> None:
        keys = get_document_source_keys(
            {
                "source_metadata": {"source_type": "Блог", "source": "dzen"},
                "raw_payload": {},
                "url": "https://www.dzen.ru/a",
                "source": "parsers360",
            }
        )

        self.assertEqual(keys["source"], ("dzen", "parsers360"))
        self.assertEqual(keys["domain"], ("dzen.ru",))
        self.assertEqual(keys["source_type"], ("блог",))

    def test_source_rankings_can_fail_open_without_document_source_type(self) -> None:
        clean_item = {
            "source_metadata": {"source": "akm"},
            "raw_payload": {},
            "url": "https://akm.ru/news",
            "source": "parsers360",
        }
        rankings = [
            {
                "source_kind": "source_type",
                "source_value": "СМИ",
                "source_key": "сми",
                "include": True,
            }
        ]

        with patch.dict(os.environ, {"AGENT_1_LABEL_IGNORE_SOURCE_TYPE_RANKINGS": "true"}):
            result = source_rankings_allow_label(clean_item, rankings)

        self.assertTrue(result["allowed"])
        self.assertEqual(
            result["reason"],
            "ignored_source_type_rankings_without_document_source_type",
        )

    def test_replace_document_kr_labels_does_not_cast_sber_flag_to_jsonb(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def __enter__(self) -> "FakeCursor":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def execute(self, sql: str, params: tuple[object, ...]) -> None:
                self.calls.append((sql, params))

        class FakeConnection:
            def __init__(self) -> None:
                self.cursor_obj = FakeCursor()

            def cursor(self) -> FakeCursor:
                return self.cursor_obj

        conn = FakeConnection()
        label = KrLabel(
            kr_id=7,
            impact="positive",
            signal_strength="direct",
            theme="Тема",
            dashboard_description="Описание.",
            why_for_goal="Связь с целью.",
            evidence=("Сбер улучшил продукт",),
            reasoning_steps=("факт", "влияние", "связь"),
            uncertainty="Не доказан долгосрочный эффект.",
            confidence=0.8,
            prompt1_payload=impact_payload(),
            is_sber_paid_news=1,
            prompt2_payload={"is_sber_paid_news": 1},
            prompt3_payload={"mentions": []},
        )

        replace_document_kr_labels(conn, 10, [label])  # type: ignore[arg-type]

        insert_sql, insert_params = conn.cursor_obj.calls[1]
        self.assertEqual(insert_params[11], 1)
        self.assertNotIn("%s::jsonb,\n                    %s::jsonb,\n                    %s::jsonb,\n                    %s::jsonb", insert_sql)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import unittest

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.extract_semantics_worker import (  # noqa: E402
    build_entities_prompt,
    build_events_prompt,
    extract_semantics,
    parse_agent_payload,
    validate_entities_payload,
    validate_events_payload,
)


class ExtractSemanticsWorkerTests(unittest.TestCase):
    def test_parse_openclaw_payload_text(self) -> None:
        response = {
            "status": "ok",
            "result": {
                "payloads": [
                    {
                        "text": "```json\n"
                        + json.dumps(
                            {
                                "entities": {
                                    "companies": ["Сбер"],
                                    "products": [],
                                    "people": [],
                                    "locations": [],
                                    "technologies": [],
                                }
                            },
                            ensure_ascii=False,
                        )
                        + "\n```"
                    }
                ]
            },
        }
        payload = parse_agent_payload(json.dumps(response, ensure_ascii=False))
        self.assertEqual(payload["entities"]["companies"], ["Сбер"])

    def test_validate_entities_payload_accepts_grounded_values(self) -> None:
        payload = validate_entities_payload(
            {
                "entities": {
                    "companies": ["«Сбер»"],
                    "products": ["СберБанк Онлайн"],
                    "people": [],
                    "locations": ["Крым"],
                    "technologies": [],
                }
            },
            evidence_text="Сбер поможет гражданам в Крыму. Открыть аккредитив теперь можно прямо в СберБанк Онлайн.",
        )

        self.assertEqual(payload["companies"], ["Сбер"])
        self.assertEqual(payload["products"], ["СберБанк Онлайн"])
        self.assertEqual(payload["locations"], ["Крым"])

    def test_validate_events_payload_accepts_grounded_evidence(self) -> None:
        payload = validate_events_payload(
            {
                "events": [
                    {
                        "event_type": "запуск программы",
                        "summary": "Сбер сообщил о новой программе помощи.",
                        "participants": ["Сбер"],
                        "event_time": None,
                        "evidence": ['"Сбер поможет гражданам и бизнесу"'],
                    }
                ]
            },
            evidence_text="Сбер поможет гражданам и бизнесу, пострадавшим в результате ЧС в Крыму.",
        )

        self.assertEqual(payload[0]["evidence"], ["Сбер поможет гражданам и бизнесу"])
        self.assertEqual(payload[0]["participants"], ["Сбер"])

    def test_build_prompts_include_label_context(self) -> None:
        clean_item = {
            "clean_title": "Сбер поможет гражданам",
            "clean_text": "Сбер поможет гражданам и бизнесу в Крыму.",
            "raw_title": "Сбер поможет гражданам",
        }
        labels = [
            {
                "kr_id": 1,
                "impact": "positive",
                "theme": "поддержка",
                "dashboard_description": "Подтверждает поддержку клиентов.",
                "kr_title": "Рост доверия",
            }
        ]

        entities_prompt = build_entities_prompt(clean_item, labels)
        events_prompt = build_events_prompt(clean_item, labels)

        self.assertIn("KR 1: impact=positive", entities_prompt)
        self.assertIn("TITLE: Сбер поможет гражданам", entities_prompt)
        self.assertIn("TEXT: Сбер поможет гражданам и бизнесу в Крыму.", events_prompt)

    def test_extract_semantics_runs_entities_then_events(self) -> None:
        calls: list[str] = []
        clean_item = {
            "id": 10,
            "raw_item_id": 100,
            "clean_title": "Сбер поможет гражданам",
            "clean_text": "Сбер поможет гражданам и бизнесу в Крыму.",
            "raw_title": "Сбер поможет гражданам",
        }
        labels = [
            {
                "kr_id": 1,
                "impact": "positive",
                "theme": "поддержка",
                "dashboard_description": "Подтверждает поддержку клиентов.",
                "kr_title": "Рост доверия",
            }
        ]

        def runner(
            prompt: str,
            item: dict[str, object],
            job_id: int,
            step_name: str,
        ) -> str:
            self.assertEqual(item["id"], 10)
            self.assertEqual(job_id, 99)
            calls.append(step_name)
            if step_name == "entities":
                return json.dumps(
                    {
                        "entities": {
                            "companies": ["Сбер"],
                            "products": [],
                            "people": [],
                            "locations": ["Крыму"],
                            "technologies": [],
                        }
                    },
                    ensure_ascii=False,
                )
            if step_name == "events":
                return json.dumps(
                    {
                        "events": [
                            {
                                "event_type": "поддержка",
                                "summary": "Сбер поможет пострадавшим.",
                                "participants": ["Сбер"],
                                "event_time": None,
                                "evidence": ["Сбер поможет гражданам и бизнесу"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected step {step_name}: {prompt[:80]}")

        result = extract_semantics(
            clean_item,
            labels,
            99,
            agent_runner=runner,
            conn=None,
            resume=False,
        )

        self.assertEqual(calls, ["entities", "events"])
        self.assertEqual(result.entities["companies"], ["Сбер"])
        self.assertEqual(result.events[0]["participants"], ["Сбер"])


if __name__ == "__main__":
    unittest.main()

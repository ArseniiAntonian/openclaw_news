from __future__ import annotations

import os
import unittest

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.kr_enrichment_sync import (  # noqa: E402
    build_goal_text,
    parse_agent_2_payload,
    validate_enrichment_payload,
)


class KrEnrichmentSyncTests(unittest.TestCase):
    def test_build_goal_text_joins_title_and_description(self) -> None:
        text = build_goal_text({"title": "Рост доверия", "description": "Увеличить метрику на 10%."})
        self.assertEqual(text, "Рост доверия\n\nУвеличить метрику на 10%.")

    def test_validate_enrichment_payload_normalizes_source_types(self) -> None:
        payload = validate_enrichment_payload(
            {
                "тема": "лояльность",
                "ключевые_слова": ["NPS", "лояльность", "NPS"],
                "типы_источников": [
                    {"тип": "СМИ", "важность": "3", "причина": "Новости о рынке."},
                    {"тип": "Отзывы", "важность": 2, "причина": "Там виден клиентский опыт."},
                ],
            }
        )

        self.assertEqual(payload["тема"], "лояльность")
        self.assertEqual(payload["ключевые_слова"], ["NPS", "лояльность"])
        self.assertEqual(payload["типы_источников"][0]["важность"], 3)
        self.assertEqual(payload["типы_источников"][1]["тип"], "Отзывы")

    def test_parse_agent_2_payload_reads_openclaw_wrapper(self) -> None:
        payload = parse_agent_2_payload(
            '{"result":{"payloads":[{"text":"{\\"тема\\":\\"лояльность\\",\\"ключевые_слова\\":[\\"NPS\\"],\\"типы_источников\\":[{\\"тип\\":\\"СМИ\\",\\"важность\\":\\"3\\",\\"причина\\":\\"Новости рынка.\\"}]}"}]}}'
        )

        self.assertEqual(payload["тема"], "лояльность")


if __name__ == "__main__":
    unittest.main()

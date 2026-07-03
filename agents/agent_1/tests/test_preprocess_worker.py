from __future__ import annotations

import os
import unittest

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.preprocess_worker import (  # noqa: E402
    MINHASH_BANDS,
    MINHASH_SIZE,
    build_dedup_document_text,
    build_lsh_band_keys,
    build_minhash_signature,
    classify_junk_topic,
    detect_language,
    extract_text_value,
    has_protected_business_context,
    normalize_text,
    normalize_dedup_text,
    preprocess_text,
    should_filter_non_russian_text,
    signature_similarity,
)


class PreprocessWorkerTests(unittest.TestCase):
    def test_normalize_text_strips_html_and_whitespace(self) -> None:
        raw = "<div>Hello&nbsp;<b>world</b></div>\n\n<p> Again </p>"
        self.assertEqual(normalize_text(raw), "Hello world\n\nAgain")

    def test_normalize_text_strips_english_boilerplate_blocks(self) -> None:
        raw = (
            "Your browser does not support the video tag. "
            "Most of England is expected to experience temperatures above 30C as soon as Tuesday"
            "Don't MissNews Neighbour from hell smacks homeowner with rounders bat after she told her "
            "'you've got rats' Most ReadFootball Another teaser"
        )
        self.assertEqual(
            normalize_text(raw),
            "Most of England is expected to experience temperatures above 30C as soon as Tuesday",
        )

    def test_extract_text_value_prefers_nested_content(self) -> None:
        payload = {
            "meta": {"ignored": "x"},
            "article": {"content": "<p>Body</p>"},
        }
        self.assertEqual(extract_text_value(payload), "<p>Body</p>")

    def test_detect_language_handles_russian_and_english(self) -> None:
        self.assertEqual(detect_language("Privet mir"), "en")
        self.assertEqual(detect_language("Привет мир"), "ru")
        self.assertEqual(
            detect_language("Это русская новость про AI, NVIDIA и OpenAI, но основной текст на русском языке."),
            "ru",
        )

    def test_should_filter_non_russian_text_rejects_english_news(self) -> None:
        english_text = (
            "Most of England is expected to experience temperatures above 30C as soon "
            "as Tuesday after a brief respite from record-breaking temperatures."
        )
        self.assertTrue(
            should_filter_non_russian_text(
                english_text,
                detect_language(english_text),
            )
        )

    def test_should_filter_non_russian_text_keeps_russian_news(self) -> None:
        russian_text = (
            "Компания объявила о запуске новой производственной линии в Казани и "
            "рассказала о росте экспорта по итогам квартала."
        )
        self.assertFalse(
            should_filter_non_russian_text(
                russian_text,
                detect_language(russian_text),
            )
        )

    def test_classify_junk_topic_filters_generic_weather_news(self) -> None:
        result = classify_junk_topic(
            clean_title="Синоптики предупредили о ливнях и грозах",
            clean_text=(
                "Синоптики предупредили о ливнях и грозах. "
                "По данным метеорологов, сильный дождь и ветер сохранятся до вечера."
            ),
            document_type="news",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "weather")

    def test_classify_junk_topic_filters_generic_fraud_news(self) -> None:
        result = classify_junk_topic(
            clean_title="Мошенники обманули пенсионера на крупную сумму",
            clean_text=(
                "Мошенники обманули пенсионера на крупную сумму. "
                "Полиция возбудила уголовное дело по факту мошенничества."
            ),
            document_type="news",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "crime_and_fraud")

    def test_classify_junk_topic_skips_when_business_context_is_present(self) -> None:
        result = classify_junk_topic(
            clean_title="Сбер предупредил бизнес-клиентов о рисках во время магнитной бури",
            clean_text=(
                "Сбер предупредил бизнес-клиентов о рисках во время магнитной бури "
                "и рассказал, как защитить платежную инфраструктуру."
            ),
            document_type="news",
        )

        self.assertIsNone(result)

    def test_classify_junk_topic_skips_banking_fraud_context(self) -> None:
        result = classify_junk_topic(
            clean_title="Сбер раскрыл новую мошенническую схему против клиентов",
            clean_text=(
                "Сбер раскрыл новую мошенническую схему против клиентов и "
                "обновил антифрод-защиту платежей."
            ),
            document_type="news",
        )

        self.assertIsNone(result)

    def test_classify_junk_topic_does_not_filter_mobile_banking_story(self) -> None:
        result = classify_junk_topic(
            clean_title="Сбер обновил приложение на Android для корпоративных клиентов",
            clean_text=(
                "Сбер обновил мобильное приложение на Android и добавил новые "
                "сценарии для корпоративных клиентов и платежей."
            ),
            document_type="news",
        )

        self.assertIsNone(result)

    def test_classify_junk_topic_filters_generic_sports_news(self) -> None:
        result = classify_junk_topic(
            clean_title="Хоккейный матч завершился победой хозяев",
            clean_text=(
                "Хоккейный матч завершился победой хозяев. "
                "Тренер отметил, что команда уверенно провела турнир."
            ),
            document_type="news",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "sports")

    def test_classify_junk_topic_filters_generic_transport_news(self) -> None:
        result = classify_junk_topic(
            clean_title="В аэропорту задержали авиарейс из-за непогоды",
            clean_text=(
                "В аэропорту задержали авиарейс из-за непогоды. "
                "Пассажиры ждут самолет в терминале."
            ),
            document_type="news",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "transport_and_airport")

    def test_has_protected_business_context_detects_banking_terms(self) -> None:
        self.assertTrue(
            has_protected_business_context(
                "Сбер запустил сервис для юрлиц",
                "Новый банковский сервис ускоряет платежи клиентов.",
            )
        )

    def test_normalize_dedup_text_removes_punctuation_and_maps_yo(self) -> None:
        self.assertEqual(
            normalize_dedup_text("Съёмка: Сбер, Ёлка & Growth!!!"),
            "съемка сбер елка growth",
        )

    def test_build_dedup_document_text_uses_title_summary_and_content(self) -> None:
        self.assertEqual(
            build_dedup_document_text(
                clean_title="Заголовок",
                summary="<p>Короткое резюме</p>",
                clean_text="Полный текст новости.",
            ),
            "заголовок короткое резюме полный текст новости",
        )

    def test_minhash_similarity_is_high_for_close_reprints(self) -> None:
        left = build_minhash_signature(
            normalize_dedup_text(
                "Компания открыла новый завод в Казани и объявила о запуске линии."
            )
        )
        right = build_minhash_signature(
            normalize_dedup_text(
                "Компания открыла новый завод в Казани и объявила о запуске линии сегодня."
            )
        )
        self.assertEqual(len(left), MINHASH_SIZE)
        self.assertEqual(len(right), MINHASH_SIZE)
        self.assertGreaterEqual(signature_similarity(left, right), 0.7)

    def test_lsh_band_keys_use_document_configuration(self) -> None:
        band_keys = build_lsh_band_keys(tuple(range(MINHASH_SIZE)))
        self.assertEqual(len(band_keys), MINHASH_BANDS)
        self.assertIn((0, (0, 1, 2, 3)), band_keys)
        self.assertIn((MINHASH_BANDS - 1, (124, 125, 126, 127)), band_keys)

    def test_preprocess_text_uses_payload_when_raw_text_missing(self) -> None:
        title, clean_text, language = preprocess_text(
            {
                "title": "<b>Title</b>",
                "raw_text": "",
                "raw_payload": {"content": "<p>Document body</p>"},
            }
        )
        self.assertEqual(title, "Title")
        self.assertEqual(clean_text, "Document body")
        self.assertEqual(language, "en")


if __name__ == "__main__":
    unittest.main()

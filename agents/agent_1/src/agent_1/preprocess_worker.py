from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
from psycopg.rows import dict_row


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_LOG_FILE = ENV_PATH.parent / "logs" / "preprocess_worker.log"
DEFAULT_BATCH_SIZE = 10
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.7
DEFAULT_DEDUP_CONTENT_LIMIT = 4_000
DEFAULT_MIN_TOKEN_COUNT = 8
DEFAULT_MIN_LANGUAGE_ALPHA_CHARS = 20
DEFAULT_MIN_RUSSIAN_LETTER_RATIO = 0.55
# MinHash tuning.
# MINHASH_SIZE number of permutations (bigger -> less estimation noise).
# MINHASH_BANDS * ROWS MUST equal MINHASH_SIZE, otherwise LSH banding is disabled.
# LSH candidate threshold ~= (1 / MINHASH_BANDS) ** (1 / MINHASH_ROWS_PER_BAND)
# With 32 bands / 4 rows the candidate threshold is ~0.42, i.e. every pair that is
# >= ~0.42 similar becomes a candidate, and the exact Jaccard check below decides.
MINHASH_SIZE = 128
MINHASH_SHINGLE_SIZE = 5
MINHASH_CHAR_SHINGLE_SIZE = 17
MINHASH_BANDS = 32
MINHASH_ROWS_PER_BAND = 4
MAX_HASH = (1 << 64) - 1
MERSENNE_PRIME = (1 << 61) - 1
BandKey = tuple[int, tuple[int, ...]]

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCK_TAG_RE = re.compile(r"</?(br|p|div|article|section|li|ul|ol|h[1-6]|tr|td|th)\b[^>]*>", re.IGNORECASE)
WORD_RE = re.compile(r"\w+", re.UNICODE)
DEDUP_TOKEN_RE = re.compile(r"[A-Za-z\u0400-\u04FF0-9]+", re.IGNORECASE)
INLINE_TOKEN_BOUNDARY_RE = re.compile(
    r"(?:(?<=[a-z\u0430-\u044f\u0451])(?=[A-Z\u0400-\u042F\u0401])|(?<=\d)(?=[A-Z\u0400-\u042F\u0401][a-z\u0430-\u044f\u0451]))"
)
LEADING_BOILERPLATE_RE = re.compile(
    r"(?:^|\n)\s*Your browser does not support the video tag\.?\s*",
    re.IGNORECASE,
)
TRAILING_BOILERPLATE_MARKERS = (
    re.compile(r"\bDon't Miss\b", re.IGNORECASE),
    re.compile(r"\bMost Read\b", re.IGNORECASE),
    re.compile(r"\bRead Next\b", re.IGNORECASE),
    re.compile(r"\bRelated Articles\b", re.IGNORECASE),
    re.compile(r"\bRecommended\b", re.IGNORECASE),
)
MIN_CONTENT_CHARS_BEFORE_BOILERPLATE_TRIM = 20
JUNK_REGEX_MIN_BODY_HITS = 2
JUNK_REGEX_MAX_MATCHES = 5


def compile_pattern(parts: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


# Guard rails: if a story explicitly contains banking / Sber / business-product
# context, do not drop it via the lightweight regex junk filter even when it
# also mentions a generic noisy topic like weather, holidays or celebrities.
PROTECTED_BUSINESS_CONTEXT_RE = compile_pattern(
    [
        r"сбер\w*",
        r"сбербанк\w*",
        r"банк\w*",
        r"банковск\w*",
        r"финанс\w*",
        r"финтех\w*",
        r"кредит\w*",
        r"ипотек\w*",
        r"вклад\w*",
        r"депозит\w*",
        r"лизинг\w*",
        r"страхован\w*",
        r"инвест\w*",
        r"облигаци\w*",
        r"акци\w*",
        r"дивиденд\w*",
        r"эквайр\w*",
        r"плат[её]ж\w*",
        r"оплат\w*",
        r"перевод\w*",
        r"расчетн\w*",
        r"расчетн\w*\s+счет\w*",
        r"банковск\w*\s+карт\w*",
        r"дебетов\w*\s+карт\w*",
        r"кредитн\w*\s+карт\w*",
        r"банкомат\w*",
        r"клиент\w*",
        r"юрлиц\w*",
        r"юрлицо\w*",
        r"мал\w*\s+бизнес\w*",
        r"средн\w*\s+бизнес\w*",
        r"корпоратив\w*",
        r"зарплат\w*",
        r"работодател\w*",
        r"genai",
        r"giga\s*chat\w*",
        r"gigachat\w*",
        r"искусственн\w*\s+интеллект\w*",
        r"\bии\b",
        r"\bai\b",
        r"llm\w*",
        r"нейросет\w*",
        r"экосистем\w*",
        r"мобильн\w*\s+приложени\w*",
        r"платежн\w*\s+терминал\w*",
        r"pos[-\s]?терминал\w*",
        r"кибербезопасн\w*",
        r"антифрод\w*",
        r"мошеннич\w*\s+схем\w*",
    ]
)

# Intentionally conservative subset of the provided regex buckets. We keep only
# categories that are usually generic human-interest noise. Crime/fraud, sports
# and transport are enabled too, but still guarded by the business-context
# allowlist above. We do NOT activate higher-risk buckets like crypto,
# consumer tech, medical, utilities or war/geopolitics because they can hide
# Sber-relevant business signals too often.
JUNK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "weather",
        compile_pattern(
            [
                r"погод\w*",
                r"синоптик\w*",
                r"гидромет\w*",
                r"метеоролог\w*",
                r"прогноз\w*\s+погод\w*",
                r"штормов\w*\s+предупрежден\w*",
                r"аномальн\w*\s+жар\w*",
                r"аномальн\w*\s+холод\w*",
                r"температурн\w*\s+качел\w*",
                r"мороз\w*",
                r"заморозк\w*",
                r"снег\w*",
                r"снегопад\w*",
                r"метел\w*",
                r"вьюг\w*",
                r"гололед\w*",
                r"гололедиц\w*",
                r"ливн\w*",
                r"дожд\w*",
                r"гроза\w*",
                r"осадк\w*",
                r"ветер\w*",
                r"потеплен\w*",
                r"похолодан\w*",
            ]
        ),
    ),
    (
        "winter_holiday_noise",
        compile_pattern(
            [
                r"елк\w*",
                r"ель\b",
                r"пихт\w*",
                r"нов\w*\s+год\w*",
                r"новогодн\w*",
                r"рождеств\w*",
                r"дед\s+мороз",
                r"снегурочк\w*",
                r"утренник\w*",
            ]
        ),
    ),
    (
        "traffic_and_pdd",
        compile_pattern(
            [
                r"пдд",
                r"гибдд",
                r"гаи",
                r"дпс",
                r"дорожн\w*\s+полици\w*",
                r"правил\w*\s+дорожн\w*\s+движени\w*",
                r"водител\w*",
                r"пешеход\w*",
                r"автоинспектор\w*",
                r"лишени\w*\s+прав",
                r"штраф\w*\s+за\s+пдд",
            ]
        ),
    ),
    (
        "school_incidents",
        compile_pattern(
            [
                r"школ\w*\s+эвакуаци\w*",
                r"чп\s+в\s+школ\w*",
                r"происшеств\w*\s+в\s+школ\w*",
                r"стрельб\w*\s+в\s+школ\w*",
                r"нападени\w*\s+в\s+школ\w*",
                r"драк\w*\s+в\s+школ\w*",
                r"буллинг\w*",
                r"школьник\w*\s+напал\w*",
                r"ученик\w*\s+напал\w*",
                r"школьник\w*\s+пострадал\w*",
            ]
        ),
    ),
    (
        "crime_and_fraud",
        compile_pattern(
            [
                r"мошенник\w*",
                r"мошенничеств\w*",
                r"обманул\w*",
                r"украл\w*",
                r"краж\w*",
                r"ограблен\w*",
                r"грабеж\w*",
                r"разбо\w*",
                r"убийств\w*",
                r"убил\w*",
                r"зарезал\w*",
                r"застрелил\w*",
                r"стрельб\w*",
                r"теракт\w*",
                r"нападени\w*",
                r"похищени\w*",
                r"насили\w*",
                r"маньяк\w*",
                r"уголовн\w*\s+дел\w*",
                r"задержал\w*\s+мошенник\w*",
            ]
        ),
    ),
    (
        "animals",
        compile_pattern(
            [
                r"кошк\w*",
                r"кот(?:а|у|ом|ы|ов)?",
                r"котен\w*",
                r"собак\w*",
                r"пес(?:а|у|ом|ы|ов)?",
                r"щен\w*",
                r"тигр\w*",
                r"медвед\w*",
                r"волк\w*",
                r"лиса\w*",
                r"дельфин\w*",
                r"обезьян\w*",
                r"зоопарк\w*",
                r"животн\w*",
                r"ветеринар\w*",
                r"растен\w*",
            ]
        ),
    ),
    (
        "family_and_newborns",
        compile_pattern(
            [
                r"новорожден\w*",
                r"младен\w*",
                r"родил\w*",
                r"родила",
                r"беремен\w*",
                r"рожениц\w*",
                r"малыш\w*",
            ]
        ),
    ),
    (
        "health_and_sleep",
        compile_pattern(
            [
                r"сон\b",
                r"сна\b",
                r"сном\b",
                r"сне\b",
                r"бессонниц\w*",
                r"выспат\w*",
                r"снотворн\w*",
                r"сомнолог\w*",
                r"храп\w*",
                r"подушк\w*",
                r"матрас\w*",
                r"ортопедическ\w*\s+матрас\w*",
                r"здоров\w*\s+сон\w*",
                r"режим\w*\s+сна",
            ]
        ),
    ),
    (
        "beauty_and_fashion",
        compile_pattern(
            [
                r"мода\w*",
                r"модн\w*",
                r"стил\w*",
                r"гардероб\w*",
                r"одежд\w*",
                r"обув\w*",
                r"аксессуар\w*",
                r"макияж\w*",
                r"косметолог\w*",
                r"прическ\w*",
                r"парикмахер\w*",
                r"ногт\w*",
                r"маникюр\w*",
                r"педикюр\w*",
                r"уход\w*\s+за\s+кож\w*",
            ]
        ),
    ),
    (
        "lifestyle_and_food",
        compile_pattern(
            [
                r"рецепт\w*",
                r"салат\w*",
                r"суп\w*",
                r"борщ\w*",
                r"котлет\w*",
                r"пирог\w*",
                r"блин\w*",
                r"оливье",
                r"кулич\w*",
                r"шашлык\w*",
                r"ингредиент\w*",
                r"кулинар\w*",
                r"диет\w*",
                r"похуден\w*",
                r"витамин\w*",
                r"космет\w*",
                r"морщин\w*",
                r"маникюр\w*",
                r"педикюр\w*",
                r"гороскоп\w*",
                r"астролог\w*",
                r"таро",
                r"знак\w*\s+зодиак\w*",
                r"ретроградн\w*\s+меркур\w*",
                r"магнитн\w*\s+бур\w*",
            ]
        ),
    ),
    (
        "food_and_meals",
        compile_pattern(
            [
                r"морепродукт\w*",
                r"завтрак\w*",
                r"обед\w*",
                r"ужин\w*",
                r"десерт\w*",
                r"напит\w*",
            ]
        ),
    ),
    (
        "religion_and_obituaries",
        compile_pattern(
            [
                r"скончал\w*",
                r"умер\w*",
                r"умерла",
                r"уш[её]л\s+из\s+жизни",
                r"покинул\w*\s+этот\s+мир",
                r"похорон\w*",
                r"некролог\w*",
                r"иерей\w*",
                r"священник\w*",
                r"митрополит\w*",
                r"епархи\w*",
                r"храм\w*",
                r"церков\w*",
                r"литурги\w*",
                r"молебен\w*",
                r"благочини\w*",
            ]
        ),
    ),
    (
        "sports",
        compile_pattern(
            [
                r"футбол\w*",
                r"хокке\w*",
                r"матч\w*",
                r"чемпионат\w*",
                r"кхл",
                r"нхл",
                r"ufc",
                r"mma",
                r"турнир\w*",
                r"кубок\w*",
                r"спортсмен\w*",
                r"спортивн\w*",
                r"тренер\w*",
                r"гол\w*",
                r"олимпиад\w*",
                r"фигурн\w*\s+катан\w*",
                r"каток\w*",
                r"шахмат\w*",
            ]
        ),
    ),
    (
        "celebrities_and_gossip",
        compile_pattern(
            [
                r"блогер\w*",
                r"шоумен\w*",
                r"телеведущ\w*",
                r"знаменитост\w*",
                r"селебрити",
                r"эпштейн\w*",
                r"роман\w*\s+со\s+звезд\w*",
                r"развел\w*",
                r"свадьб\w*\s+звезд\w*",
                r"скандал\w*\s+со\s+звезд\w*",
                r"личн\w*\s+жизн\w*\s+звезд\w*",
            ]
        ),
    ),
    (
        "transport_and_airport",
        compile_pattern(
            [
                r"аэропорт\w*",
                r"троллейбус\w*",
                r"лавин\w*",
                r"самолет\w*",
                r"авиарейс\w*",
            ]
        ),
    ),
    (
        "gardening_and_hobby",
        compile_pattern(
            [
                r"дач\w*",
                r"огород\w*",
                r"рассад\w*",
                r"урожа\w*",
                r"садовод\w*",
                r"садоводств\w*",
                r"клумб\w*",
                r"цветочн\w*",
                r"комнатн\w*\s+растени\w*",
                r"рыбалк\w*",
                r"рыбак\w*",
                r"охот\w*",
                r"грибн\w*",
                r"ягод\w*",
            ]
        ),
    ),
    (
        "missing_persons_and_searches",
        compile_pattern(
            [
                r"пропал\w*",
                r"пропавш\w*",
                r"без\s+вести",
                r"разыскива\w*",
                r"поиск\w*\s+ребенк\w*",
                r"поиск\w*\s+мужчин\w*",
                r"поиск\w*\s+женщин\w*",
                r"волонтер\w*\s+ищут",
                r"найден\w*\s+жив",
            ]
        ),
    ),
]


@dataclass
class NearDuplicateEntry:
    clean_item_id: int
    raw_item_id: int
    # Key for EXACT dedup: normalized title + FULL clean_text. Deliberately
    # excludes source_metadata's summary and the near-dup content limit, so two
    # byte-identical documents always collide here even when their source
    # summaries differ or are missing.
    exact_key: str
    # Document used for NEAR dedup (title + summary + truncated text).
    normalized_text: str
    signature: tuple[int, ...]
    band_keys: frozenset[BandKey]


@dataclass
class NearDuplicateCache:
    entries: list[NearDuplicateEntry]
    bands: dict[BandKey, list[NearDuplicateEntry]]
    exact_text_index: dict[str, NearDuplicateEntry]


near_duplicate_signature_cache: NearDuplicateCache | None = None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_line(level: str, message: str, *, stderr: bool = False) -> None:
    line = f"{utc_now_text()} {level} {message}"
    print(line, file=sys.stderr if stderr else sys.stdout)

    log_file = Path(LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process agent_1 preprocess jobs from PostgreSQL queue."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one claimed batch and exit.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"How many pending preprocess jobs to claim at once. Default: {DEFAULT_BATCH_SIZE}.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            "How many seconds to sleep when no jobs are available. "
            f"Default: {DEFAULT_POLL_INTERVAL_SECONDS}."
        ),
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Optional upper bound on processed jobs before exit.",
    )
    parser.add_argument(
        "--near-duplicate-threshold",
        type=float,
        default=DEFAULT_NEAR_DUPLICATE_THRESHOLD,
        help=(
            "Final shingle-set (Jaccard) similarity threshold for news-like near-duplicate filtering. "
            f"Default: {DEFAULT_NEAR_DUPLICATE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("AGENT_1_PREPROCESS_LOG_FILE", str(DEFAULT_LOG_FILE)),
        help="Optional log file path.",
    )
    return parser.parse_args(argv)


def clean_html(text: str) -> str:
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = COMMENT_RE.sub(" ", text)
    text = BLOCK_TAG_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    return html.unescape(text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def strip_boilerplate(text: str) -> str:
    if not text:
        return text

    text = INLINE_TOKEN_BOUNDARY_RE.sub(" ", text)
    text = LEADING_BOILERPLATE_RE.sub(" ", text)

    trim_from: int | None = None
    for marker in TRAILING_BOILERPLATE_MARKERS:
        match = marker.search(text)
        if match is None:
            continue
        if match.start() < MIN_CONTENT_CHARS_BEFORE_BOILERPLATE_TRIM:
            continue
        if trim_from is None or match.start() < trim_from:
            trim_from = match.start()

    if trim_from is not None:
        text = text[:trim_from]

    return normalize_whitespace(text)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = clean_html(text)
    text = normalize_whitespace(text)
    text = strip_boilerplate(text)
    return text.strip()


def normalize_title(text: str | None) -> str | None:
    if not text:
        return None
    normalized = normalize_text(text)
    return normalized or None


def normalize_dedup_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    return " ".join(DEDUP_TOKEN_RE.findall(normalized))


def count_script_letters(text: str) -> tuple[int, int]:
    cyrillic = 0
    latin = 0
    for char in text:
        lower_char = char.lower()
        if "а" <= lower_char <= "я" or lower_char == "ё":
            cyrillic += 1
        elif "a" <= lower_char <= "z":
            latin += 1
    return cyrillic, latin


def detect_language(text: str) -> str | None:
    cyrillic, latin = count_script_letters(text)
    total = cyrillic + latin

    if total == 0:
        return None
    if latin == 0:
        return "ru"
    if cyrillic == 0:
        return "en"

    if total < DEFAULT_MIN_LANGUAGE_ALPHA_CHARS:
        if cyrillic >= latin:
            return "ru"
        return "en"

    if cyrillic / total >= DEFAULT_MIN_RUSSIAN_LETTER_RATIO:
        return "ru"
    return "en"


def should_filter_non_russian_text(text: str, language: str | None) -> bool:
    cyrillic, latin = count_script_letters(text)
    total = cyrillic + latin

    if not text.strip():
        return False
    if total < DEFAULT_MIN_LANGUAGE_ALPHA_CHARS:
        return False

    return language != "ru"


def extract_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [extract_text_value(item) for item in value]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(value, dict):
        preferred_keys = (
            "content",
            "raw_text",
            "text",
            "body",
            "article",
            "description",
            "summary",
        )
        for key in preferred_keys:
            extracted = extract_text_value(value.get(key))
            if extracted.strip():
                return extracted

        parts = [extract_text_value(item) for item in value.values()]
        return "\n".join(part for part in parts if part.strip())

    return ""


def build_dedup_document_text(
    *,
    clean_title: str | None,
    clean_text: str,
    summary: Any = None,
    content_limit: int = DEFAULT_DEDUP_CONTENT_LIMIT,
) -> str:
    parts: list[str] = []
    if clean_title:
        parts.append(clean_title)

    summary_text = normalize_text(extract_text_value(summary))
    if summary_text:
        parts.append(summary_text)

    if clean_text:
        parts.append(clean_text[:content_limit])

    return normalize_dedup_text(" ".join(parts))


def build_exact_dedup_key(clean_title: str | None, clean_text: str) -> str:
    """Key for exact (one-to-one) duplicate detection.

    Uses only the document's own content (title + full text). The source
    summary is intentionally NOT part of this key: two identical articles
    coming from different feeds often carry different summaries in
    source_metadata, and including the summary made such one-to-one
    duplicates invisible to the exact check.
    """
    parts: list[str] = []
    if clean_title:
        parts.append(clean_title)
    if clean_text:
        parts.append(clean_text)
    return normalize_dedup_text(" ".join(parts))


def build_word_shingles(
    clean_text: str, *, shingle_size: int = MINHASH_SHINGLE_SIZE
) -> frozenset[str]:
    tokens = WORD_RE.findall(clean_text.casefold())
    if not tokens:
        return frozenset()

    if len(tokens) < shingle_size:
        return frozenset({" ".join(tokens)})

    return frozenset(
        " ".join(tokens[index : index + shingle_size])
        for index in range(len(tokens) - shingle_size + 1)
    )


def hash64(value: str) -> int:
    # 64-bit deterministic hash. Used only for deriving the MinHash mask/
    # multiplier parameters (256 calls at import), where the full 64-bit width
    # matters. NOT used on the hot shingle path -- see hash_shingle.
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def hash_shingle(value: str) -> int:
    # Hot-path shingle hash. crc32 is ~2x faster than blake2b (C, no per-call
    # object) and was the dominant cost of build_shingles / the startup dedup
    # cache load (rework-agent-1-v5 3.7 re-measure: hash64 = 13.9s of a 23.8s
    # cache load). 32-bit is fine here: the MinHash mixing scheme lifts these
    # to 64-bit via the masks/multipliers, and collision risk at ~2000
    # shingles/doc is negligible (~5e-4). Deterministic across runs/workers.
    return zlib.crc32(value.encode("utf-8"))


def build_shingles(
    normalized_text: str,
    *,
    shingle_size: int = MINHASH_SHINGLE_SIZE,
    char_shingle_size: int = MINHASH_CHAR_SHINGLE_SIZE,
) -> frozenset[int]:
    tokens = normalized_text.split()
    shingles: set[int] = set()

    if tokens:
        if len(tokens) < shingle_size:
            shingles.add(hash_shingle(f"w:{normalized_text}"))
        else:
            for index in range(len(tokens) - shingle_size + 1):
                shingle = " ".join(tokens[index : index + shingle_size])
                shingles.add(hash_shingle(f"w:{shingle}"))

    if char_shingle_size > 0 and normalized_text:
        compact_text = re.sub(r"\s+", " ", normalized_text).strip()
        if len(compact_text) <= char_shingle_size:
            shingles.add(hash_shingle(f"c:{compact_text}"))
        else:
            for index in range(len(compact_text) - char_shingle_size + 1):
                shingles.add(hash_shingle(f"c:{compact_text[index:index + char_shingle_size]}"))

    return frozenset(shingles)


# MinHash mixing parameters for the (x XOR mask_i) * mult_i scheme.
#
# This replaced the earlier (a*x + b) mod (2**61-1) scheme (rework-agent-1-v5
# task 3.3). The old formula could not be vectorized: a*x overflows uint64 for
# 61-bit operands, forcing numpy into object-dtype (Python-speed) arithmetic, so
# the per-doc signature stayed a nested Python loop measured at ~150 ms/doc on
# the real corpus (the dominant cost of the whole worker; see design.md D5).
# This scheme uses natural uint64 wraparound so the 128-permutation min-hash is a
# single numpy broadcast, dropping the signature to a few ms/doc.
#
# The parameters MUST be identical across every worker and every run, otherwise
# signatures computed by different processes are incomparable. They are derived
# deterministically from fixed seeds (hash64 of a fixed string), never from an
# unseeded RNG. Multipliers are forced odd so `* mult` is a bijection on uint64
# (no shingle information collapses to 0).
def build_minhash_mixers(
    signature_size: int = MINHASH_SIZE,
) -> tuple[np.ndarray, np.ndarray]:
    masks = np.empty(signature_size, dtype=np.uint64)
    mults = np.empty(signature_size, dtype=np.uint64)
    for index in range(signature_size):
        masks[index] = np.uint64(hash64(f"mask-{index}"))
        mults[index] = np.uint64(hash64(f"mult-{index}") | 1)  # force odd
    return masks, mults


MINHASH_MASKS, MINHASH_MULTIPLIERS = build_minhash_mixers()


def build_minhash_signature_from_shingles(
    shingles: frozenset[int],
    *,
    masks: np.ndarray = MINHASH_MASKS,
    mults: np.ndarray = MINHASH_MULTIPLIERS,
) -> tuple[int, ...]:
    if not shingles:
        return tuple(MAX_HASH for _ in range(len(masks)))

    # (N,) shingle hashes -> (128, N) mixed values -> per-permutation column min.
    # uint64 multiply wraps mod 2**64 natively; the high bits (which dominate the
    # min ordering) are well mixed by the odd multiply.
    s = np.fromiter(shingles, dtype=np.uint64, count=len(shingles))
    mixed = (s[np.newaxis, :] ^ masks[:, np.newaxis]) * mults[:, np.newaxis]
    signature = mixed.min(axis=1)
    return tuple(int(value) for value in signature)


def build_minhash_signature(
    normalized_text: str,
    *,
    shingle_size: int = MINHASH_SHINGLE_SIZE,
    char_shingle_size: int = MINHASH_CHAR_SHINGLE_SIZE,
) -> tuple[int, ...]:
    shingles = build_shingles(
        normalized_text,
        shingle_size=shingle_size,
        char_shingle_size=char_shingle_size,
    )
    return build_minhash_signature_from_shingles(
        shingles,
    )


def signature_similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    if not left or not right:
        return 0.0

    size = min(len(left), len(right))
    matches = sum(1 for index in range(size) if left[index] == right[index])
    return matches / size


def jaccard_similarity(left: frozenset[Any], right: frozenset[Any]) -> float:
    if not left or not right:
        return 0.0

    return len(left & right) / len(left | right)


def build_lsh_band_keys(
    signature: tuple[int, ...],
    *,
    bands: int = MINHASH_BANDS,
    rows_per_band: int = MINHASH_ROWS_PER_BAND,
) -> frozenset[BandKey]:
    required_size = bands * rows_per_band
    if len(signature) < required_size:
        return frozenset()

    return frozenset(
        (
            band_index,
            signature[
                band_index * rows_per_band : (band_index + 1) * rows_per_band
            ],
        )
        for band_index in range(bands)
    )


def build_near_duplicate_entry(
    *,
    clean_item_id: int,
    raw_item_id: int,
    clean_title: str | None,
    clean_text: str,
    summary: Any = None,
    is_news: bool = True,
) -> NearDuplicateEntry | None:
    exact_key = build_exact_dedup_key(clean_title, clean_text)
    normalized_text = build_dedup_document_text(
        clean_title=clean_title,
        clean_text=clean_text,
        summary=summary,
    )
    if not exact_key and not normalized_text:
        return None

    # Every document (any type) is indexed for EXACT dedup by its exact_key.
    # Only news documents get a MinHash signature + LSH band keys for NEAR dedup.
    signature: tuple[int, ...] = tuple()
    band_keys: frozenset[BandKey] = frozenset()
    if is_news and len(normalized_text.split()) >= DEFAULT_MIN_TOKEN_COUNT:
        signature = build_minhash_signature(normalized_text)
        band_keys = build_lsh_band_keys(signature)

    return NearDuplicateEntry(
        clean_item_id=clean_item_id,
        raw_item_id=raw_item_id,
        exact_key=exact_key,
        normalized_text=normalized_text,
        signature=signature,
        band_keys=band_keys,
    )


def index_near_duplicate_entry(
    cache: NearDuplicateCache, entry: NearDuplicateEntry
) -> None:
    cache.entries.append(entry)
    if entry.exact_key:
        cache.exact_text_index.setdefault(entry.exact_key, entry)
    for band_key in entry.band_keys:
        cache.bands.setdefault(band_key, []).append(entry)


def load_near_duplicate_signature_cache(
    conn: psycopg.Connection[Any],
) -> NearDuplicateCache:
    global near_duplicate_signature_cache

    if near_duplicate_signature_cache is not None:
        return near_duplicate_signature_cache

    cache = NearDuplicateCache(entries=[], bands={}, exact_text_index={})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                clean.id AS clean_item_id,
                clean.raw_item_id,
                clean.clean_title,
                clean.clean_text,
                raw.document_type,
                raw.source_metadata -> 'summary' AS summary
            FROM agent_1.clean_items AS clean
            JOIN agent_1.raw_items AS raw
              ON raw.id = clean.raw_item_id
            ORDER BY clean.id
            """
        )
        for row in cur.fetchall():
            entry = build_near_duplicate_entry(
                clean_item_id=row["clean_item_id"],
                raw_item_id=row["raw_item_id"],
                clean_title=row["clean_title"],
                clean_text=row["clean_text"],
                summary=row["summary"],
                is_news=(row["document_type"] == "news"),
            )
            if entry is not None:
                index_near_duplicate_entry(cache, entry)

    near_duplicate_signature_cache = cache
    return cache


def add_near_duplicate_signature_cache_entry(
    *,
    clean_item_id: int,
    raw_item_id: int,
    clean_title: str | None,
    clean_text: str,
    summary: Any = None,
    is_news: bool = True,
) -> None:
    if near_duplicate_signature_cache is None:
        return

    entry = build_near_duplicate_entry(
        clean_item_id=clean_item_id,
        raw_item_id=raw_item_id,
        clean_title=clean_title,
        clean_text=clean_text,
        summary=summary,
        is_news=is_news,
    )
    if entry is not None:
        index_near_duplicate_entry(near_duplicate_signature_cache, entry)


def claim_jobs(conn: psycopg.Connection[Any], batch_size: int) -> list[tuple[int, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH next_jobs AS (
                SELECT id
                FROM agent_1.processing_jobs
                WHERE job_type = 'preprocess'
                  AND status = 'pending'
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE agent_1.processing_jobs AS jobs
            SET status = 'processing'
            FROM next_jobs
            WHERE jobs.id = next_jobs.id
            RETURNING jobs.id, jobs.entity_id
            """,
            (batch_size,),
        )
        rows = cur.fetchall()
    conn.commit()
    return [(row["id"], row["entity_id"]) for row in rows]


def fetch_raw_item(conn: psycopg.Connection[Any], raw_item_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                source,
                document_type,
                external_id,
                url,
                title,
                raw_text,
                raw_payload,
                source_metadata,
                published_at
            FROM agent_1.raw_items
            WHERE id = %s
            """,
            (raw_item_id,),
        )
        return cur.fetchone()


def fetch_existing_clean_item_id(
    conn: psycopg.Connection[Any], raw_item_id: int
) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM agent_1.clean_items
            WHERE raw_item_id = %s
            """,
            (raw_item_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row["id"]


def find_exact_duplicate(
    conn: psycopg.Connection[Any],
    *,
    clean_title: str | None,
    clean_text: str,
    raw_item_id: int,
    summary: Any = None,
) -> dict[str, Any] | None:
    del summary  # Exact matching is intentionally summary-independent.

    exact_key = build_exact_dedup_key(clean_title, clean_text)
    if not exact_key:
        return None

    cache = load_near_duplicate_signature_cache(conn)
    entry = cache.exact_text_index.get(exact_key)
    if entry is not None and entry.raw_item_id != raw_item_id:
        return {
            "kind": "exact",
            "clean_item_id": entry.clean_item_id,
            "raw_item_id": entry.raw_item_id,
            "similarity": 1.0,
        }

    # Authoritative fallback against the database. The in-memory cache is
    # per-process and loaded once at startup, so it cannot see clean_items
    # inserted by another worker (or any other writer) after startup. Without
    # this check, byte-identical duplicates slip through whenever more than
    # one process feeds clean_items.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id AS clean_item_id, raw_item_id
            FROM agent_1.clean_items
            WHERE clean_text = %s
              AND clean_title IS NOT DISTINCT FROM %s
              AND raw_item_id <> %s
            ORDER BY id
            LIMIT 1
            """,
            (clean_text, clean_title, raw_item_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "kind": "exact",
        "clean_item_id": row["clean_item_id"],
        "raw_item_id": row["raw_item_id"],
        "similarity": 1.0,
    }


def find_near_duplicate(
    conn: psycopg.Connection[Any],
    *,
    clean_title: str | None,
    clean_text: str,
    raw_item_id: int,
    summary: Any = None,
    threshold: float,
) -> dict[str, Any] | None:
    normalized_text = build_dedup_document_text(
        clean_title=clean_title,
        clean_text=clean_text,
        summary=summary,
    )
    if len(normalized_text.split()) < DEFAULT_MIN_TOKEN_COUNT:
        return None

    query_shingles = build_shingles(normalized_text)
    if not query_shingles:
        return None

    signature = build_minhash_signature_from_shingles(query_shingles)
    band_keys = build_lsh_band_keys(signature)
    if not signature or not band_keys:
        return None

    cache = load_near_duplicate_signature_cache(conn)
    candidates: dict[int, NearDuplicateEntry] = {}
    for band_key in band_keys:
        for candidate in cache.bands.get(band_key, []):
            candidates[candidate.clean_item_id] = candidate

    best_match: dict[str, Any] | None = None
    for candidate in candidates.values():
        if candidate.raw_item_id == raw_item_id:
            continue

        if not candidate.signature:
            continue

        # LSH (MinHash) is used only to shortlist candidates. The actual accept/reject
        # decision uses the EXACT Jaccard similarity of the shingle sets, so the outcome
        # is deterministic and free of MinHash estimation noise near the threshold.
        candidate_shingles = build_shingles(candidate.normalized_text)
        exact_similarity = jaccard_similarity(query_shingles, candidate_shingles)
        if exact_similarity < threshold:
            continue

        if best_match is None or exact_similarity > best_match["similarity"]:
            best_match = {
                "kind": "near",
                "clean_item_id": candidate.clean_item_id,
                "raw_item_id": candidate.raw_item_id,
                "similarity": exact_similarity,
                "minhash_similarity": signature_similarity(signature, candidate.signature),
            }

    return best_match


def merge_source_metadata(
    conn: psycopg.Connection[Any], raw_item_id: int, patch: dict[str, Any]
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.raw_items
            SET source_metadata = COALESCE(source_metadata, '{}'::jsonb) || %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(patch, ensure_ascii=False), raw_item_id),
        )


def mark_job_status(conn: psycopg.Connection[Any], job_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_1.processing_jobs
            SET status = %s
            WHERE id = %s
            """,
            (status, job_id),
        )


def insert_clean_item(
    conn: psycopg.Connection[Any],
    *,
    raw_item_id: int,
    clean_title: str | None,
    clean_text: str,
    language: str | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_1.clean_items (
                raw_item_id,
                clean_title,
                clean_text,
                language
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (raw_item_id, clean_title, clean_text, language),
        )
        row = cur.fetchone()
    return row["id"]


def preprocess_text(raw_item: dict[str, Any]) -> tuple[str | None, str, str | None]:
    clean_title = normalize_title(raw_item.get("title"))

    text_source = raw_item.get("raw_text")
    if not text_source:
        text_source = extract_text_value(raw_item.get("raw_payload"))

    clean_text = normalize_text(text_source)
    language = detect_language(clean_text)
    return clean_title, clean_text, language


def collect_pattern_matches(
    pattern: re.Pattern[str],
    text: str,
    *,
    max_matches: int = JUNK_REGEX_MAX_MATCHES,
) -> tuple[str, ...]:
    if not text:
        return tuple()

    matches: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        value = normalize_whitespace(match.group(0))
        normalized = value.casefold()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        matches.append(value)
        if len(matches) >= max_matches:
            break
    return tuple(matches)


def has_protected_business_context(
    clean_title: str | None,
    clean_text: str,
    *,
    guard_re: re.Pattern[str] = PROTECTED_BUSINESS_CONTEXT_RE,
) -> bool:
    combined = "\n".join(part for part in (clean_title, clean_text) if part)
    return bool(guard_re.search(combined))


def classify_junk_topic(
    *,
    clean_title: str | None,
    clean_text: str,
    document_type: str | None,
    junk_patterns: list[tuple[str, re.Pattern[str]]] = JUNK_PATTERNS,
    guard_re: re.Pattern[str] = PROTECTED_BUSINESS_CONTEXT_RE,
) -> dict[str, Any] | None:
    # junk_patterns/guard_re default to the module's hardcoded set (the v1
    # worker) but can be passed in -- the v5 worker loads them from the
    # agent_1_v5.junk_categories table so the matching logic here stays
    # identical between the two.
    if document_type != "news":
        return None
    if not clean_text.strip():
        return None
    if has_protected_business_context(clean_title, clean_text, guard_re=guard_re):
        return None

    title = clean_title or ""
    for category, pattern in junk_patterns:
        title_matches = collect_pattern_matches(pattern, title)
        body_matches = collect_pattern_matches(pattern, clean_text)
        if not title_matches and len(body_matches) < JUNK_REGEX_MIN_BODY_HITS:
            continue

        matched_terms = list(title_matches)
        for match in body_matches:
            if match.casefold() not in {item.casefold() for item in matched_terms}:
                matched_terms.append(match)
            if len(matched_terms) >= JUNK_REGEX_MAX_MATCHES:
                break

        return {
            "category": category,
            "matched_terms": matched_terms,
            "title_match_count": len(title_matches),
            "body_match_count": len(body_matches),
        }
    return None


def build_preprocess_patch(**values: Any) -> dict[str, Any]:
    payload = dict(values)
    payload["updated_at"] = utc_now_text()
    return {"preprocess": payload}


def process_job(
    conn: psycopg.Connection[Any],
    job_id: int,
    raw_item_id: int,
    *,
    near_duplicate_threshold: float,
) -> str:
    raw_item = fetch_raw_item(conn, raw_item_id)
    if raw_item is None:
        mark_job_status(conn, job_id, "failed")
        conn.commit()
        return "failed_missing_raw"

    existing_clean_item_id = fetch_existing_clean_item_id(conn, raw_item_id)
    if existing_clean_item_id is not None:
        mark_job_status(conn, job_id, "done")
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="already_processed",
                clean_item_id=existing_clean_item_id,
            ),
        )
        conn.commit()
        return "already_processed"

    clean_title, clean_text, language = preprocess_text(raw_item)
    summary = None
    if isinstance(raw_item.get("source_metadata"), dict):
        summary = raw_item["source_metadata"].get("summary")
    if not clean_text:
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="failed",
                reason="empty_clean_text",
            ),
        )
        mark_job_status(conn, job_id, "failed")
        conn.commit()
        return "failed_empty_text"

    if should_filter_non_russian_text(clean_text, language):
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="filtered_out",
                reason="non_russian_text",
                language=language,
                clean_text_length=len(clean_text),
            ),
        )
        mark_job_status(conn, job_id, "done")
        conn.commit()
        return "filtered_non_russian"

    junk_topic = classify_junk_topic(
        clean_title=clean_title,
        clean_text=clean_text,
        document_type=raw_item.get("document_type"),
    )
    if junk_topic is not None:
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="filtered_out",
                reason="junk_topic_regex",
                junk_category=junk_topic["category"],
                junk_matches=junk_topic["matched_terms"],
                title_match_count=junk_topic["title_match_count"],
                body_match_count=junk_topic["body_match_count"],
                language=language,
                clean_text_length=len(clean_text),
            ),
        )
        mark_job_status(conn, job_id, "done")
        conn.commit()
        return f"filtered_junk_{junk_topic['category']}"

    exact_match = find_exact_duplicate(
        conn,
        clean_title=clean_title,
        clean_text=clean_text,
        raw_item_id=raw_item_id,
        summary=summary,
    )
    if exact_match is not None:
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="duplicate",
                duplicate_kind=exact_match["kind"],
                duplicate_of_clean_item_id=exact_match["clean_item_id"],
                duplicate_of_raw_item_id=exact_match["raw_item_id"],
                similarity=exact_match["similarity"],
            ),
        )
        mark_job_status(conn, job_id, "done")
        conn.commit()
        return "duplicate_exact"

    near_match = None
    if raw_item["document_type"] == "news":
        near_match = find_near_duplicate(
            conn,
            clean_title=clean_title,
            clean_text=clean_text,
            raw_item_id=raw_item_id,
            summary=summary,
            threshold=near_duplicate_threshold,
        )

    if near_match is not None:
        merge_source_metadata(
            conn,
            raw_item_id,
            build_preprocess_patch(
                status="duplicate",
                duplicate_kind=near_match["kind"],
                duplicate_of_clean_item_id=near_match["clean_item_id"],
                duplicate_of_raw_item_id=near_match["raw_item_id"],
                similarity=round(near_match["similarity"], 3),
                minhash_similarity=round(near_match["minhash_similarity"], 3),
            ),
        )
        mark_job_status(conn, job_id, "done")
        conn.commit()
        return "duplicate_near"

    clean_item_id = insert_clean_item(
        conn,
        raw_item_id=raw_item_id,
        clean_title=clean_title,
        clean_text=clean_text,
        language=language,
    )
    # rework-agent-1-v5 stage 2: LLM-разметка (label_kr / extract_semantics)
    # выведена из Агента 1 (см. openspec/changes/rework-agent-1-v5). Раньше
    # здесь был enqueue_label_job(conn, clean_item_id).
    merge_source_metadata(
        conn,
        raw_item_id,
        build_preprocess_patch(
            status="cleaned",
            clean_item_id=clean_item_id,
            language=language,
            clean_text_length=len(clean_text),
        ),
    )
    mark_job_status(conn, job_id, "done")
    conn.commit()

    # Only mutate the in-memory dedup cache AFTER the row is durably committed.
    # Otherwise a rollback later in this transaction would leave the cache pointing
    # at a clean_item_id that was never persisted, silently corrupting dedup.
    add_near_duplicate_signature_cache_entry(
        clean_item_id=clean_item_id,
        raw_item_id=raw_item_id,
        clean_title=clean_title,
        clean_text=clean_text,
        summary=summary,
        is_news=(raw_item["document_type"] == "news"),
    )
    return "cleaned"


def open_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    global LOG_FILE

    args = parse_args(argv)
    LOG_FILE = args.log_file

    processed_jobs = 0
    conn = open_connection()

    log_line(
        "INFO",
        (
            "Starting preprocess worker "
            f"batch_size={args.batch_size} once={args.once} "
            f"poll_interval={args.poll_interval} "
            f"near_duplicate_threshold={args.near_duplicate_threshold} "
            f"minhash_size={MINHASH_SIZE} "
            f"shingle_size={MINHASH_SHINGLE_SIZE} "
            f"bands={MINHASH_BANDS} "
            f"rows_per_band={MINHASH_ROWS_PER_BAND}"
        ),
    )

    try:
        while True:
            claimed_jobs = claim_jobs(conn, args.batch_size)
            if not claimed_jobs:
                if args.once:
                    break
                time.sleep(args.poll_interval)
                continue

            for job_id, raw_item_id in claimed_jobs:
                try:
                    result = process_job(
                        conn,
                        job_id,
                        raw_item_id,
                        near_duplicate_threshold=args.near_duplicate_threshold,
                    )
                    log_line(
                        "INFO",
                        f"job_id={job_id} raw_item_id={raw_item_id} result={result}",
                    )
                except Exception as exc:
                    conn.rollback()
                    # Marking the job failed must never itself crash the worker loop,
                    # otherwise one bad row takes down the whole process.
                    try:
                        merge_source_metadata(
                            conn,
                            raw_item_id,
                            build_preprocess_patch(
                                status="failed",
                                reason="exception",
                                error=str(exc),
                            ),
                        )
                        mark_job_status(conn, job_id, "failed")
                        conn.commit()
                    except Exception as mark_exc:
                        conn.rollback()
                        log_line(
                            "ERROR",
                            (
                                f"job_id={job_id} raw_item_id={raw_item_id} "
                                f"could not record failure error={mark_exc}"
                            ),
                            stderr=True,
                        )
                    log_line(
                        "ERROR",
                        (
                            f"job_id={job_id} raw_item_id={raw_item_id} "
                            f"failed error={exc}"
                        ),
                        stderr=True,
                    )

                processed_jobs += 1
                if args.max_jobs is not None and processed_jobs >= args.max_jobs:
                    log_line("INFO", f"Reached max_jobs={args.max_jobs}.")
                    return 0

            if args.once:
                break
    finally:
        conn.close()
        log_line("INFO", "Preprocess worker stopped.")

    return 0


load_dotenv(ENV_PATH)
DB_DSN = os.environ["AGENT_1_DB_DSN"]
LOG_FILE = os.getenv("AGENT_1_PREPROCESS_LOG_FILE", str(DEFAULT_LOG_FILE))


if __name__ == "__main__":
    raise SystemExit(main())

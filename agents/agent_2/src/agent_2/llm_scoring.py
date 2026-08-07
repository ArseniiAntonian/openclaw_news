"""Решающая LLM-оценка кандидатов по словесной рубрике (design D3, D5-D7).

Вызов -- через OpenClaw (паттерн `agent_1/label_kr_worker.py:call_agent_1`),
не напрямую в OpenRouter/другой провайдерский API (design D5). Модель --
параметр конфигурации вызова, не хардкод (design D3, спека
«Вызов решающей LLM — через OpenClaw»).

Кэш -- таблица `agent_1_v5.agent_2_llm_scores`, ключ (id_object,
id_clean_post, model) (design D6): без модели в ключе кэш молча
переиспользует чужие оценки -- уже наступали на эту грабли в прототипе.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_2.db import SCHEMA

THRESHOLD = 7.5  # design D3

RUBRIC_PROMPT_TEMPLATE = """Оцени по шкале 0-10, насколько эта новость относится к объекту наблюдения.

## Объект наблюдения
{label}

Что ищем (поисковое описание объекта): {search_description}

## Новость
Заголовок: {title}
Текст: {text}

## Рубрика
- 10 — новость целиком об объекте наблюдения по существу.
- 4-6 — объект упомянут, но новость по существу про что-то другое.
- 0 — новость не относится к объекту вообще.
Используй промежуточные значения по своему суждению.

## Формат ответа — строго JSON, без пояснений вне JSON:
{{"score": <число от 0 до 10>, "reason": "<одно предложение обоснования>"}}
"""


class ScoringError(RuntimeError):
    pass


class AgentCapacityError(ScoringError):
    """429 / временная перегрузка -- ожидается и обрабатывается пейсером,
    не считается фатальной ошибкой (design D7)."""


def _is_capacity_error_detail(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in ("429", "too many requests", "rate limit", "usage limit", "quota")
    )


def _parse_retry_after_seconds(detail: str) -> float | None:
    """Best-effort вытащить Retry-After из текста ошибки CLI.

    OpenClaw оборачивает HTTP-вызов к провайдеру; заголовок Retry-After
    сюда доходит только если CLI сам его пробрасывает в текст ошибки.
    Если нет -- вызывающий код падает на пейсер по умолчанию (design D7:
    "пейсер по умолчанию, если заголовка нет").
    """
    match = re.search(r"retry[-_ ]after[\"':\s]*(\d+(?:\.\d+)?)", detail, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


class RateLimiter:
    """Перенесено из `agent_1/label_kr_worker.py` без изменений логики."""

    def __init__(self, calls_per_minute: float) -> None:
        self.calls_per_minute = calls_per_minute
        self.next_allowed_at = 0.0

    def wait(self) -> None:
        if self.calls_per_minute <= 0:
            return
        interval_seconds = 60.0 / self.calls_per_minute
        now = time.monotonic()
        if now < self.next_allowed_at:
            time.sleep(self.next_allowed_at - now)
            now = time.monotonic()
        self.next_allowed_at = now + interval_seconds


def resolve_default_openclaw_cmd() -> str:
    bundled = Path("/root/.hermes/node/bin/openclaw")
    if bundled.exists():
        return str(bundled)
    return "openclaw"


@dataclass(frozen=True)
class ScoringConfig:
    openclaw_cmd: str
    agent_id: str
    model: str | None = None
    thinking: str | None = None
    agent_timeout: int = 300
    rate_limit_per_minute: float = 20.0
    max_retries: int = 5
    default_backoff_seconds: float = 30.0


def call_openclaw(
    prompt: str,
    session_key: str,
    *,
    config: ScoringConfig,
) -> str:
    """Один вызов OpenClaw. Поднимает `AgentCapacityError` на 429/перегрузку
    -- вызывающая сторона решает, ждать и повторять или нет (design D7)."""
    cmd = shlex.split(config.openclaw_cmd)
    if not cmd:
        raise ScoringError("openclaw command is empty")

    args = [
        *cmd,
        "agent",
        "--agent",
        config.agent_id,
        "--session-key",
        session_key,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(config.agent_timeout),
    ]
    if config.model:
        args.extend(["--model", config.model])
    if config.thinking:
        args.extend(["--thinking", config.thinking])

    timeout = None if config.agent_timeout == 0 else config.agent_timeout + 60
    completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        if _is_capacity_error_detail(detail):
            raise AgentCapacityError(detail)
        raise ScoringError(f"openclaw call failed: {detail}")
    return completed.stdout


def call_openclaw_with_backoff(
    prompt: str,
    session_key: str,
    *,
    config: ScoringConfig,
) -> str:
    """429 -- временная ошибка, не фатальная (design D7): повтор с учётом
    Retry-After (если он есть в тексте ошибки) или пейсера по умолчанию,
    не пропускает документ без оценки молча."""
    last_error: AgentCapacityError | None = None
    for attempt in range(1, config.max_retries + 1):
        try:
            return call_openclaw(prompt, session_key, config=config)
        except AgentCapacityError as exc:
            last_error = exc
            wait_seconds = _parse_retry_after_seconds(str(exc)) or config.default_backoff_seconds
            if attempt >= config.max_retries:
                break
            time.sleep(wait_seconds)
    raise ScoringError(
        f"решающая оценка не получена после {config.max_retries} попыток "
        f"(последняя ошибка: {last_error})"
    )


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ScoringError("ответ агента не содержит JSON-объект")
    return json.loads(text[start : end + 1])


def parse_score_response(raw_output: str) -> tuple[float, str]:
    """Разобрать ответ OpenClaw (обёрнутый JSON) до `{"score": ..., "reason": ...}`.

    OpenClaw с `--json` оборачивает ответ агента в свой конверт (см.
    `agent_1/label_kr_worker.py:extract_agent_reply_text`); здесь -- та же
    двухуровневая распаковка, упрощённая под задачу с одним числом.
    """
    outer = _extract_json_object(raw_output)
    if "score" in outer:
        payload = outer
    else:
        reply_text = None
        result = outer.get("result")
        if isinstance(result, dict):
            meta = result.get("meta")
            if isinstance(meta, dict):
                reply_text = meta.get("finalAssistantVisibleText") or meta.get(
                    "finalAssistantRawText"
                )
        if reply_text is None:
            meta = outer.get("meta")
            if isinstance(meta, dict):
                reply_text = meta.get("finalAssistantVisibleText") or meta.get(
                    "finalAssistantRawText"
                )
        if reply_text is None:
            raise ScoringError("ответ OpenClaw не содержит текст ассистента со score")
        payload = _extract_json_object(reply_text)

    score = payload.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ScoringError("score должен быть числом 0-10")
    score = float(score)
    if score < 0 or score > 10:
        raise ScoringError(f"score вне диапазона 0-10: {score}")
    reason = payload.get("reason")
    return score, (reason if isinstance(reason, str) else "")


# --------------------------------------------------------------------------- #
# Кэш: agent_1_v5.agent_2_llm_scores, ключ (id_object, id_clean_post, model)
# --------------------------------------------------------------------------- #


def fetch_cached_scores(
    conn, id_object: int, doc_ids: list[int], model: str
) -> dict[int, float]:
    if not doc_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id_clean_post, score
            FROM {SCHEMA}.agent_2_llm_scores
            WHERE id_object = %s AND model = %s AND id_clean_post = ANY(%s)
            """,
            (id_object, model, doc_ids),
        )
        return {row[0]: float(row[1]) for row in cur.fetchall()}


def store_score(conn, id_object: int, id_clean_post: int, model: str, score: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.agent_2_llm_scores (id_object, id_clean_post, model, score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id_object, id_clean_post, model)
            DO UPDATE SET score = EXCLUDED.score, scored_at = now()
            """,
            (id_object, id_clean_post, model, score),
        )
    conn.commit()


def score_candidates(
    conn,
    *,
    id_object: int,
    label: str,
    search_description: str,
    candidates: list[dict[str, Any]],  # [{id_clean_post, title, text}, ...]
    config: ScoringConfig,
) -> dict[int, float]:
    """Оценить всех кандидатов, используя кэш и пропуская уже оценённых
    этой же моделью. Возвращает {id_clean_post: score}."""
    doc_ids = [c["id_clean_post"] for c in candidates]
    model_key = config.model or "default"
    cached = fetch_cached_scores(conn, id_object, doc_ids, model_key)

    limiter = RateLimiter(config.rate_limit_per_minute)
    scores: dict[int, float] = dict(cached)
    for candidate in candidates:
        doc_id = candidate["id_clean_post"]
        if doc_id in cached:
            continue
        prompt = RUBRIC_PROMPT_TEMPLATE.format(
            label=label,
            search_description=search_description,
            title=candidate.get("title") or "",
            text=candidate.get("text") or "",
        )
        session_key = f"agent:{config.agent_id}:agent2-score-obj-{id_object}-doc-{doc_id}"
        limiter.wait()
        raw = call_openclaw_with_backoff(prompt, session_key, config=config)
        score, _reason = parse_score_response(raw)
        store_score(conn, id_object, doc_id, model_key, score)
        scores[doc_id] = score
    return scores
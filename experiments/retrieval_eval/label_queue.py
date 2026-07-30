#!/usr/bin/env python3
"""Шаг 2: разметка очереди по объектам наблюдения моделью через OpenClaw.

Вход  -- `data/label_queue.jsonl` (выход `triage_pool.py`).
Выход -- `data/labels.jsonl`, по строке на документ: какие объекты каталога
к нему относятся. Это и есть эталон, против которого меряется ретрив.

Способ вызова модели повторяет старый разметчик Агента 1
(`label_kr_worker.py`): OpenClaw дёргается подпроцессом
`openclaw agent --agent ... --session-key ... --message ... --json`,
ответ достаётся из `result.meta.finalAssistantVisibleText`. Своего
транспорта не изобретаем -- этот путь уже отлажен на боевой разметке.

## Два свойства, без которых разметка бесполезна

**1. Модель не видит `matched_objects`.** В очереди лежит результат
регексов каталога, и его нельзя показывать разметчику: он спишет ответ, и
recall ключевых слов окажется 100% по построению -- ровно та circularity,
ради ухода от которой пул набирался широким неводом. Поле используется
только как контроль после разметки, никогда как вход.

**2. Качество меряется без участия человека.** Пользователь размечать
вручную не будет, поэтому доверие к меткам нужно чем-то обосновать. Две
встроенные проверки:

- *контроль по объекту 1.* Регекс GigaChat -- единственный, известный
  дословно (остальные реконструированы). Документы, где он сработал, почти
  наверняка про GigaChat. Если модель их не отмечает, меткам верить нельзя.
- *самосогласованность.* Доля документов (`--recheck-frac`) размечается
  второй раз в отдельной сессии, другим порядком и в другой компоновке
  батча. Совпадение двух проходов -- верхняя оценка надёжности.

Обе цифры печатаются в конце. Низкие значения = эталон непригоден, и это
надо увидеть до того, как на нём померены метрики ретрива.

Скрипт возобновляемый: уже размеченные `id_clean_post` при перезапуске
пропускаются, файл дописывается.

    python label_queue.py --queue data/label_queue.jsonl --out data/labels.jsonl
    python label_queue.py --queue ... --out ... --limit 40   # проба
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patterns import OBJECT_PATTERNS  # noqa: E402

DEFAULT_AGENT_ID = "agent_1"
DEFAULT_BATCH = 10
DEFAULT_TIMEOUT = 600
DEFAULT_TEXT_CHARS = 700

# Каталог для промпта: имя + алиасы из observation_objects_context.md.
# Это материал промпта, а не механика матчинга, поэтому живёт здесь, а не в
# patterns.py -- модель читает описания, регексы её не касаются.
CATALOG: dict[int, str] = {
    1: "GigaChat — нейросеть Сбера. Алиасы: ГигаЧат, Гигачад, гига, Кандинский, Kandinsky.",
    2: "YandexGPT / Алиса — ИИ-продукты Яндекса. Алиасы: ЯндексГПТ, YaGPT, Алиса AI, Яндекс Нейро, Yandex Cloud ML.",
    3: "Open-source модели для self-hosting — открытые модели, которые можно развернуть у себя. DeepSeek, Qwen, Llama, Mistral.",
    4: "OpenAI / ChatGPT — глобальные лидеры генеративного ИИ. Также GPT-4/5, Sora, Anthropic, Claude.",
    5: "Доверие к ИИ / общественное восприятие — отношение людей к ИИ: доверие, страх, скепсис, хайп, недовольство ИИ-продуктами.",
    6: "Регуляторика ИИ — законы, маркировка ИИ-контента, персональные данные, госрегулирование, AI Act, инициативы Госдумы.",
    7: "GPU и вычислительные мощности — видеокарты, чипы, память для ИИ, дата-центры под ИИ, Nvidia, дефицит железа.",
    8: "Корпоративное внедрение GenAI — применение ИИ в бизнесе и на производстве, enterprise AI, цифровая трансформация.",
    9: "Лидеры мнений в теме ИИ — эксперты, евангелисты, ИИ-блогеры; публичные высказывания и прогнозы о нейросетях.",
    10: "Инциденты и безопасность GenAI — утечки, галлюцинации, дипфейки, мошенничество с ИИ, сбои и запреты нейросетей.",
}

PROMPT_HEAD = """Ты размечаешь новости для системы мониторинга.

Есть каталог объектов наблюдения:

{catalog}

Ниже {n} новостей. Для каждой определи, к каким объектам каталога она
относится по существу.

Правила:
- Относится = новость сообщает о событии, которое касается этого объекта.
  Проходное упоминание в тексте про другое — НЕ относится.
- Объектов может быть несколько, может не быть ни одного (пустой список).
- Не угадывай по одному лишь наличию слова: важно, о чём новость.
- Если новость про ИИ вообще, но ни под один объект не подходит — пустой список.

Верни СТРОГО JSON, без пояснений и без markdown-обёртки:

{{"labels": [{{"id": <id новости>, "objects": [<номера>], "confidence": "high"|"low"}}]}}

confidence = "low", если сомневаешься или текста мало для решения.
Ответь по всем {n} новостям, ничего не пропуская.

Новости:
"""


class AgentError(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def resolve_default_openclaw_cmd() -> str:
    bundled = Path("/root/.hermes/node/bin/openclaw")
    return str(bundled) if bundled.exists() else "openclaw"


def extract_json_object_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AgentError("в ответе нет JSON-объекта")
    return text[start : end + 1]


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(raw))
    if not isinstance(parsed, dict):
        raise AgentError("JSON верхнего уровня должен быть объектом")
    return parsed


def extract_reply_text(response: dict[str, Any]) -> str:
    """Достаёт текст ответа. Форма совпадает с label_kr_worker."""
    if "labels" in response:
        return json.dumps(response, ensure_ascii=False)
    for container in (response.get("result"), response):
        if not isinstance(container, dict):
            continue
        meta = container.get("meta")
        if isinstance(meta, dict):
            for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        payloads = container.get("payloads")
        if isinstance(payloads, list):
            parts = [
                p["text"].strip()
                for p in payloads
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            if any(parts):
                return "\n".join(p for p in parts if p)
    raise AgentError("не нашёл текст ответа в JSON от OpenClaw")


def build_prompt(batch: list[dict[str, Any]], text_chars: int) -> str:
    catalog = "\n".join(f"{i}. {CATALOG[i]}" for i in sorted(CATALOG))
    head = PROMPT_HEAD.format(catalog=catalog, n=len(batch))
    blocks = []
    for doc in batch:
        # ВНИМАНИЕ: matched_objects сюда не попадает намеренно (см. шапку).
        parts = [f"--- id: {doc['id_clean_post']}", f"Заголовок: {doc.get('title') or ''}"]
        if doc.get("summary"):
            parts.append(f"Аннотация: {doc['summary']}")
        body = (doc.get("text_head") or "")[:text_chars]
        if body:
            parts.append(f"Текст: {body}")
        blocks.append("\n".join(parts))
    return head + "\n\n".join(blocks)


def call_agent(prompt: str, *, cmd: str, agent_id: str, session_key: str,
               model: str | None, timeout: int) -> str:
    argv = shlex.split(cmd)
    if not argv:
        raise AgentError("пустая команда openclaw")
    args = [
        *argv, "agent",
        "--agent", agent_id,
        "--session-key", session_key,
        "--message", prompt,
        "--json",
        "--timeout", str(timeout),
    ]
    if model:
        args.extend(["--model", model])

    completed = subprocess.run(
        args, check=False, capture_output=True, text=True,
        timeout=None if timeout == 0 else timeout + 60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise AgentError(f"openclaw вернул ошибку: {detail}")
    return completed.stdout


def label_batch(batch: list[dict[str, Any]], *, session_key: str, args: argparse.Namespace,
                ) -> dict[int, dict[str, Any]]:
    prompt = build_prompt(batch, args.text_chars)
    raw = call_agent(
        prompt, cmd=args.openclaw_cmd, agent_id=args.agent_id,
        session_key=session_key, model=args.model, timeout=args.agent_timeout,
    )
    payload = parse_json_object(extract_reply_text(parse_json_object(raw)))
    rows = payload.get("labels")
    if not isinstance(rows, list):
        raise AgentError("в ответе нет списка labels")

    valid_ids = {d["id_clean_post"] for d in batch}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        doc_id = row.get("id")
        if doc_id not in valid_ids:
            continue  # модель выдумала id -- молча не принимаем
        objects = [o for o in (row.get("objects") or []) if isinstance(o, int) and o in CATALOG]
        out[doc_id] = {
            "label_objects": sorted(set(objects)),
            "confidence": row.get("confidence") if row.get("confidence") in ("high", "low") else None,
        }
    return out


def chunks(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def report(labelled: dict[int, dict[str, Any]], queue: list[dict[str, Any]],
           recheck: dict[int, list[int]]) -> None:
    by_id = {d["id_clean_post"]: d for d in queue}
    total = len(labelled)
    print(f"\n=== Разметка: {total} документов ===\n")
    if not total:
        return

    counter: Counter[int] = Counter()
    empty = 0
    low = 0
    for doc_id, row in labelled.items():
        objs = row["label_objects"]
        counter.update(objs)
        if not objs:
            empty += 1
        if row.get("confidence") == "low":
            low += 1

    print("Размечено по объектам:")
    for pattern in OBJECT_PATTERNS:
        mark = " (approx-регекс)" if pattern.approx else ""
        print(f"  {pattern.object_id:>2}  {pattern.label[:44]:<44} {counter.get(pattern.object_id, 0):>5}{mark}")
    print(f"\nБез объектов (мусор/не по теме): {empty}  ({empty * 100 // total}%)")
    print(f"Помечено моделью как неуверенные:  {low}  ({low * 100 // total}%)")

    # --- проверка 1: контроль по объекту 1 (единственный точный регекс) ---
    control = [i for i, d in by_id.items() if 1 in (d.get("matched_objects") or []) and i in labelled]
    if control:
        agree = sum(1 for i in control if 1 in labelled[i]["label_objects"])
        print(f"\nКонтроль по объекту 1 (GigaChat, регекс известен дословно):")
        print(f"  регекс сработал на {len(control)}, модель подтвердила {agree}"
              f"  ({agree * 100 // len(control)}%)")
        print("  низкий процент => меткам верить нельзя, разметку надо переделывать")
    else:
        print("\nКонтроль по объекту 1 невозможен: таких документов в очереди нет.")

    # --- проверка 2: самосогласованность на повторном проходе ---
    if recheck:
        same = sum(1 for i, objs in recheck.items()
                   if i in labelled and sorted(objs) == labelled[i]["label_objects"])
        print(f"\nСамосогласованность (повторная разметка вслепую):")
        print(f"  перепроверено {len(recheck)}, совпало точно {same}"
              f"  ({same * 100 // len(recheck)}%)")
        print("  <70% => модель шумит, эталон стоит сузить до confidence=high")
    else:
        print("\nПовторный проход не запускался (--recheck-frac 0).")

    # Ключевая цифра для эксперимента: сколько размеченных положительных
    # регекс каталога НЕ поймал. Это и есть пропуски ключевых слов.
    misses = sum(
        1 for i, row in labelled.items()
        if row["label_objects"] and not set(row["label_objects"]) & set(by_id[i].get("matched_objects") or [])
    )
    positives = sum(1 for row in labelled.values() if row["label_objects"])
    if positives:
        print(f"\nРазмеченных положительных: {positives}")
        print(f"  из них регекс каталога не поймал ни одного их объекта: {misses}"
              f"  ({misses * 100 // positives}%)")
        print("  это верхняя оценка recall-потерь ключевых слов -- ради неё всё и делалось")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Разметка очереди по объектам наблюдения")
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--env-file", type=Path,
                    default=Path("/root/.openclaw/workspace/agents/agent_1/.env"))
    ap.add_argument("--openclaw-cmd", default=os.getenv("AGENT_1_OPENCLAW_CMD", resolve_default_openclaw_cmd()))
    ap.add_argument("--agent-id", default=os.getenv("AGENT_1_LABEL_AGENT_ID", DEFAULT_AGENT_ID))
    ap.add_argument("--model", default=os.getenv("AGENT_1_LABEL_MODEL"))
    ap.add_argument("--agent-timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="документов в одном запросе")
    ap.add_argument("--text-chars", type=int, default=DEFAULT_TEXT_CHARS)
    ap.add_argument("--limit", type=int, default=None, help="разметить только N документов (проба)")
    ap.add_argument("--recheck-frac", type=float, default=0.1,
                    help="доля документов на повторную слепую разметку (0 = выключить)")
    ap.add_argument("--session-prefix", default="retrieval-eval-label")
    ap.add_argument("--seed", type=int, default=17)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)

    if not args.queue.is_file():
        print(f"ERROR: не найден {args.queue}", file=sys.stderr)
        return 1
    queue = [json.loads(l) for l in args.queue.read_text(encoding="utf-8").splitlines() if l.strip()]

    done: dict[int, dict[str, Any]] = {}
    if args.out.is_file():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["id_clean_post"]] = row
        print(f"Возобновление: уже размечено {len(done)}")

    pending = [d for d in queue if d["id_clean_post"] not in done]
    if args.limit is not None:
        pending = pending[: args.limit]
    print(f"К разметке: {len(pending)} из {len(queue)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with args.out.open("a", encoding="utf-8") as handle:
        for n, batch in enumerate(chunks(pending, args.batch), start=1):
            key = f"{args.session_prefix}-{n}-{batch[0]['id_clean_post']}"
            try:
                result = label_batch(batch, session_key=key, args=args)
            except (AgentError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                failures += 1
                print(f"  батч {n}: ОШИБКА {exc}", file=sys.stderr)
                if failures >= 3:
                    print("Три сбоя подряд по батчам — останавливаюсь, разбирайся с причиной.",
                          file=sys.stderr)
                    return 1
                continue
            for doc in batch:
                row = result.get(doc["id_clean_post"])
                if row is None:
                    continue  # модель пропустила документ
                out_row = {
                    "id_clean_post": doc["id_clean_post"],
                    "label_objects": row["label_objects"],
                    "confidence": row["confidence"],
                    "label_source": "openclaw",
                }
                handle.write(json.dumps(out_row, ensure_ascii=False) + "\n")
                done[doc["id_clean_post"]] = out_row
            handle.flush()
            print(f"  батч {n}: размечено {len(result)}/{len(batch)}  (всего {len(done)})")

    # --- повторный слепой проход на подвыборке ---
    recheck: dict[int, list[int]] = {}
    if args.recheck_frac > 0 and done:
        rnd = random.Random(args.seed)
        pool = [d for d in queue if d["id_clean_post"] in done]
        sample = rnd.sample(pool, max(1, int(len(pool) * args.recheck_frac)))
        rnd.shuffle(sample)  # другой порядок и другая компоновка батчей
        print(f"\nПовторная разметка вслепую: {len(sample)} документов")
        for n, batch in enumerate(chunks(sample, args.batch), start=1):
            key = f"{args.session_prefix}-recheck-{n}-{batch[0]['id_clean_post']}"
            try:
                result = label_batch(batch, session_key=key, args=args)
            except (AgentError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                print(f"  recheck {n}: ОШИБКА {exc}", file=sys.stderr)
                continue
            for doc_id, row in result.items():
                recheck[doc_id] = row["label_objects"]

    report(done, queue, recheck)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
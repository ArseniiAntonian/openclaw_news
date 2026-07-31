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

**2. Разметка двухуровневая: «событие» и «упоминание».** Одного бита
«относится / не относится» недостаточно, потому что методы отвечают на
разные вопросы: ключевые слова каталога банка ловят *упоминания*
(«выражения включения новости в выборку объекта»), а пайплайну на выходе
нужны *события* (драйверы). Разметив оба уровня за один проход, recall
считается под любое из двух определений без повторного прогона -- а
контроль сравнивается с суммой, как и положено ключевым словам.

**3. Качество меряется без участия человека.** Пользователь размечать
вручную не будет, поэтому доверие к меткам нужно чем-то обосновать. Две
встроенные проверки:

- *контроль по однозначной подстроке.* `CONTROL_PATTERNS` из `patterns.py`:
  `gigachat|гигачат`, без `кандинск` -- каталожная альтернатива ловит
  Василия Кандинского наравне с моделью Сбера, и построенный на ней
  контроль давал ложную тревогу.
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
# Батч меньше, текста больше: модель обязана видеть примерно то же, что
# видит регекс (до 6000 знаков). В первом прогоне было 700 знаков против
# 6000 у регекса -- методы сравнивались на разных входных данных, и весь
# замер оказался испорчен. Это была ошибка постановки, а не модели.
DEFAULT_BATCH = 5
DEFAULT_TIMEOUT = 900
DEFAULT_TEXT_CHARS = 4000

# Каталог для промпта: имя + алиасы из observation_objects_context.md.
# Это материал промпта, а не механика матчинга, поэтому живёт здесь, а не в
# patterns.py -- модель читает описания, регексы её не касаются.
# Границы объектов заданы явно, через «не относится». Формулировки взяты не
# с потолка: это разбор перелейблинга первого прогона, где объект 8 собрал
# 234 лишние метки («CEO Циан о рынке ипотеки», «микрофонный массив с
# ИИ-обработкой»), объект 10 -- 202, объект 5 -- 133. Расплывчатое название
# категории притягивает всё подряд, пока не сказано, что в неё НЕ входит.
CATALOG: dict[int, str] = {
    1: "GigaChat — генеративная нейросеть Сбера (ГигаЧат, Гигачад) и Кандинский/Kandinsky "
       "для генерации изображений.\n"
       "     НЕ относится: художник Василий Кандинский, выставки живописи.",
    2: "YandexGPT / Алиса — ИИ-продукты Яндекса: YandexGPT, YaGPT, Алиса AI, Яндекс Нейро, "
       "Yandex Cloud ML.\n"
       "     НЕ относится: Алиса Селезнёва, другие тёзки.",
    3: "Open-source модели для self-hosting — открытые модели, разворачиваемые у себя: "
       "DeepSeek, Qwen, Llama, Mistral; их релизы, лицензии, сравнения.\n"
       "     НЕ относится: закрытые облачные модели.",
    4: "OpenAI / ChatGPT — глобальные лидеры генеративного ИИ: OpenAI, ChatGPT, GPT-4/5, "
       "Sora, Anthropic, Claude; их релизы, иски, ограничения доступа.",
    5: "Доверие к ИИ / общественное восприятие — отношение ЛЮДЕЙ к ИИ: опросы, страхи, "
       "скепсис, протест, отказ пользоваться, недовольство ИИ-продуктами.\n"
       "     НЕ относится: обычная новость о выходе или возможностях ИИ-продукта; "
       "рассуждения о влиянии ИИ на экономику.",
    6: "Регуляторика ИИ — конкретные регуляторные действия: законопроекты и законы об ИИ, "
       "обязательная маркировка ИИ-контента, требования к персональным данным, "
       "стандарты, запреты, решения регуляторов.\n"
       "     НЕ относится: чьё-то мнение о том, что ИИ надо бы регулировать.",
    7: "GPU и вычислительные мощности — железо и инфраструктура под ИИ: ускорители и "
       "ИИ-чипы, память, серверы, дата-центры для ИИ-нагрузки, дефицит и поставки.\n"
       "     НЕ относится: видеокарты для игр, майнинг, котировки Nvidia.",
    8: "Корпоративное внедрение GenAI — конкретная компания, отрасль или ведомство "
       "ВНЕДРЯЕТ генеративный ИИ у себя, либо измеримые последствия такого внедрения.\n"
       "     НЕ относится: вендор выпустил ИИ-продукт или добавил ИИ-функцию в устройство; "
       "общие рассуждения об экономике и рынке труда; новость про бизнес, где ИИ помянут "
       "вскользь.",
    9: "Лидеры мнений в теме ИИ — НАЗВАННЫЙ человек публично высказывается об ИИ: "
       "эксперт, руководитель, евангелист, ИИ-блогер; его прогноз или оценка.\n"
       "     НЕ относится: позиция компании или ведомства без конкретного спикера.",
    10: "Инциденты и безопасность GenAI — произошедшее событие: утечка, дипфейк, "
        "мошенничество с ИИ, галлюцинация с последствиями, сбой, блокировка модели.\n"
        "     НЕ относится: общие рассуждения о рисках ИИ, если инцидента не было.",
}

PROMPT_HEAD = """Ты размечаешь новости для системы мониторинга.

Есть каталог объектов наблюдения:

{catalog}

Ниже {n} новостей. Для каждой новости и каждого объекта определи отношение,
различая ДВА разных уровня:

- "событие" — новость сообщает о событии, которое касается этого объекта.
  Объект в центре сюжета или прямо им затронут.
- "упоминание" — объект назван или задет в тексте, но новость про другое.
  Например, он в перечислении, в сравнении, в фоновой справке.
- ничего — объект в новости не фигурирует.

Различать их важно: это два разных вопроса, и ответы на них используются
по-разному. Не сваливай упоминания в события и наоборот.

Правила:
- Читай раздел «НЕ относится» у объекта: он задаёт границу категории.
- Объектов может быть несколько, может не быть ни одного.
- Наличие слова само по себе ничего не решает — важно, о чём новость.
- Текст новости может быть обрезан. Суди по тому, что видишь; если данных
  не хватает для решения, ставь confidence "low".

Верни СТРОГО JSON, без пояснений и без markdown-обёртки:

{{"labels": [{{"id": <id новости>, "events": [<номера>], "mentions": [<номера>], "confidence": "high"|"low"}}]}}

Объект не должен попадать одновременно в events и mentions.
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
        # ВНИМАНИЕ: matched_objects и control_objects сюда не попадают
        # намеренно (см. шапку) -- иначе модель спишет ответ у регекса.
        parts = [f"--- id: {doc['id_clean_post']}", f"Заголовок: {doc.get('title') or ''}"]
        if doc.get("summary"):
            parts.append(f"Аннотация: {doc['summary']}")
        body = (doc.get("text_head") or "")[:text_chars]
        if body:
            parts.append(f"Текст: {body}")
            if len(doc.get("text_head") or "") > text_chars or doc.get("text_truncated_for_prompt"):
                parts.append("(текст обрезан)")
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

    def clean(values: Any) -> set[int]:
        return {o for o in (values or []) if isinstance(o, int) and o in CATALOG}

    for row in rows:
        if not isinstance(row, dict):
            continue
        doc_id = row.get("id")
        if doc_id not in valid_ids:
            continue  # модель выдумала id -- молча не принимаем
        events = clean(row.get("events"))
        # Событие сильнее упоминания: если модель поставила объект в оба
        # списка вопреки инструкции, оставляем его событием.
        mentions = clean(row.get("mentions")) - events
        out[doc_id] = {
            "label_objects": sorted(events),
            "mention_objects": sorted(mentions),
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

    events_counter: Counter[int] = Counter()
    mentions_counter: Counter[int] = Counter()
    empty = 0
    low = 0
    for row in labelled.values():
        events_counter.update(row["label_objects"])
        mentions_counter.update(row.get("mention_objects") or [])
        if not row["label_objects"] and not (row.get("mention_objects") or []):
            empty += 1
        if row.get("confidence") == "low":
            low += 1

    print(f"{'об':>3}  {'объект':<42} {'событие':>8} {'упомин.':>8}")
    for pattern in OBJECT_PATTERNS:
        mark = " *" if pattern.approx else ""
        print(f"  {pattern.object_id:>2}  {pattern.label[:42]:<42} "
              f"{events_counter.get(pattern.object_id, 0):>8} "
              f"{mentions_counter.get(pattern.object_id, 0):>8}{mark}")
    print("  * -- регекс каталога реконструирован")
    print(f"\nНи одного объекта вообще:         {empty}  ({empty * 100 // total}%)")
    print(f"Помечено моделью как неуверенные: {low}  ({low * 100 // total}%)")

    # --- проверка 1: контроль по однозначной подстроке ---
    # Сравнивается с суммой «событие + упоминание»: ключевые слова ловят
    # именно упоминания, поэтому требовать от них событийной точности
    # некорректно. Прошлый прогон сравнивал только с событиями и потому
    # показывал провал там, где расхождения не было.
    control = [i for i, d in by_id.items() if 1 in (d.get("control_objects") or []) and i in labelled]
    if control:
        agree = sum(
            1 for i in control
            if 1 in set(labelled[i]["label_objects"]) | set(labelled[i].get("mention_objects") or [])
        )
        strict = sum(1 for i in control if 1 in labelled[i]["label_objects"])
        print(f"\nКонтроль по объекту 1 (подстрока gigachat/гигачат, без «кандинск»):")
        print(f"  подстрока найдена в {len(control)} документах")
        print(f"  модель отметила объект хоть как-то: {agree}  ({agree * 100 // len(control)}%)")
        print(f"  из них как событие:                 {strict}")
        print("  первая цифра ниже ~80% => меткам верить нельзя")
    else:
        print("\nКонтроль невозможен: документов с подстрокой в очереди нет.")

    # --- проверка 2: самосогласованность на повторном проходе ---
    if recheck:
        same = sum(
            1 for i, pair in recheck.items()
            if i in labelled
            and pair[0] == labelled[i]["label_objects"]
            and pair[1] == (labelled[i].get("mention_objects") or [])
        )
        loose = sum(
            1 for i, pair in recheck.items()
            if i in labelled
            and set(pair[0]) | set(pair[1])
            == set(labelled[i]["label_objects"]) | set(labelled[i].get("mention_objects") or [])
        )
        print(f"\nСамосогласованность (повторная разметка вслепую), {len(recheck)} документов:")
        print(f"  совпало точно (события и упоминания):        {same}"
              f"  ({same * 100 // len(recheck)}%)")
        print(f"  совпал состав объектов без учёта градации:   {loose}"
              f"  ({loose * 100 // len(recheck)}%)")
        print("  вторая цифра <80% => модель шумит в самом наборе объектов")
    else:
        print("\nПовторный проход не запускался (--recheck-frac 0).")

    # Пропуски ключевых слов, по-объектно. Прошлая версия считала на уровне
    # документа («ни один из его объектов не пойман») и потому занижала:
    # документ с метками 4 и 8, где регекс поймал только 4, пропуском не
    # считался, хотя ключевые слова объекта 8 его упустили.
    print("\nПропуски ключевых слов по объектам (событийные метки):")
    print(f"{'об':>3}  {'объект':<42} {'событий':>8} {'пропущ.':>8}")
    for pattern in OBJECT_PATTERNS:
        oid = pattern.object_id
        positives = [i for i, row in labelled.items() if oid in row["label_objects"]]
        if not positives:
            continue
        missed = sum(1 for i in positives if oid not in (by_id[i].get("matched_objects") or []))
        share = f"{missed * 100 // len(positives)}%"
        mark = " *" if pattern.approx else ""
        print(f"  {oid:>2}  {pattern.label[:42]:<42} {len(positives):>8} "
              f"{missed:>5} {share:>4}{mark}")
    print("  * -- часть пропуска может быть узостью нашей реконструкции регекса,")
    print("       а не свойством ключевых слов банка")


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
                    "mention_objects": row["mention_objects"],
                    "confidence": row["confidence"],
                    "label_source": "openclaw",
                }
                handle.write(json.dumps(out_row, ensure_ascii=False) + "\n")
                done[doc["id_clean_post"]] = out_row
            handle.flush()
            print(f"  батч {n}: размечено {len(result)}/{len(batch)}  (всего {len(done)})")

    # --- повторный слепой проход на подвыборке ---
    recheck: dict[int, tuple[list[int], list[int]]] = {}
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
                recheck[doc_id] = (row["label_objects"], row["mention_objects"])

    report(done, queue, recheck)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
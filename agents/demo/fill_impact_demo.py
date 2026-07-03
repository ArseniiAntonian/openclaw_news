#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import psycopg


ROOT = Path(__file__).resolve().parent
ENV_FILE = Path("/root/.openclaw/workspace/agents/agent_1/.env")
BATCH_SIZE = 1000

SBER_RE = re.compile(
    r"\b(sber|sberbank|gigachat|kandinsky|sberpay)\b|"
    r"(\bсбер(?:а|е|у|ом)?\b|\bсбербанк(?:а|е|ом)?\b|сбербанк онлайн|"
    r"\bсбербизнес\b|сбер бизнес|\bсберинвестиц\w*|сбербанк киб|"
    r"\bдомклик\b|\bсберспасибо\b|\bсбермаркет\b)",
    re.IGNORECASE,
)

POSITIVE_RE = re.compile(
    r"(запустил|запускает|внедрил|внедряет|представил|открыл|доступн|снова доступно|"
    r"улучшил|расширил|увеличил|вырос|рост|лидир|лучший|первое место|топ-|"
    r"поддержк|защитил|защита|безопасн|удобн|снизил ставки|кредитные каникулы|"
    r"побед|награ|рекорд|25 миллионов клиентов)",
    re.IGNORECASE,
)

NEGATIVE_RE = re.compile(
    r"(сбой|не работает|недоступн|утечк|мошен|потерял|потеряла|похищ|обман|"
    r"жалоб|комисс|штраф|заморозк|санкц|угроз|уязвим|"
    r"снизил[аио]? довер|\bотказал[аи]?\b|отказ в|проблем|скандал|блокировк|"
    r"\bиск\b|\bсуд\b|риски для|операционн\w*\s+риск|кредитн\w*\s+риск)",
    re.IGNORECASE,
)

PR_RE = re.compile(
    r"(пресс-служб[аые]? сбер|сообщили .*сбер|в сбер[еа]? сообщили|"
    r"сбер запустил|сбер представил|сбер вводит|сбер предоставляет|"
    r"по версии frank rg|25 миллионов клиентов)",
    re.IGNORECASE,
)

BOILERPLATE_RE = re.compile(
    r"оценивайте\s+свои\s+финансовые\s+возможности\s+и\s+риски",
    re.IGNORECASE,
)

VACANCY_RE = re.compile(
    r"(ваканс|отклик|соискател|бренд работодател|интерес к работе|кандидат)",
    re.IGNORECASE,
)

WEAK_KR3_RE = re.compile(
    r"(мойофис|новые облачные технологии|офисного по|офисное по|продуктов «мойофис»)",
    re.IGNORECASE,
)

GOAL_PATTERNS = {
    2: re.compile(
        r"(ммб|мал(ый|ого) бизнес|средн(ий|его) бизнес|предпринимател|"
        r"бизнес-клиент|бизнес клиент|эквайринг|расчетн(ый|ого) счет|рко|"
        r"сбербизнес|сбер бизнес|кредит.*бизнес|для бизнеса|\bип\b|юрлиц)",
        re.IGNORECASE,
    ),
    3: re.compile(
        r"(кксб|корпоративн|крупн(ый|ого) бизнес|корпоративн(ые|ых) клиент|"
        r"бизнес-показател|корпоративн(ый|ого) сегмент|сберинвестиц|брокер|"
        r"акционер|cib|b2b|дистрибьютор)",
        re.IGNORECASE,
    ),
    4: re.compile(
        r"(довер|надежн|надёжн|безопасн|защит|сбережен|клиент|мошен|"
        r"лучший банк|сохранност|поддержк|социальн|помощ)",
        re.IGNORECASE,
    ),
    5: re.compile(
        r"(зарплат|зарплатн(ый|ого|ые|ых) проект|зарплатн(ые|ых) клиент|"
        r"зарплатн(ая|ую) карт|перевод зарплат|выплат[аы] зарплат)",
        re.IGNORECASE,
    ),
    6: re.compile(
        r"(genai|генеративн|искусственн(ый|ого) интеллект|нейросет|\bии\b|\bai\b|"
        r"gigachat|гигачат|kandinsky|кандинск)",
        re.IGNORECASE,
    ),
}

STRICT_POSITIVE = {
    2: re.compile(
        r"(кредитн(ые|ых) каникул|снизить финансовую нагрузку|поддерж(ит|ка)|"
        r"реструктуризац|без комиссии|сбербизнес|платформенн)",
        re.IGNORECASE,
    ),
    3: re.compile(
        r"(сберинвестиц.*доступн|назначен организатор|внедрил.*сервис|"
        r"рост бизнес-показател|клуб акционеров|доля сбера лидирует|b2b|cib)",
        re.IGNORECASE,
    ),
    4: re.compile(
        r"(довер|над[её]ж|защит|безопас|аккредитив|сохранност|лучший банк|"
        r"поддерж(ит|ка)|кредитн(ые|ых) каникул|снизить финансовую нагрузку)",
        re.IGNORECASE,
    ),
    5: re.compile(
        r"(зарплатн(ые|ых) клиент|нов(ые|ых) зарплатн|зарплатн(ая|ую) карт|"
        r"перевод зарплат|зарплат[ау] в сбер|выплат[аы] зарплат)",
        re.IGNORECASE,
    ),
    6: re.compile(
        r"(gigachat|гигачат|kandinsky|кандинск|genai|генеративн|искусственн(ый|ого) интеллект|"
        r"\bии\b|ai|нейросет)",
        re.IGNORECASE,
    ),
}

STRICT_NEGATIVE = {
    2: re.compile(
        r"(комисс|штраф|блокировк|сбой|недоступн|не работает|жалоб|отток|"
        r"ухудш|отказ в|проблем)",
        re.IGNORECASE,
    ),
    3: re.compile(
        r"(сбой|недоступн|не работает|убыт|штраф|санкц|блокировк|отток|"
        r"снизил[аио]?|потерял|проблем)",
        re.IGNORECASE,
    ),
    4: re.compile(
        r"(отток|потерял|блокировк|заморозк|комисс|мошен|утечк|жалоб|"
        r"снизил[аио]? довер|недоступн|сбой|не работает)",
        re.IGNORECASE,
    ),
    5: re.compile(
        r"(зарплат.*сбой|сбой.*зарплат|зарплат.*комисс|зарплат.*блокировк|"
        r"отток.*зарплат|потерял.*зарплатн)",
        re.IGNORECASE,
    ),
    6: re.compile(
        r"(искусственн(ый|ого) интеллект.*риск|нейросет.*риск|genai.*риск|"
        r"gigachat.*сбой|гигачат.*сбой|ии.*сбой|ии.*угроз|нейросет.*проблем)",
        re.IGNORECASE,
    ),
}


def load_dsn() -> str:
    dsn = os.environ.get("AGENT_1_DB_DSN")
    if dsn:
        return dsn
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENT_1_DB_DSN="):
                return line.split("=", 1)[1].strip()
    return "dbname=mvp_db user=postgres"


def domain(url: str) -> str:
    host = urlparse(url or "").netloc
    return host.split("@")[-1].split(":")[0]


def sber_mentions(text: str) -> list[dict]:
    mentions = []
    for match in SBER_RE.finditer(text):
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 120)
        fragment = " ".join(text[start:end].split())
        window = text[max(0, match.start() - 160) : min(len(text), match.end() + 240)]
        sentiment = "neutral"
        confidence = 0.6
        if NEGATIVE_RE.search(window):
            sentiment = "negative"
            confidence = 0.7
        elif POSITIVE_RE.search(window):
            sentiment = "positive"
            confidence = 0.7
        if fragment and all(item["text"] != fragment for item in mentions):
            mentions.append(
                {"text": fragment, "sentiment": sentiment, "confidence": confidence}
            )
    return mentions[:12]


def classify(kr_id: int, goal: str, source: str, url: str, title: str, content: str) -> dict:
    full_text = f"{title or ''}\n{content or ''}"
    signal_text = BOILERPLATE_RE.sub("", full_text)
    sber_matches = list(SBER_RE.finditer(signal_text))
    sber = bool(sber_matches)
    goal_match = bool(GOAL_PATTERNS.get(kr_id, re.compile("$^")).search(full_text))
    windows = [
        signal_text[max(0, m.start() - 220) : min(len(signal_text), m.end() + 320)]
        for m in sber_matches
    ]
    positive = any(STRICT_POSITIVE[kr_id].search(window) for window in windows)
    negative = any(STRICT_NEGATIVE[kr_id].search(window) for window in windows)
    if kr_id == 3 and WEAK_KR3_RE.search(full_text):
        goal_match = False
        positive = False
        negative = False
    if kr_id == 5 and VACANCY_RE.search(full_text):
        positive = False
        negative = False

    impact = "neutral"
    signal_strength = "indirect"
    confidence = 0.6
    why = "Явной причинно-следственной связи с целью Сбера в тексте не найдено."

    if sber and goal_match:
        signal_strength = "direct"
        if negative:
            impact = "negative"
            confidence = 0.7
            why = "Текст явно упоминает Сбер и содержит риск/проблему именно в контексте цели."
        elif positive:
            impact = "positive"
            confidence = 0.7
            why = "Текст явно упоминает Сбер и описывает улучшение/прогресс именно в контексте цели."
    elif sber and kr_id == 4:
        signal_strength = "direct"
        if negative:
            impact = "negative"
            confidence = 0.7
            why = "Негативный сюжет с упоминанием Сбера может снижать доверие."
        elif positive:
            impact = "positive"
            confidence = 0.7
            why = "Позитивный сюжет с упоминанием Сбера поддерживает доверие к бренду."

    paid = 0
    if impact in {"positive", "negative"} and PR_RE.search(full_text):
        paid = 1

    return {
        "impact": impact,
        "signal_strength": signal_strength,
        "theme": (goal or "цель")[:60],
        "why_for_goal": why,
        "confidence": confidence,
        "is_sber_paid_news": paid,
        "mentions": sber_mentions(full_text),
        "_url_domain": domain(url),
        "_source": source,
    }


def update_batch(conn, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE demo.doc_labels
               SET impact = %s,
                   sber_paid_news = %s,
                   entity_tonality = %s::jsonb,
                   raw_json = %s::jsonb
             WHERE id = %s
            """,
            rows,
        )


def main() -> None:
    dsn = load_dsn()
    processed = 0
    changed = {"positive": 0, "negative": 0, "neutral": 0}
    log_path = ROOT / "tonality.log"

    with psycopg.connect(dsn) as read_conn, psycopg.connect(dsn) as write_conn:
        read_conn.execute("SET search_path TO demo, public")
        write_conn.execute("SET search_path TO demo, public")
        with write_conn.cursor() as reset_cur:
            reset_cur.execute(
                """
                UPDATE demo.doc_labels
                   SET impact = 'neutral',
                       sber_paid_news = 0,
                       entity_tonality = '{"mentions":[]}'::jsonb,
                       raw_json = '{"impact":"neutral","signal_strength":"indirect","theme":"not relevant","why_for_goal":"Строка не размечалась, так как relevance=false.","confidence":0.5,"is_sber_paid_news":0,"mentions":[]}'::jsonb
                 WHERE relevance IS NOT TRUE
                   AND (
                       impact <> 'neutral'
                       OR sber_paid_news IS DISTINCT FROM 0
                       OR entity_tonality IS DISTINCT FROM '{"mentions":[]}'::jsonb
                   )
                """
            )
        write_conn.commit()
        with read_conn.cursor(name="impact_fill") as cur:
            cur.itersize = BATCH_SIZE
            cur.execute(
                """
                SELECT d.id, d.kr_id, k.text, r.source, r.url,
                       COALESCE(r.title, ''), COALESCE(r.content, '')
                  FROM demo.doc_labels d
                  JOIN demo.kr k ON k.id = d.kr_id
                  JOIN demo.clean_items c ON c.id = d.clean_item_id
                  JOIN demo.raw_items r ON r.id = c.raw_id
                 WHERE d.relevance IS TRUE
                 ORDER BY d.id
                """
            )
            batch = []
            for label_id, kr_id, goal, source, url, title, content in cur:
                result = classify(kr_id, goal, source, url, title, content)
                changed[result["impact"]] += 1
                entity = {"mentions": result["mentions"]}
                raw = {k: v for k, v in result.items() if not k.startswith("_")}
                batch.append(
                    (
                        result["impact"],
                        result["is_sber_paid_news"],
                        json.dumps(entity, ensure_ascii=False),
                        json.dumps(raw, ensure_ascii=False),
                        label_id,
                    )
                )
                processed += 1
                if len(batch) >= BATCH_SIZE:
                    update_batch(write_conn, batch)
                    write_conn.commit()
                    batch.clear()
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(f"processed={processed} counts={changed}\n")
            if batch:
                update_batch(write_conn, batch)
                write_conn.commit()
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"done processed={processed} counts={changed}\n")
    print(f"done processed={processed} counts={changed}")


if __name__ == "__main__":
    main()

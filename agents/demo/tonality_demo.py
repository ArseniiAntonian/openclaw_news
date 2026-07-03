#!/usr/bin/env python3

import json, os, subprocess
from datetime import datetime, timezone

PSQL = ["sudo", "-u", "postgres", "psql", "mvp_db", "-v", "ON_ERROR_STOP=1", "-Atc"]
LIMIT = int(os.environ.get("LIMIT", "50"))
AGENT = os.environ.get("AGENT", "agent_1")
TIMEOUT = 180

IMPACT_PROMPT = '''Ты — senior business analyst. Определи, как новость влияет на достижение цели.
positive: повышает вероятность/прогресс. negative: снижает/риск. neutral: слабое/разнонаправленное/сомнение.
Не ставь positive, если позитив для конкурентов Сбера (ВТБ, Альфа, Т-банк и др.).
Верни ТОЛЬКО JSON без markdown:
{{"impact":"positive|negative|neutral","theme":"1-3 слова","why_for_goal":"связь","confidence":0.5}}
GOAL: {goal}
SOURCE: {source}
TITLE: {title}
TEXT: {text}'''

PAID_PROMPT = '''Выглядит ли позитивная новость про Сбер как аффилированная/PR/сберо-центричная подача?
1 если: источник/домен связан со Сбером, односторонняя хвала, пресс-релиз, спикеры/продукты Сбера. Иначе 0.
Верни ТОЛЬКО JSON: {{"is_sber_paid_news":0}}
SOURCE: {source}
TITLE: {title}
TEXT: {text}'''

ENTITY_PROMPT = '''Найди ВСЕ явные упоминания Сбербанка/его продуктов и оцени тональность.
positive: явный успех. negative: явная проблема. neutral: факт без оценки (по умолчанию).
Верни ТОЛЬКО JSON: {{"mentions":[{{"text":"фрагмент","sentiment":"positive|negative|neutral","confidence":0.0}}]}}
Если упоминаний нет: {{"mentions":[]}}
TEXT: {text}'''

def q(sql):
return subprocess.check_output(PSQL + [sql], text=True)

def call_llm(prompt):
# вызов агента через codex/openclaw — та же команда, что и для agent_2
cmd = ["openclaw", "agent", "--agent", AGENT,
"--session-key", "demo:tonality",
"--message", prompt, "--json", "--timeout", str(TIMEOUT)]
out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 30)
if out.returncode != 0:
raise RuntimeError(out.stderr.strip()[:200])
txt = out.stdout
s = txt.find("{")
if s < 0:
raise ValueError("no json")
depth = 0
for i in range(s, len(txt)):
if txt[i] == "{":
depth += 1
elif txt[i] == "}":
depth -= 1
if depth == 0:
return json.loads(txt[s:i+1])
raise ValueError("unterminated json")

def esc(s):
return s.replace("'", "''")

def log(m):
print(f"{datetime.now(timezone.utc):%H:%M:%S} {m}", flush=True)

def main():
rows = q(f"""
SELECT d.id, k.text, c.title, c.content, c.source
FROM demo.doc_labels d
JOIN demo.kr k ON k.id = d.kr_id
JOIN demo.clean_items c ON c.id = d.clean_item_id
WHERE d.relevance = true
ORDER BY d.id LIMIT {LIMIT}
""").splitlines()
total = len(rows)
log(f"START | к разметке {total} (limit={LIMIT})")
done = 0
for line in rows:
did, goal, title, content, source = (line.split("|") + [""]*5)[:5]
text = (content or "")[:6000]
try:
imp = call_llm(IMPACT_PROMPT.format(goal=goal, source=source, title=title, text=text))
impact = imp.get("impact", "neutral")
paid = None
if impact in ("positive", "negative"):
p = call_llm(PAID_PROMPT.format(source=source, title=title, text=text))
paid = int(p.get("is_sber_paid_news", 0))
ent = call_llm(ENTITY_PROMPT.format(text=text))
paid_sql = "NULL" if paid is None else str(paid)
ent_sql = esc(json.dumps(ent, ensure_ascii=False))
raw_sql = esc(json.dumps({"impact": imp, "entity": ent}, ensure_ascii=False))
q(f"""UPDATE demo.doc_labels SET impact='{impact}', sber_paid_news={paid_sql},
entity_tonality='{ent_sql}'::jsonb, raw_json='{raw_sql}'::jsonb WHERE id={did}""")
done += 1
log(f"[{did}] '{goal[:22]}' impact={impact} paid={paid} mentions={len(ent.get('mentions',[]))}")
except Exception as e:
log(f"[{did}] FAIL: {e}")
log(f"DONE | размечено {done}/{total}")

if name == "main":
main()

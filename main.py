#!/usr/bin/env python3
"""
Vič Job Tracker — ежедневный скрапер вакансий Kariera.si.

Что делает при каждом запуске:

1. Скачивает страницу со списком всех вакансий на Kariera.si.
2. Для каждой ещё не проверенной вакансии открывает её страницу и читает поля
   «Kraj dela» (город), «Regija», даты и полный текст.
3. Оставляет только вакансии с городом Ljubljana (см. CITY_FILTER).
4. Отмечает вакансии, в тексте которых встречается район/улица рядом с Vič
   (см. VIC_KEYWORDS), и по возможности геокодирует их точнее.
5. Сохраняет всё в data/jobs.json (для карты) и новые вакансии в
   data/new_jobs.json (для Telegram-дайджеста, его шлёт notify.py).

ОГРАНИЧЕНИЕ ПО ДАННЫМ: портал показывает локацию на уровне города
(«Ljubljana»), без адреса работодателя. Поэтому точный пин на карте возможен
только там, где район/улица упомянуты в самом тексте вакансии.
Подробнее — README.md.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — это единственное место, которое обычно нужно править
# ---------------------------------------------------------------------------
CITY_FILTER = "ljubljana"

VIC_KEYWORDS = [
    "vič",
    "rožna dolina",
    "brdo",
    "dolgi most",
    "tržaška cesta",
]

KEYWORD_INCLUDE: list[str] = []
KEYWORD_EXCLUDE: list[str] = []

REQUEST_PAUSE_SEC = 1.0
# ---------------------------------------------------------------------------

BASE_URL = "https://www.kariera.si"
LIST_URL = f"{BASE_URL}/sl/delovna-mesta"

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "jobs.json"
NEW_JOBS_FILE = DATA_DIR / "new_jobs.json"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sl,en;q=0.8",
}

LJUBLJANA_CENTER = (46.0569, 14.5058)
VIC_CENTER = (46.0447, 14.4681)

STREET_RE = re.compile(r"([A-ZČŠŽ][a-zčšž]+(?:\s[A-ZČŠŽa-zčšž]+)?\s(?:cesta|ulica|trg)\s?\d{0,3}[a-z]?)")


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(
            f"403 Forbidden для {url} — сайт отказал в доступе. "
            "Похоже на антибот-защиту. Попробуй позже или увеличь REQUEST_PAUSE_SEC."
        )
    r.raise_for_status()
    return r.text


def list_job_links() -> list[tuple[str, str]]:
    soup = BeautifulSoup(fetch(LIST_URL), "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"/sl/delovno-mesto/[\w-]+/?$")):
        href = a["href"]
        job_id = href.rstrip("/").split("/")[-1]
        found[job_id] = urljoin(BASE_URL, href)
    return list(found.items())


def parse_detail(url: str) -> dict:
    soup = BeautifulSoup(fetch(url), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    main_text = text.split("Sorodna delovna mesta")[0]

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""

    def field(label: str) -> str:
        m = re.search(rf"^{re.escape(label)}\s*:?\s*\n+([^\n]+)", main_text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    return {
        "title": title,
        "regija": field("Regija"),
        "kraj": field("Kraj dela"),
        "datum_objave": field("Datum objave"),
        "rok_prijave": field("Rok prijave"),
        "full_text": main_text,
    }


def geocode(query: str):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "si"},
            headers={"User-Agent": "VicJobTracker/1.0 (personal, non-commercial)"},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print(f"  геокодинг «{query}» не удался: {e}", file=sys.stderr)
    return None


# Ищем ключевые слова только как отдельные слова: простое вхождение "vič"
# срабатывает внутри обычных слов вроде "sendvičev" или фамилий на -vič.
VIC_PATTERNS = [re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)") for kw in VIC_KEYWORDS]


def is_in_vic(full_text: str, kraj: str) -> bool:
    haystack = f"{kraj}\n{full_text}".lower()
    return any(p.search(haystack) for p in VIC_PATTERNS)


def matches_keywords(full_text: str) -> bool:
    text = full_text.lower()
    if KEYWORD_EXCLUDE and any(k.lower() in text for k in KEYWORD_EXCLUDE):
        return False
    if KEYWORD_INCLUDE and not any(k.lower() in text for k in KEYWORD_INCLUDE):
        return False
    return True


def load_store() -> dict:
    if DATA_FILE.exists():
        store = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    else:
        store = {}
    store.setdefault("jobs", {})
    store.setdefault("seen", [])
    return store


def save_store(store: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store["seen"] = sorted(set(store["seen"]))
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def run():
    store = load_store()
    seen = set(store["seen"])

    links = list_job_links()
    if not links:
        print(
            "ОШИБКА: на странице списка не найдено ни одной ссылки на вакансию.\n"
            "Либо сайт недоступен/блокирует запросы, либо поменял структуру ссылок.\n"
            "Данные не тронуты, ничего не отправлено.",
            file=sys.stderr,
        )
        sys.exit(1)

    current_ids = {jid for jid, _ in links}
    to_check = [(jid, url) for jid, url in links if jid not in seen]
    print(f"На сайте сейчас {len(links)} вакансий; ещё не проверенных: {len(to_check)}")

    new_jobs = []
    checked = 0
    empty_kraj = 0

    for job_id, url in to_check:
        time.sleep(REQUEST_PAUSE_SEC)
        try:
            detail = parse_detail(url)
        except Exception as e:
            print(f"  пропускаю {url}: {e}", file=sys.stderr)
            continue

        seen.add(job_id)
        checked += 1
        kraj = detail["kraj"]
        if not kraj:
            empty_kraj += 1
            continue

        if CITY_FILTER not in kraj.lower():
            continue
        if not matches_keywords(detail["full_text"]):
            continue

        in_vic = is_in_vic(detail["full_text"], kraj)
        approx = True
        coords = VIC_CENTER if in_vic else LJUBLJANA_CENTER

        if in_vic:
            street = STREET_RE.search(detail["full_text"])
            if street:
                geo = geocode(f"{street.group(1)}, Ljubljana, Slovenia")
                if geo:
                    coords, approx = geo, False

        record = {
            "id": job_id,
            "url": url,
            "title": detail["title"],
            "kraj": kraj,
            "regija": detail["regija"],
            "datum_objave": detail["datum_objave"],
            "rok_prijave": detail["rok_prijave"],
            "in_vic": in_vic,
            "lat": coords[0],
            "lon": coords[1],
            "approx": approx,
            "active": True,
            "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        store["jobs"][job_id] = record
        new_jobs.append(record)
        print(f"  + {'[Vič] ' if in_vic else ''}{record['title']} — {kraj}")

    for jid, job in store["jobs"].items():
        job["active"] = jid in current_ids

    if checked and empty_kraj == checked:
        print(
            "ПРЕДУПРЕЖДЕНИЕ: ни у одной проверенной вакансии не удалось прочитать поле «Kraj dela».\n"
            "Скорее всего, сайт поменял вёрстку — нужно поправить parse_detail() в main.py.",
            file=sys.stderr,
        )

    store["seen"] = list(seen)
    save_store(store)
    NEW_JOBS_FILE.write_text(json.dumps(new_jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    active_total = sum(1 for j in store["jobs"].values() if j["active"])
    LAST_RUN_FILE.write_text(
        f"{now}\nпроверено новых: {checked}, подошло: {len(new_jobs)}, активных на карте: {active_total}\n",
        encoding="utf-8",
    )
    print(f"Итог: новых подходящих вакансий {len(new_jobs)} "
          f"(из них рядом с Vič: {sum(j['in_vic'] for j in new_jobs)}); активных на карте: {active_total}")


if __name__ == "__main__":
    run()

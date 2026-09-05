#!/usr/bin/env python3
"""
Slovenia Job Tracker — ежедневный скрапер вакансий с Kariera.si и MojeDelo.com.

Что делает при каждом запуске:
  1. Обходит все источники из SOURCES и собирает список вакансий с каждого.
  2. Для каждой ещё не проверенной вакансии открывает её и читает город,
     регион, даты и полный текст.
  3. Оставляет только вакансии в зоне LPP (см. CITY_FILTER).
  4. Отбрасывает вакансии, в тексте которых встретилось хотя бы одно
     стоп-слово (см. KEYWORD_EXCLUDE) — не требующее квалификации,
     без ночных/ранних/поздних смен, без запроса продвинутого словенского,
     полная занятость, физически не тяжёлая. Философия: лучше пропустить
     вакансию, которая не совсем подходит, чем отсеять подходящую —
     поэтому исключаем только по явным, однозначным сигналам в тексте.
  5. Сохраняет всё в data/jobs.json (для дедупликации между запусками) и
     новые вакансии в data/new_jobs.json (их шлёт в Telegram notify.py).

Карты больше нет — координаты и геокодинг из проекта убраны.

Источники. Каждый источник — пара функций list_job_links_<source>() и
parse_detail_<source>(url), зарегистрированных в SOURCES. Идентификаторы
в data/jobs.json префиксованы именем источника («kariera:<id>»,
«mojedelo:<id>»), чтобы не пересекались. CITY_FILTER и KEYWORD_EXCLUDE
общие и применяются ко всем источникам одинаково.

Kariera.si отдаёт обычный HTML, его разбирает BeautifulSoup. MojeDelo.com —
JS-приложение, HTML вакансий не содержит; данные берём из того же открытого
JSON API (api.mojedelo.com), в который ходит их собственный фронтенд.
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

CITY_FILTER = [
    "ljubljana",
    "medvode",
    "brezovica",
    "dobrova",
    "škofljica",
    "skofljica",
    "beričevo",
    "bericevo",
    "dragomelj",
]

KEYWORD_EXCLUDE = [
    "univerzitetna izobrazba",
    "visokošolska izobrazba",
    "visoka strokovna izobrazba",
    "univerzitetna diploma",
    "magisterij",
    "doktorat",
    "vii. stopnja",
    "nočna izmena",
    "nočno delo",
    "delo ponoči",
    "zgodnja izmena",
    "pozna izmena",
    "odlično znanje slovenščine",
    "tekoče znanje slovenščine",
    "materni jezik",
    "skrajšan delovni čas",
    "polovični delovni čas",
    "študentsko delo",
    "delo prek napotnice",
    "sezonsko delo",
    "fizično zahtevno delo",
    "fizično naporno delo",
    "dvigovanje težkih bremen",
    "težko fizično delo",
]

KEYWORD_INCLUDE: list[str] = []

# Пауза между запросами, отдельно для каждого источника. Kariera.si — обычные
# HTML-страницы, MojeDelo.com — их JSON API, который заметно легче.
REQUEST_PAUSE_SEC = 1.0
MOJEDELO_PAUSE_SEC = 0.3

# ---------------------------------------------------------------------------
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


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 403:
        raise RuntimeError(
            f"403 Forbidden для {url} — сайт отказал в доступе. "
            "Похоже на антибот-защиту. Попробуй позже или увеличь REQUEST_PAUSE_SEC."
        )
    r.raise_for_status()
    return r.text


def html_to_text(html: str) -> str:
    """Текст из куска HTML — описания вакансий на MojeDelo приходят разметкой."""
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


# ---------------------------------------------------------------------------
# Источник: Kariera.si (обычный HTML)
# ---------------------------------------------------------------------------
BASE_URL = "https://www.kariera.si"
LIST_URL = f"{BASE_URL}/sl/delovna-mesta"


def list_job_links_kariera() -> list[tuple[str, str]]:
    soup = BeautifulSoup(fetch(LIST_URL), "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"/sl/delovno-mesto/[\w-]+/?$")):
        href = a["href"]
        job_id = href.rstrip("/").split("/")[-1]
        found[job_id] = urljoin(BASE_URL, href)
    return list(found.items())


def parse_detail_kariera(url: str) -> dict:
    soup = BeautifulSoup(fetch(url), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    main_text = text.split("Sorodna delovna mesta")[0]

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""

    # Если поле на странице пустое, за меткой сразу идёт метка следующего поля
    # (например «Kraj dela» → «Datum objave»). Такое значение — не значение.
    labels = {"Regija", "Kraj dela", "Datum objave", "Rok prijave"}

    def field(label: str) -> str:
        m = re.search(rf"^{re.escape(label)}\s*:?\s*\n+([^\n]+)", main_text, re.MULTILINE)
        if not m:
            return ""
        value = m.group(1).strip()
        return "" if value in labels else value

    return {
        "title": title,
        "regija": field("Regija"),
        "kraj": field("Kraj dela"),
        "datum_objave": field("Datum objave"),
        "rok_prijave": field("Rok prijave"),
        "full_text": main_text,
    }


# ---------------------------------------------------------------------------
# Источник: MojeDelo.com (JS-приложение + открытый JSON API)
#
# Страницы MojeDelo рендерятся браузером: в HTML лежит только пустой
# <div id="root">, поэтому BeautifulSoup по нему ничего не найдёт. Зато сам
# фронтенд берёт данные из открытого api.mojedelo.com, куда можно ходить
# обычным requests. Никакой капчи и антибот-защиты там нет — нужны только
# три заголовка (tenantId / channelId / languageId), значения которых сайт
# публикует в своём же конфиге, он и читается ниже.
#
# Аналог поля «Kraj dela» здесь называется town (town.translation, например
# «Ljubljana» или «Ljubljana z okolico»). Не путать с jobLocationInput —
# это произвольный текст работодателя, он часто пустой или вообще про другое
# место («Letališče Brnik» у вакансии с town = Ljubljana).
# ---------------------------------------------------------------------------
MOJEDELO_BASE = "https://www.mojedelo.com"
MOJEDELO_API = "https://api.mojedelo.com"
MOJEDELO_CONFIG_URL = f"{MOJEDELO_API}/uploaded-files/config/www.mojedelo.com/jb.globals.js"
MOJEDELO_PAGE_SIZE = 100  # больше API не отдаёт

_mojedelo_headers: dict | None = None


def mojedelo_headers() -> dict:
    """Заголовки для API. Значения берём из конфига сайта, а не хардкодим."""
    global _mojedelo_headers
    if _mojedelo_headers is not None:
        return _mojedelo_headers

    raw = fetch(MOJEDELO_CONFIG_URL)
    cfg = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    app = cfg["appConfig"]
    _mojedelo_headers = {
        **HEADERS,
        "Accept": "application/json",
        "tenantId": app["tenantId"],
        "channelId": app.get("jbChannelId") or app["adminChannelId"],
        "languageId": app.get("defaultLanguageId") or app["languages"][0]["id"],
    }
    return _mojedelo_headers


def mojedelo_api(path: str, params: list | dict | None = None) -> dict:
    r = requests.get(
        f"{MOJEDELO_API}{path}", params=params, headers=mojedelo_headers(), timeout=40
    )
    r.raise_for_status()
    return r.json()["data"]


def mojedelo_town_ids() -> list[tuple[str, str]]:
    """CITY_FILTER → идентификаторы городов в справочнике MojeDelo.

    Нужны, чтобы не тянуть детали всех ~3800 объявлений Словении: API умеет
    фильтровать по городу на своей стороне. Это только предварительный отбор —
    окончательное решение всё равно принимает общий matches_city().
    """
    found: dict[str, str] = {}
    for city in CITY_FILTER:
        for town in mojedelo_api("/small-index/towns", {"name": city})["items"]:
            if matches_city(town["translation"]):
                found[town["id"]] = town["translation"]
    return sorted(found.items(), key=lambda kv: kv[1])


def mojedelo_slug(title: str) -> str:
    table = str.maketrans("čšžćđČŠŽĆĐ", "cszcdcszcd")
    slug = re.sub(r"[^a-z0-9]+", "-", title.translate(table).lower())
    return slug.strip("-") or "oglas"


def list_job_links_mojedelo() -> list[tuple[str, str]]:
    towns = mojedelo_town_ids()
    if not towns:
        raise RuntimeError(
            "ни один город из CITY_FILTER не нашёлся в справочнике MojeDelo — "
            "либо справочник переехал, либо в CITY_FILTER опечатка"
        )
    print(f"  MojeDelo: города зоны LPP — {', '.join(name for _, name in towns)}")

    found: dict[str, str] = {}
    for town_id, town_name in towns:
        start_from = 0
        while True:
            time.sleep(MOJEDELO_PAUSE_SEC)
            data = mojedelo_api(
                "/job-ads-search",
                [
                    ("townIds", town_id),
                    ("pageSize", MOJEDELO_PAGE_SIZE),
                    ("startFrom", start_from),
                ],
            )
            items = data.get("items") or []
            for it in items:
                found[it["id"]] = f"{MOJEDELO_BASE}/oglas/{mojedelo_slug(it['title'])}/{it['id']}"
            start_from += len(items)
            if not items or start_from >= (data.get("total") or 0):
                break
        print(f"    {town_name}: {data.get('total', 0)}")
    return list(found.items())


def parse_detail_mojedelo(url: str) -> dict:
    job_id = url.rstrip("/").split("/")[-1]
    d = mojedelo_api(f"/job-ads/{job_id}")

    town = d.get("town") or {}
    regions = d.get("regions") or []

    # Часть свойств вакансии на MojeDelo лежит не в прозе, а в справочниках.
    # Подклеиваем к тексту только те, что описывают саму вакансию, — по ним
    # общие стоп-слова работают так же, как по прозе Kariera.si.
    #
    # НЕ подклеиваем educationLevels и employmentTypes: это списки того, что
    # работодатель ГОТОВ ПРИНЯТЬ, а не требования. Метка верхнего уровня
    # образования в справочнике буквально содержит слово «magisterij»
    # («univerzitetni programi pred bolonjsko reformo / magisterij stroke /
    # 2. bolonjska stopnja»), поэтому от их склейки стоп-слово срабатывало
    # на вакансиях, которые магистра лишь допускают наравне с гимназией и
    # основной школой, — терялось около 29% подходящих. То же с
    # «Študentsko delo» в наборе рядом с «Redno, nedoločen d. č.».
    taxonomy: list[str] = []
    for key in ("workModes", "languageRequired"):
        value = d.get(key) or []
        taxonomy += [v.get("translation", "") for v in value if isinstance(v, dict)]
    for key in ("workTime", "totalWorkExperience", "salary", "occupation"):
        value = d.get(key) or {}
        if isinstance(value, dict) and value.get("translation"):
            taxonomy.append(value["translation"])

    parts = [d.get("title") or ""]
    parts += taxonomy
    parts += [
        html_to_text(d.get(key) or "")
        for key in ("adSummary", "jobDescription", "weExpect", "weOffer",
                    "aboutTheCompany", "waysToApply")
    ]

    return {
        "title": d.get("title") or "",
        "regija": regions[0]["translation"] if regions else "",
        "kraj": town.get("translation") or "",
        "datum_objave": (d.get("startDate") or "")[:10],
        "rok_prijave": (d.get("endDate") or "")[:10],
        "full_text": "\n".join(p for p in parts if p),
    }


# ---------------------------------------------------------------------------
# Реестр источников: имя → (список ссылок, разбор вакансии, пауза).
# ---------------------------------------------------------------------------
SOURCES = {
    "kariera": (list_job_links_kariera, parse_detail_kariera, REQUEST_PAUSE_SEC),
    "mojedelo": (list_job_links_mojedelo, parse_detail_mojedelo, MOJEDELO_PAUSE_SEC),
}


def matches_city(kraj: str) -> bool:
    k = kraj.lower()
    return any(city in k for city in CITY_FILTER)


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

    # Собираем списки по всем источникам. Источник, который не отозвался,
    # не должен ронять остальные и не должен гасить свои же вакансии.
    links: list[tuple[str, str]] = []
    current_ids: set[str] = set()
    listed_sources: set[str] = set()

    for source, (list_links, _parse, _pause) in SOURCES.items():
        try:
            found = list_links()
        except Exception as e:
            print(f"ИСТОЧНИК {source}: не удалось получить список — {e}", file=sys.stderr)
            continue
        if not found:
            print(f"ИСТОЧНИК {source}: список пуст, пропускаю", file=sys.stderr)
            continue
        listed_sources.add(source)
        prefixed = [(f"{source}:{raw_id}", url) for raw_id, url in found]
        links += prefixed
        current_ids |= {jid for jid, _ in prefixed}
        print(f"Источник {source}: вакансий на сайте {len(prefixed)}")

    if not links:
        print(
            "ОШИБКА: ни один источник не отдал ни одной вакансии.\n"
            "Либо сайты недоступны/блокируют запросы, либо поменяли структуру.\n"
            "Данные не тронуты, ничего не отправлено.",
            file=sys.stderr,
        )
        sys.exit(1)

    to_check = [(jid, url) for jid, url in links if jid not in seen]
    print(f"Всего вакансий: {len(links)}; ещё не проверенных: {len(to_check)}")

    new_jobs = []
    checked = 0
    empty_kraj = 0
    excluded_by_keyword = 0

    for job_id, url in to_check:
        source = job_id.split(":", 1)[0]
        _list, parse_detail, pause = SOURCES[source]
        time.sleep(pause)
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

        if not matches_city(kraj):
            continue
        if not matches_keywords(detail["full_text"]):
            excluded_by_keyword += 1
            continue

        record = {
            "id": job_id,
            "source": source,
            "url": url,
            "title": detail["title"],
            "kraj": kraj,
            "regija": detail["regija"],
            "datum_objave": detail["datum_objave"],
            "rok_prijave": detail["rok_prijave"],
            "active": True,
            "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        store["jobs"][job_id] = record
        new_jobs.append(record)
        print(f"  + [{source}] {record['title']} — {kraj}")

    # Гасим только вакансии тех источников, чей список мы правда получили.
    for jid, job in store["jobs"].items():
        if jid.split(":", 1)[0] in listed_sources:
            job["active"] = jid in current_ids

    if checked and empty_kraj == checked:
        print(
            "ПРЕДУПРЕЖДЕНИЕ: ни у одной проверенной вакансии не удалось прочитать город.\n"
            "Скорее всего, сайт поменял вёрстку — нужно поправить parse_detail_* в main.py.",
            file=sys.stderr,
        )

    store["seen"] = list(seen)
    save_store(store)
    NEW_JOBS_FILE.write_text(json.dumps(new_jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    active_total = sum(1 for j in store["jobs"].values() if j["active"])
    per_source = ", ".join(
        f"{src}: {sum(1 for j in new_jobs if j['source'] == src)}" for src in SOURCES
    )
    LAST_RUN_FILE.write_text(
        f"{now}\nпроверено новых: {checked}, отброшено по стоп-словам: {excluded_by_keyword}, "
        f"подошло: {len(new_jobs)} ({per_source}), активных всего: {active_total}\n",
        encoding="utf-8",
    )
    print(f"Итог: новых подходящих вакансий {len(new_jobs)} ({per_source}); "
          f"отброшено по стоп-словам: {excluded_by_keyword}; активных всего: {active_total}")


if __name__ == "__main__":
    run()

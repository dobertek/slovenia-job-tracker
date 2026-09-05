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

# Стоп-лист по НАЗВАНИЮ вакансии (не по всему тексту): профессии, которые
# требуют своей квалификации или лицензии. Регистронезависимо, по вхождению.
TITLE_EXCLUDE = [
    "zdravnik", "zobozdravnik", "farmacevt", "veterinar", "medicinska sestra",
    "fizioterapevt", "babica", "psihiater",
    "učitelj", "vzgojitelj", "profesor", "socialni delavec",
    "inženir", "arhitekt", "pravnik", "odvetnik", "računovodja", "psiholog",
    "ekonomist", "programer", "revizor", "notar", "razvijalec",
    "vodj", "direktor", "poslovodja",
    "mehanik", "viličar", "varilec", "električar", "elektrikar", "vodovodar", "zidar",
    "tesar", "ključavničar", "krovec",
    "frizer", "kozmetičarka", "maser",
    "gradbeni delavec", "kopač", "nakladalec",
    "varnostnik", "gasilec", "dimnikar", "rudar",
    "voznik tovornjaka", "voznik tovornega vozila", "poklicni voznik c",
]

# Если в названии есть одно из этих слов, TITLE_EXCLUDE не применяется вовсе:
# «Pomočnik mehanika» — не механик, а подсобник, и должен пройти.
TITLE_EXCLUDE_GUARD = ["pomočnik", "pomočnica", "asistent"]

# Точечная защита от ложных вхождений: слово-маска гасит конкретное стоп-слово
# и только его, остальной список в этом же названии продолжает работать.
# «pripravnik» содержит «pravnik» подстрокой, но стажёр — не юрист; при этом
# «Srednja medicinska sestra … - pripravnik» обязана срезаться по
# «medicinska sestra», поэтому глушить весь заголовок целиком нельзя.
TITLE_EXCLUDE_MASKS = {"pravnik": ["pripravnik"]}

# Эти стоп-слова совпадают только с начала слова. Для «inženir» подстрочный
# матч ничего не спасает и лишь ловит название сферы деятельности внутри
# другого слова («Proizvodni tehnolog – elektroinženirstvo»). Для остальных
# подстрока, наоборот, полезна: она ловит avtomehanik, delovodja, skupinovodja.
TITLE_EXCLUDE_WORD_START = {"inženir"}

# Максимальная ступень образования, которая ещё подходит (V).
MAX_STOPNJA = 5

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
# Ступень образования (stopnja izobrazbe)
# ---------------------------------------------------------------------------
ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9}
_R = r"(?:VIII|VII|VI|V|IV|III|II|I)"
_TOKEN = rf"(?:{_R}|[1-9])\s*(?:[./]\s*[12])?\s*\.?"
_SEP = r"\s*(?:ali|in|oz\.?|do|/|–|-|,)?\s*"
STOPNJA_RE = re.compile(rf"((?:{_TOKEN}{_SEP})+)stopnj", re.MULTILINE)
_NUM_RE = re.compile(rf"({_R}|[1-9])\s*(?:([./])\s*([12]))?")

def parse_stopnja(text: str):
    best = None
    for m in STOPNJA_RE.finditer(text):
        for nm in _NUM_RE.finditer(m.group(1)):
            major = ROMAN.get(nm.group(1)) if nm.group(1) in ROMAN else int(nm.group(1))
            if major is None or not (1 <= major <= 9):
                continue
            val = major + (int(nm.group(3)) / 10 if nm.group(3) else 0)
            best = val if best is None else min(best, val)
    return best


# ---------------------------------------------------------------------------
# Короткие детали для дайджеста: зарплата, сменность, категория прав
# ---------------------------------------------------------------------------
_AMOUNT = r"(\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*€"
_SALARY_WORD = re.compile(r"plač|bruto|neto|zaslužek|osnovn|mesečn", re.I)
# «prevoza» сознательно нет: километраж отсекается порогом суммы, а слово
# «prevoz na delo» стоит рядом с настоящей зарплатой и убивало бы её.
_NOT_SALARY = re.compile(r"malic|prehran|kilometr|regres|božičnic|jubilej|nagrad|bonus|bonitet|"
                         r"štipendij|odpravnin|pokojninsk|zavarovanj|dodatek za|udeležb|"
                         r"letn|za leto|leta 20", re.I)
# Границы фразы. Двоеточие границей НЕ считается: оно как раз вводит сумму
# («Plačilo: Plača 13 €/h»), а вот «Regres za dopust : … 2.496 € (neto)»
# должно остаться одной фразой, иначе regres перестаёт дисквалифицировать.
# Точка внутри числа («18.000») — не конец предложения.
_CLAUSE_END_RE = re.compile(r"[,;!?\n]|(?<!\d)\.|\.(?!\d)")


def _clause(t, start, end):
    left = t[max(0, start - 130): start]
    bounds = [m.end() for m in _CLAUSE_END_RE.finditer(left)]
    right = t[end: end + 70]
    nxt = _CLAUSE_END_RE.search(right)
    return left[bounds[-1] if bounds else 0:] + t[start:end] + right[: nxt.start() if nxt else len(right)]
_HOURLY = re.compile(r"€\s*/?\s*(?:h\b|uro|uri)", re.I)

def _num(s):
    s = s.replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.match(r"^\d{1,3}(\.\d{3})+$", s):
        s = s.replace(".", "")
    try: return float(s)
    except ValueError: return None

def _fmt(v):
    return f"{int(round(v)):,}".replace(",", ".") if v >= 1000 else f"{v:g}".replace(".", ",")

def find_salary(text: str):
    """Зарплата из текста. Суммы про malica/kilometrino/regres не считаются."""
    t = re.sub(r"\s+", " ", text or "")
    found = []
    for m in re.finditer(_AMOUNT, t):
        # И слово о зарплате, и дисквалификаторы ищем в пределах одной фразы.
        # Иначе «letni bonus v višini 1.600 € (…), plačan prost dan» проходит
        # как зарплата, а «Regres … 2.496 € (neto)» — по слову «neto».
        clause = _clause(t, m.start(), m.end())
        if not _SALARY_WORD.search(clause) or _NOT_SALARY.search(clause):
            continue
        v = _num(m.group(1))
        if v is None:
            continue
        hourly = bool(_HOURLY.match(t[m.end() - 1: m.end() + 6]))
        if hourly and 4 <= v <= 100:
            found.append((v, True, m.start(), m.end()))
        elif not hourly and 500 <= v <= 100000:
            found.append((v, False, m.start(), m.end()))
    if not found:
        return ""
    lo = min(found, key=lambda f: f[0])
    # диапазон: две суммы рядом, соединённые тире или «do»
    for a in found:
        for b in found:
            if a[0] < b[0] and a[1] == b[1] and 0 < b[2] - a[3] <= 12 and \
               re.fullmatch(r"[\s\-–—]*(?:do)?[\s\-–—]*", t[a[3]:b[2]]):
                unit = " €/h" if a[1] else " €"
                return f"{_fmt(a[0])}–{_fmt(b[0])}{unit}"
    return f"{_fmt(lo[0])}{' €/h' if lo[1] else ' €'}"

_SHIFT_PATTERNS = [
    (re.compile(r"enoizmensk|\b1[\s-]*izmensk|\beni izmeni\b|\benoizmenski\b", re.I), "1 izmena"),
    (re.compile(r"dvoizmensk|\bdveh izmenah\b|\bdve izmeni\b|\b2[\s-]*izmen", re.I), "2 izmeni"),
    (re.compile(r"triizmensk|troizmensk|\btreh izmenah\b|\b3[\s-]*izmen", re.I), "3 izmene"),
    (re.compile(r"večizmensk|\bveč izmenah\b", re.I), "večizmensko"),
]

def find_shift(text: str):
    t = re.sub(r"izmenjav\w*", " ", re.sub(r"\s+", " ", text or ""), flags=re.I)
    for rx, label in _SHIFT_PATTERNS:
        if rx.search(t):
            return label
    return ""

# Буква категории не должна быть хвостом предыдущего слова: без этого
# «vozniško dovoljenje (kategorija B)» отдавало «E» — конечную букву слова
# «dovoljenje», куда regex откатывался.
_NOT_LETTER = r"(?<![a-zA-ZčšžćđČŠŽĆĐ])"
_LICENSE = re.compile(
    rf"(?:vozniš\w*\s+dovoljenj\w*|izpit\w*)\s*(?:za\s+)?(?:kategorij\w*\s*)?\(?\s*{_NOT_LETTER}([A-E])\b\s*(\+?\s*E\b)?"
    rf"|kategorij[ae]?\s*\(?\s*{_NOT_LETTER}([A-E])\b\s*(\+?\s*E\b)?", re.I)

def find_license(text: str):
    t = re.sub(r"\s+", " ", text or "")
    m = _LICENSE.search(t)
    if not m:
        return ""
    cat = (m.group(1) or m.group(3) or "").upper()
    plus = (m.group(2) or m.group(4) or "").replace(" ", "").upper()
    return f"kat. {cat}{plus}" if cat else ""


# ---------------------------------------------------------------------------
# Сфера деятельности — по заголовку. Категории подобраны по реальному
# распределению заголовков, а не наугад: первое совпадение выигрывает,
# поэтому более узкие категории стоят выше более широких.
# ---------------------------------------------------------------------------
CATEGORIES = [
    ("Zdravstvo in nega", ["bolničar","negoval","zdravstven","oskrbovalec starejših","dom starejših","fizioterap"]),
    ("Gostinstvo", ["kuhar","kuhinj","natakar","strežb","strežnik","delilec hrane","picopek","slaščičar","barist",
                    "gostin","točaj","kuharsk","pomivalec","restavracij","zajtrk","catering","hostes"]),
    ("Čiščenje", ["čistil","čiščenj","sobaric","snažil","perilo","pralnic","higien"]),
    ("Skladišče", ["skladišč","komisionar","manipulant","pakirec","pakiral","pakiranj","embalaž","viličarist",
                   "priprava naročil","sortirec","sortiranj","logist","pošiljk"]),
    ("Transport in dostava", ["voznik","voznica","dostav","šofer","kurir","prevoz","pismonoša","pošti",
                              "disponent","spremljevalec vozila"]),
    ("Trgovina", ["prodajal","blagajni","trgovin","hipermarket","supermarket","market","polnilec polic",
                  "trgovec","mesar","cvetličar","prodajno mesto","poslovne enote","trafik"]),
    ("Prodaja in svetovanje", ["komercialist","zastopnik","sales","account","prodajn","prodaji","prodajo",
                               "svetovalec za prodajo","skrbnik ključnih kupcev","business development"]),
    ("Klicni center in podpora strankam", ["klicnem centru","klicni center","podporo strankam","podpora strankam",
                                           "delo s strankami","telefonist","service desk","podporo uporabnikom",
                                           "rezervacijsk","customer support"]),
    ("Finance in zavarovalništvo", ["bančni","banč","zavarovaln","finanč","škodn","cenilec","saldakont",
                                    "kalkulant","likvidator","obračun"]),
    ("Proizvodnja", ["proizvodn","operater","montaž","monter","strojnik","na stroju","obdelav","linij","livar",
                     "šivilj","tekstil","kovinar","preddelavec","orodjar","mizar","tiskar","upravljalec",
                     "finišer","klepar","pleskar","živilec","peskar","delavec","tehnolog","sestavlja"]),
    ("Vzdrževanje in servis", ["vzdrževal","serviser","servis","tehnik","inštalater","instalater","mehatronik",
                               "sprejemnik","pralec vozil","elektrikar"]),
    ("Gradbeništvo", ["gradben","gradbiš","asfalter","fasader","tesarsk","betoner","cestn"]),
    ("IT in razvoj", ["developer","software","tester","informacijsk","informatik","web ","razvijal","programer",
                      "sistemsk","aplikativ"]),
    ("Administracija", ["referent","administra","tajni","recepcij","asistent","sekretar","kadrov","nabav",
                        "planer","analitik","kontrolor","koordinator","dokument","arhivar"]),
]
DEFAULT_CATEGORY = "Drugo"

def categorize(title: str) -> str:
    t = " " + (title or "").lower() + " "
    for name, keys in CATEGORIES:
        if any(k in t for k in keys):
            return name
    return DEFAULT_CATEGORY


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
        "stopnja": parse_stopnja(main_text),
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


_mojedelo_edu: dict[str, float] | None = None


def mojedelo_education_levels() -> dict[str, float]:
    """id уровня образования → номер ступени, из справочника самого MojeDelo.

    В самих вакансиях метка приходит без римского префикса («dokončana
    gimnazija …»), а в справочнике он есть («V. - dokončana gimnazija …»),
    поэтому сопоставляем по id, а номер берём из префикса. Так маппинг
    не сломается, если MojeDelo переименует уровень.
    """
    global _mojedelo_edu
    if _mojedelo_edu is None:
        roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}
        _mojedelo_edu = {}
        for item in mojedelo_api("/taxonomy/education-levels")["items"]:
            m = re.match(r"\s*(VIII|VII|VI|V|IV|III|II|I)\.?(?:/([12]))?\s*-", item["translation"])
            if m:
                sub = int(m.group(2)) / 10 if m.group(2) else 0
                _mojedelo_edu[item["id"]] = roman[m.group(1)] + sub
    return _mojedelo_edu


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

    # educationLevels — список ДОПУСТИМЫХ уровней, поэтому берём минимальный:
    # вакансия подходит, если среди допустимых есть V или ниже.
    levels = mojedelo_education_levels()
    got = [levels.get(v.get("id")) for v in (d.get("educationLevels") or []) if isinstance(v, dict)]
    got = [g for g in got if g is not None]

    return {
        "title": d.get("title") or "",
        "regija": regions[0]["translation"] if regions else "",
        "kraj": town.get("translation") or "",
        "datum_objave": (d.get("startDate") or "")[:10],
        "rok_prijave": (d.get("endDate") or "")[:10],
        "full_text": "\n".join(p for p in parts if p),
        "stopnja": min(got) if got else None,
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


def matches_title(title: str) -> bool:
    t = (title or "").lower()
    if any(g in t for g in TITLE_EXCLUDE_GUARD):
        return True
    for key in TITLE_EXCLUDE:
        haystack = t
        for mask in TITLE_EXCLUDE_MASKS.get(key, ()):
            haystack = haystack.replace(mask, " ")
        if key in TITLE_EXCLUDE_WORD_START:
            if re.search(rf"(?<![0-9a-zčšžćđ]){re.escape(key)}", haystack):
                return False
        elif key in haystack:
            return False
    return True


def matches_education(stopnja) -> bool:
    """Не найденная или неоднозначная ступень — не повод резать."""
    return stopnja is None or stopnja <= MAX_STOPNJA


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
    excluded_by_title = 0
    excluded_by_stopnja = 0

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
        if not matches_title(detail["title"]):
            excluded_by_title += 1
            continue
        if not matches_education(detail.get("stopnja")):
            excluded_by_stopnja += 1
            continue

        text = f"{detail['title']}\n{detail['full_text']}"
        record = {
            "id": job_id,
            "source": source,
            "url": url,
            "title": detail["title"],
            "kraj": kraj,
            "regija": detail["regija"],
            "datum_objave": detail["datum_objave"],
            "rok_prijave": detail["rok_prijave"],
            "kategorija": categorize(detail["title"]),
            "placa": find_salary(text),
            "izmene": find_shift(text),
            "izpit": find_license(text),
            "stopnja": detail.get("stopnja"),
            "active": True,
            "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        store["jobs"][job_id] = record
        new_jobs.append(record)
        print(f"  + [{source}] {record['title']} — {kraj} [{record['kategorija']}]")

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
    dropped = (f"стоп-слова: {excluded_by_keyword}, названия: {excluded_by_title}, "
               f"ступень образования: {excluded_by_stopnja}")
    LAST_RUN_FILE.write_text(
        f"{now}\nпроверено новых: {checked}, отброшено ({dropped}), "
        f"подошло: {len(new_jobs)} ({per_source}), активных всего: {active_total}\n",
        encoding="utf-8",
    )
    print(f"Итог: новых подходящих вакансий {len(new_jobs)} ({per_source}); "
          f"отброшено — {dropped}; активных всего: {active_total}")


if __name__ == "__main__":
    run()

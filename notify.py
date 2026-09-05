#!/usr/bin/env python3
"""Отправляет дайджест новых вакансий в Telegram. Запускается после main.py."""
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

NEW_JOBS_FILE = Path(__file__).parent / "data" / "new_jobs.json"

SEND_WHEN_EMPTY = True

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TELEGRAM_LIMIT = 3800

# Telegram пропускает в один чат примерно 20 сообщений в минуту. Дайджест из
# двух источников бывает и на несколько десятков сообщений, поэтому держим
# паузу между ними и уважаем retry_after, если всё же упёрлись в лимит.
SEND_PAUSE_SEC = 3.0


def send(text: str):
    for attempt in (1, 2):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.ok:
            return
        if r.status_code == 429 and attempt == 1:
            wait = 5
            try:
                wait = int(r.json()["parameters"]["retry_after"])
            except Exception:
                pass
            print(f"Telegram просит подождать {wait} c, жду и пробую ещё раз.", file=sys.stderr)
            time.sleep(wait + 1)
            continue
        print(f"Telegram вернул ошибку {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()


def chunks(lines: list[str], limit: int = TELEGRAM_LIMIT):
    buf: list[str] = []
    size = 0
    for line in lines:
        if buf and size + len(line) + 1 > limit:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        yield "\n".join(buf)


SOURCE_LABELS = {"kariera": "Kariera", "mojedelo": "MojeDelo"}

DEFAULT_CATEGORY = "Drugo"


def format_job(j: dict) -> str:
    title = html.escape(j.get("title") or "(без названия)")
    source = SOURCE_LABELS.get(j.get("source"), j.get("source") or "")
    tag = f"[{html.escape(source)}] " if source else ""
    # Показываем только то, что реально нашлось, — без «не указано».
    details = [j.get("kraj"), j.get("placa"), j.get("izmene"), j.get("izpit")]
    tail = "".join(f" · {html.escape(d)}" for d in details if d)
    return f'• {tag}<a href="{j["url"]}">{title}</a>{tail}'


def group_by_category(jobs: list[dict]) -> list[tuple[str, list[dict]]]:
    """Группы по убыванию количества, «Drugo» всегда последней."""
    groups: dict[str, list[dict]] = {}
    for j in jobs:
        groups.setdefault(j.get("kategorija") or DEFAULT_CATEGORY, []).append(j)
    return sorted(
        groups.items(),
        key=lambda kv: (kv[0] == DEFAULT_CATEGORY, -len(kv[1]), kv[0]),
    )


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print(
            "ОШИБКА: не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID.\n"
            "В GitHub: Settings → Secrets and variables → Actions → New repository secret.",
            file=sys.stderr,
        )
        sys.exit(1)

    jobs = []
    if NEW_JOBS_FILE.exists():
        jobs = json.loads(NEW_JOBS_FILE.read_text(encoding="utf-8"))

    if not jobs:
        if SEND_WHEN_EMPTY:
            send("Сегодня новых подходящих вакансий нет.")
        print("Новых вакансий нет.")
        return

    lines = [f"<b>Новые вакансии: {len(jobs)}</b>"]
    for category, items in group_by_category(jobs):
        lines.append("")
        lines.append(f"<b>{html.escape(category)} ({len(items)})</b>")
        lines += [format_job(j) for j in items]

    for n, part in enumerate(chunks(lines)):
        if n:
            time.sleep(SEND_PAUSE_SEC)
        send(part)
    print(f"Отправлено: {len(jobs)} вакансий.")


if __name__ == "__main__":
    main()

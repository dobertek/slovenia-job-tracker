#!/usr/bin/env python3
"""Отправляет дайджест новых вакансий в Telegram. Запускается после main.py."""
import html
import json
import os
import sys
from pathlib import Path

import requests

NEW_JOBS_FILE = Path(__file__).parent / "data" / "new_jobs.json"

SEND_WHEN_EMPTY = True

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TELEGRAM_LIMIT = 3800


def send(text: str):
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
    if not r.ok:
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


def format_job(j: dict) -> str:
    title = html.escape(j.get("title") or "(без названия)")
    kraj = j.get("kraj") or ""
    place = f" · {html.escape(kraj)}" if kraj.lower() != "ljubljana" else ""
    mark = "📍 <b>Vič</b>" if j.get("in_vic") else "•"
    return f'{mark} <a href="{j["url"]}">{title}</a>{place}'


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
            send("Сегодня новых вакансий по Любляне нет.")
        print("Новых вакансий нет.")
        return

    vic = [j for j in jobs if j.get("in_vic")]
    rest = [j for j in jobs if not j.get("in_vic")]

    lines = [f"<b>Новые вакансии по Любляне: {len(jobs)}</b>"]
    if vic:
        lines.append(f"\nРядом с Vič ({len(vic)}):")
        lines += [format_job(j) for j in vic]
    if rest:
        lines.append(f"\nОстальная Любляна ({len(rest)}):")
        lines += [format_job(j) for j in rest]

    for part in chunks(lines):
        send(part)
    print(f"Отправлено: {len(jobs)} вакансий.")


if __name__ == "__main__":
    main()

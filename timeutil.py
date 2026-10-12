"""Робота з часом: у сховищі лише UTC, київський час — тільки для відображення.

API віддає мітки у форматі '2026-10-11T21:39:07.000000Z' (UTC). Ми зберігаємо їх
як рядки без змін. Час отримання знімка (`fetched_at`) пишемо в тому ж форматі,
щоб рядки в базі можна було порівнювати лексикографічно.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Потрібен пакет tzdata (на Windows системної бази часових поясів немає).
KYIV = ZoneInfo("Europe/Kyiv")
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now_iso() -> str:
    """Поточний момент у UTC у форматі API."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_utc(ts: str) -> datetime:
    """Рядок UTC із API -> aware datetime (UTC). Рядок у базі не змінюється."""
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def to_kyiv(ts: str) -> datetime:
    """UTC-рядок -> aware datetime у Europe/Kyiv (з урахуванням літнього часу)."""
    return parse_utc(ts).astimezone(KYIV)


def fmt_kyiv(ts: str | None) -> str:
    """Для звітів: '2026-10-12 02:15:07 EEST'. Скорочення пояса завжди вказане."""
    if ts is None:
        return "—"
    return to_kyiv(ts).strftime("%Y-%m-%d %H:%M:%S %Z")

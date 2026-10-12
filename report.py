"""Короткий звіт про вміст бази та якість даних.

Запуск: python report.py [--db data/alarms.sqlite]
Усі часи у звіті — київські (Europe/Kyiv, із позначкою EET/EEST); у базі лишається UTC.
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

import db
from collect import DEFAULT_DB
from timeutil import fmt_kyiv, parse_utc, utc_now_iso

WINDOW_HOURS = 72


def section(title):
    print(f"\n== {title} ==")


def snapshots_section(conn):
    section("Знімки API")
    rows = conn.execute(
        "SELECT CASE WHEN endpoint LIKE '/stats/%' THEN '/stats/{slug}' ELSE endpoint END AS ep,"
        " COUNT(*) n, SUM(ok) ok, MIN(fetched_at) first, MAX(fetched_at) last"
        " FROM snapshots GROUP BY ep ORDER BY ep").fetchall()
    if not rows:
        print("Знімків немає. Спочатку запустіть: python collect.py backfill")
        return False
    for r in rows:
        print(f"{r['ep']:<14} всього {r['n']:>5}, успішних {r['ok']:>5}, помилок {r['n'] - r['ok']:>3}; "
              f"перший {fmt_kyiv(r['first'])}, останній {fmt_kyiv(r['last'])}")
    errors = conn.execute("SELECT endpoint, error FROM snapshots WHERE ok=0 ORDER BY id DESC LIMIT 5").fetchall()
    for e in errors:
        print(f"  помилка: {e['endpoint']}: {e['error']}")
    return True


def episodes_section(conn, now_iso):
    section("Епізоди")
    total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    print(f"Усього епізодів: {total}")
    if not total:
        return
    for title, sql in [
        ("за статусом", "SELECT status k, COUNT(*) n FROM episodes GROUP BY k"),
        ("за типом регіону", "SELECT COALESCE(r.type,'(немає в довіднику)') k, COUNT(*) n FROM episodes e"
                             " LEFT JOIN regions r USING(slug) GROUP BY k"),
        ("за level", "SELECT COALESCE(level,'(не надано)') k, COUNT(*) n FROM episodes GROUP BY k"),
    ]:
        print(f"  {title}: " + ", ".join(f"{r['k']}={r['n']}" for r in conn.execute(sql)))
    lo, hi = conn.execute("SELECT MIN(started_at), MAX(started_at) FROM episodes").fetchone()
    print(f"Початки епізодів від {fmt_kyiv(lo)} до {fmt_kyiv(hi)}")
    cutoff = (parse_utc(now_iso) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    n72 = conn.execute("SELECT COUNT(*) FROM episodes WHERE started_at >= ?", (cutoff,)).fetchone()[0]
    print(f"Епізодів, що почалися за останні {WINDOW_HOURS} год (з {fmt_kyiv(cutoff)}): {n72}")
    print("Примітка: level надає лише /alerts; у /stats його немає, тому для старих епізодів він порожній.")
    print("Примітка: епізоди областей є об'єднанням районних, їх не можна сумувати разом із районними.")


def quality_section(conn, now_iso):
    section("Перевірки якості")
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    print(f"Епізодів зі slug, якого немає в довіднику: "
          f"{q('SELECT COUNT(*) FROM episodes WHERE slug NOT IN (SELECT slug FROM regions)')}")
    print(f"Епізодів з ended_at раніше за started_at: "
          f"{q('SELECT COUNT(*) FROM episodes WHERE ended_at IS NOT NULL AND ended_at < started_at')}")
    near = conn.execute(
        "SELECT a.slug, a.started_at s1, b.started_at s2 FROM episodes a JOIN episodes b"
        " ON a.slug=b.slug AND a.started_at < b.started_at"
        " AND (julianday(b.started_at) - julianday(a.started_at)) * 86400 < 120").fetchall()
    print(f"Пар епізодів одного регіону з початками ближче ніж 120 с (можливі дублікати "
          f"через розбіжність міток між ендпоїнтами): {len(near)}")
    for r in near[:5]:
        print(f"  {r['slug']}: {r['s1']} / {r['s2']}")
    print(f"Епізодів, де started_at у /alerts і /stats розійшовся на 1–2 с (зліплено в один запис): "
          f"{q('SELECT COUNT(*) FROM episodes WHERE started_at_alt IS NOT NULL')}")
    missing = conn.execute(
        "SELECT e.slug, e.started_at, c.api_count FROM episodes e"
        " JOIN coverage c ON c.slug = e.slug AND c.snapshot_id = (SELECT MAX(snapshot_id) FROM coverage WHERE slug = e.slug)"
        " JOIN snapshots s ON s.id = c.snapshot_id"
        " WHERE e.in_alerts = 1 AND e.in_stats = 0"
        "  AND (c.oldest_recent_started_at IS NULL OR e.started_at >= c.oldest_recent_started_at)"
        "  AND e.started_at >= s.api_from"  # старші за вікно /stats там і не мають бути
        "  AND julianday(e.started_at) < julianday(s.api_to) - 120.0 / 86400").fetchall()
    print(f"Епізодів, які є в /alerts, але /stats їх не віддав, хоча мав би (розбіжність ендпоїнтів): {len(missing)}")
    for r in missing[:5]:
        print(f"  {r['slug']}: початок {fmt_kyiv(r['started_at'])}, /stats alerts.count={r['api_count']}")
    unconf = q("SELECT COUNT(*) FROM episodes WHERE status='end_unconfirmed'")
    print(f"Епізодів end_unconfirmed (зникли зі списку активних, але ended_at немає — "
          f"це не доказ завершення): {unconf}")

    sus = conn.execute("SELECT * FROM episodes WHERE suspect IS NOT NULL ORDER BY started_at").fetchall()
    print(f"Підозрілих записів (для окремої перевірки, мітки не змінено): {len(sus)}")
    now = parse_utc(now_iso)
    for r in sus[:12]:
        end = parse_utc(r["ended_at"]) if r["ended_at"] else now
        hours = (end - parse_utc(r["started_at"])).total_seconds() / 3600
        print(f"  {r['slug']:<42} {r['status']:<15} початок {fmt_kyiv(r['started_at'])}, "
              f"{hours:,.1f} год, {r['suspect']}")
    if len(sus) > 12:
        print(f"  ... ще {len(sus) - 12}")


def coverage_section(conn):
    section(f"Повнота журналу подій за {WINDOW_HOURS} год")
    rows = conn.execute(
        "SELECT c.slug, c.api_count, c.recent_len, c.oldest_recent_started_at oldest, s.fetched_at"
        " FROM coverage c JOIN snapshots s ON s.id = c.snapshot_id"
        " WHERE c.snapshot_id = (SELECT MAX(snapshot_id) FROM coverage c2 WHERE c2.slug = c.slug)"
        " ORDER BY c.slug").fetchall()
    if not rows:
        print("Даних про покриття немає.")
        return
    incomplete = []
    for r in rows:
        truncated = r["recent_len"] < r["api_count"]
        cutoff = (parse_utc(r["fetched_at"]) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if truncated and not (r["oldest"] and r["oldest"] <= cutoff):
            incomplete.append(r)
    print(f"Регіонів із знімком /stats: {len(rows)}; повний журнал за {WINDOW_HOURS} год: "
          f"{len(rows) - len(incomplete)}; неповний (API віддав лише частину епізодів): {len(incomplete)}")
    for r in incomplete[:15]:
        print(f"  {r['slug']:<42} API count={r['api_count']}, отримано {r['recent_len']}; "
              f"найстаріший відомий початок {fmt_kyiv(r['oldest'])}")
    if len(incomplete) > 15:
        print(f"  ... ще {len(incomplete) - 15}")
    print("Повнота перевіряється порівнянням alerts.count з кількістю епізодів у recent та межею вікна.")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):  # при перенаправленні Windows бере cp1251, де немає ≥, ⚠ тощо
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    args = p.parse_args(argv)
    if not Path(args.db).exists():
        print(f"Базу {args.db} не знайдено. Спочатку: python collect.py backfill")
        return 1
    conn = db.connect(args.db)
    now = utc_now_iso()
    print(f"Звіт сформовано {fmt_kyiv(now)} (Europe/Kyiv); у базі всі мітки зберігаються в UTC.")
    if snapshots_section(conn):
        episodes_section(conn, now)
        quality_section(conn, now)
        coverage_section(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

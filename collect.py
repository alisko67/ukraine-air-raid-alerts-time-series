"""Збір даних: API -> SQLite. Запуск:

  python collect.py backfill        # довідник регіонів + /alerts + /stats/{slug}?period=7d для всіх регіонів
  python collect.py refresh         # оновити /stats лише там, де є епізоди без ended_at
  python collect.py poll            # цикл: /alerts кожні 60 с, періодично refresh

Повторні запуски безпечні: епізоди мають ключ (slug, started_at) і оновлюються, а не дублюються.
"""
import argparse
import sys
import time

import db
from client import ApiError, TryvohaClient
from timeutil import utc_now_iso

DEFAULT_DB = "data/alarms.sqlite"
MIN_POLL_INTERVAL = 30  # відповіді кешуються на сервері 30–60 с, частіше опитувати немає сенсу


def log(msg: str):
    print(f"[{utc_now_iso()} UTC] {msg}", flush=True)


def snapshot_regions(conn, client) -> bool:
    try:
        db.record_regions(conn, client.regions())
        return True
    except ApiError as e:
        db.record_failure(conn, utc_now_iso(), "/regions", e, http_status=e.status)
        log(f"ПОМИЛКА /regions: {e}")
        return False


def snapshot_alerts(conn, client) -> bool:
    try:
        resp = client.alerts()
    except ApiError as e:
        db.record_failure(conn, utc_now_iso(), "/alerts", e, http_status=e.status)
        log(f"ПОМИЛКА /alerts: {e}")
        return False
    db.record_alerts(conn, resp)
    log(f"/alerts: активних {len(resp.data['alerts'])} (updated_at API: {resp.data['updated_at']})")
    return True


def refresh_stats(conn, client, slugs, period="7d") -> tuple[int, int]:
    """Оновлює епізоди через /stats для списку регіонів. Повертає (успішно, з помилкою)."""
    ok = failed = 0
    for i, slug in enumerate(slugs, 1):
        try:
            db.record_stats(conn, slug, period, client.stats(slug, period))
            ok += 1
        except ApiError as e:
            db.record_failure(conn, utc_now_iso(), f"/stats/{slug}", e,
                              params=f"period={period}", http_status=e.status)
            log(f"ПОМИЛКА /stats/{slug}: {e}")
            failed += 1
        if i % 25 == 0 or i == len(slugs):
            log(f"/stats: {i}/{len(slugs)}")
    return ok, failed


def count_episodes(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def cmd_backfill(conn, client, args):
    before = count_episodes(conn)
    if not snapshot_regions(conn, client):
        return 1
    snapshot_alerts(conn, client)
    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM regions ORDER BY slug")]
    ok, failed = refresh_stats(conn, client, slugs, args.period)
    db.flag_suspicious(conn, utc_now_iso())
    log(f"Готово: регіонів {len(slugs)} (успішно {ok}, помилок {failed}); "
        f"епізодів було {before}, стало {count_episodes(conn)}")
    return 0 if failed == 0 else 2


def cmd_refresh(conn, client, args):
    slugs = db.unresolved_slugs(conn)
    ok, failed = refresh_stats(conn, client, slugs, args.period)
    db.flag_suspicious(conn, utc_now_iso())
    log(f"refresh: регіонів {len(slugs)}, успішно {ok}, помилок {failed}")
    return 0 if failed == 0 else 2


def cmd_poll(conn, client, args):
    if conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 0:
        snapshot_regions(conn, client)
    last_refresh = time.monotonic()
    i = 0
    try:
        while args.max_iterations is None or i < args.max_iterations:
            started = time.monotonic()
            snapshot_alerts(conn, client)
            if (started - last_refresh) / 60 >= args.refresh_minutes:
                refresh_stats(conn, client, db.unresolved_slugs(conn), args.period)
                last_refresh = time.monotonic()
            db.flag_suspicious(conn, utc_now_iso())
            i += 1
            if args.max_iterations is None or i < args.max_iterations:
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log("Зупинено користувачем")
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):  # при перенаправленні Windows бере cp1251, де немає ≥, ⚠ тощо
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "refresh", "poll"):
        s = sub.add_parser(name)
        s.add_argument("--period", default="7d", choices=["24h", "7d", "30d"])
    poll = sub.choices["poll"]
    poll.add_argument("--interval", type=int, default=60, help="секунд між знімками /alerts (мін. 30)")
    poll.add_argument("--refresh-minutes", type=int, default=60)
    poll.add_argument("--max-iterations", type=int, default=None)
    args = p.parse_args(argv)
    if args.cmd == "poll" and args.interval < MIN_POLL_INTERVAL:
        p.error(f"--interval не може бути меншим за {MIN_POLL_INTERVAL} с")

    conn = db.connect(args.db)
    client = TryvohaClient()
    return {"backfill": cmd_backfill, "refresh": cmd_refresh, "poll": cmd_poll}[args.cmd](conn, client, args)


if __name__ == "__main__":
    sys.exit(main())

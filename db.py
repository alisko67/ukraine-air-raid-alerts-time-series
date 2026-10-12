"""SQLite-сховище: схема, збереження знімків API, дедуплікація, статуси подій.

Таблиці (усі поля — лише ті, що підтверджені відповідями API, або наші службові):
  regions     довідник з /regions
  snapshots   кожен запит до API: коли (UTC) отримано, чи вдалося, що повернув
  episodes    епізоди тривог; ключ (slug, started_at); часові мітки API — без змін
  active_log  які епізоди були в /alerts у кожному знімку (щоб бачити зміни в часі)
  coverage    для кожного знімка /stats: скільки епізодів сказав API і скільки
              реально віддав (журнал `recent` обмежений ~20 записами)

Статуси епізоду:
  active          API вважає тривогу триваючою (є в /alerts або ended_at=null в /stats)
  ended           API дав фактичний ended_at
  end_unconfirmed епізод зник зі списку /alerts, але ended_at API не надав.
                  Це НЕ доказ завершення; ended_at у такому разі залишається порожнім.
"""
import sqlite3
from pathlib import Path

from timeutil import parse_utc

SUSPECT_MINUTES = 24 * 60   # довші за добу епізоди позначаємо для ручної перевірки
MINUTES_TOLERANCE = 2       # допуск між minutes від API і ended_at - started_at
KEY_TOLERANCE_SECONDS = 2   # /alerts і /stats іноді розходяться в started_at на 1 с (спостережено)

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    slug        TEXT PRIMARY KEY,
    region_id   INTEGER,
    type        TEXT NOT NULL,          -- state | district | community
    name_uk     TEXT,
    name_en     TEXT,
    oblast_slug TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at     TEXT NOT NULL,       -- UTC, коли ми отримали відповідь
    endpoint       TEXT NOT NULL,       -- '/alerts', '/stats/<slug>', '/regions'
    params         TEXT,
    http_status    INTEGER,
    ok             INTEGER NOT NULL,    -- 1 успішно, 0 помилка
    error          TEXT,
    api_updated_at TEXT,                -- поле updated_at з /alerts
    api_from       TEXT,                -- поля from/to з /stats
    api_to         TEXT,
    item_count     INTEGER
);
CREATE TABLE IF NOT EXISTS episodes (
    slug             TEXT NOT NULL,
    started_at       TEXT NOT NULL,     -- як у API, UTC, не змінюється
    ended_at         TEXT,              -- лише значення з API; NULL = невідомо/триває
    level            TEXT,              -- red | yellow; дає лише /alerts (у /stats його немає)
    status           TEXT NOT NULL,     -- active | ended | end_unconfirmed
    api_minutes      INTEGER,           -- поле minutes з /stats (для триваючих — до моменту запиту)
    first_seen_at    TEXT NOT NULL,     -- коли МИ вперше побачили запис (не початок події!)
    last_seen_at     TEXT NOT NULL,     -- коли останній знімок містив цей епізод
    source_first     TEXT NOT NULL,     -- alerts | stats
    last_snapshot_id INTEGER,
    suspect          TEXT,              -- причини позначки для перевірки (через кому) або NULL
    started_at_alt   TEXT,              -- started_at з іншого ендпоїнта, якщо відрізнявся на 1–2 с
    in_alerts        INTEGER NOT NULL DEFAULT 0,  -- епізод хоч раз був у /alerts
    in_stats         INTEGER NOT NULL DEFAULT 0,  -- епізод хоч раз був у /stats recent
    PRIMARY KEY (slug, started_at)
);
CREATE TABLE IF NOT EXISTS active_log (
    snapshot_id INTEGER NOT NULL,
    slug        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    level       TEXT,
    PRIMARY KEY (snapshot_id, slug, started_at)
);
CREATE TABLE IF NOT EXISTS coverage (
    snapshot_id              INTEGER PRIMARY KEY,
    slug                     TEXT NOT NULL,
    period                   TEXT NOT NULL,
    api_count                INTEGER,   -- alerts.count за вікно
    recent_len               INTEGER,   -- скільки епізодів реально є в recent
    oldest_recent_started_at TEXT       -- найстаріший епізод у recent
);
CREATE INDEX IF NOT EXISTS idx_episodes_started ON episodes(started_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_endpoint ON snapshots(endpoint, id);
"""


def connect(path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _add_snapshot(conn, fetched_at, endpoint, *, params=None, http_status=None, ok=1,
                  error=None, api_updated_at=None, api_from=None, api_to=None,
                  item_count=None) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots (fetched_at, endpoint, params, http_status, ok, error,"
        " api_updated_at, api_from, api_to, item_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fetched_at, endpoint, params, http_status, ok, error,
         api_updated_at, api_from, api_to, item_count))
    return cur.lastrowid


def _canonical_start(conn, slug, started_at) -> str:
    """Ключ епізоду. Якщо для цього регіону вже є запис із початком у межах
    KEY_TOLERANCE_SECONDS, це той самий епізод: беремо його started_at (першу
    побачену мітку), а мітку з іншого ендпоїнта пишемо в started_at_alt."""
    row = conn.execute(
        "SELECT started_at FROM episodes WHERE slug=?"
        " AND ABS(julianday(started_at) - julianday(?)) * 86400 <= ?"
        " ORDER BY ABS(julianday(started_at) - julianday(?)) LIMIT 1",
        (slug, started_at, KEY_TOLERANCE_SECONDS, started_at)).fetchone()
    key = row[0] if row else started_at
    if key != started_at:
        conn.execute("UPDATE episodes SET started_at_alt=? WHERE slug=? AND started_at=?",
                     (started_at, slug, key))
    return key


def record_failure(conn, fetched_at, endpoint, error, *, params=None, http_status=None) -> int:
    """Невдалий запит теж фіксуємо: прогалини в даних мають бути видимі."""
    with conn:
        return _add_snapshot(conn, fetched_at, endpoint, params=params,
                             http_status=http_status, ok=0, error=str(error))


def record_regions(conn, resp) -> int:
    regions = resp.data["regions"]
    with conn:
        sid = _add_snapshot(conn, resp.fetched_at, "/regions", http_status=resp.status,
                            item_count=len(regions))
        conn.executemany(
            "INSERT INTO regions (slug, region_id, type, name_uk, name_en, oblast_slug)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET region_id=excluded.region_id,"
            " type=excluded.type, name_uk=excluded.name_uk, name_en=excluded.name_en,"
            " oblast_slug=excluded.oblast_slug",
            [(r["slug"], r.get("id"), r["type"], r.get("name_uk"), r.get("name_en"),
              r.get("oblast_slug")) for r in regions])
    return sid


def record_alerts(conn, resp) -> int:
    """Знімок /alerts: нові епізоди додаються, відомі оновлюються, зниклі позначаються."""
    data, fetched_at = resp.data, resp.fetched_at
    with conn:
        prev = conn.execute(
            "SELECT MAX(id) FROM snapshots WHERE endpoint='/alerts' AND ok=1").fetchone()[0]
        sid = _add_snapshot(conn, fetched_at, "/alerts", http_status=resp.status,
                            api_updated_at=data["updated_at"], item_count=len(data["alerts"]))
        for a in data["alerts"]:
            key = _canonical_start(conn, a["slug"], a["started_at"])
            conn.execute("INSERT OR IGNORE INTO active_log VALUES (?,?,?,?)",
                         (sid, a["slug"], key, a.get("level")))
            conn.execute(
                "INSERT INTO episodes (slug, started_at, level, status, first_seen_at,"
                " last_seen_at, source_first, last_snapshot_id, in_alerts)"
                " VALUES (?,?,?,'active',?,?,'alerts',?,1)"
                " ON CONFLICT(slug, started_at) DO UPDATE SET in_alerts=1,"
                "  level=COALESCE(excluded.level, episodes.level), last_seen_at=excluded.last_seen_at,"
                "  last_snapshot_id=excluded.last_snapshot_id,"
                "  status=CASE WHEN episodes.ended_at IS NULL THEN 'active' ELSE episodes.status END",
                (a["slug"], key, a.get("level"), fetched_at, fetched_at, sid))
        if prev is not None:
            # Був у попередньому успішному знімку, зник зараз, а ended_at немає.
            conn.execute(
                "UPDATE episodes SET status='end_unconfirmed'"
                " WHERE ended_at IS NULL AND status='active'"
                "  AND (slug, started_at) IN (SELECT slug, started_at FROM active_log WHERE snapshot_id=?)"
                "  AND (slug, started_at) NOT IN (SELECT slug, started_at FROM active_log WHERE snapshot_id=?)",
                (prev, sid))
    return sid


def record_stats(conn, slug, period, resp) -> int:
    """Знімок /stats/{slug}: додає епізоди з recent і дописує ended_at, коли API його надав."""
    data, fetched_at = resp.data, resp.fetched_at
    alerts = data["alerts"]
    recent = alerts["recent"]
    with conn:
        sid = _add_snapshot(conn, fetched_at, f"/stats/{slug}", params=f"period={period}",
                            http_status=resp.status, api_from=data["from"], api_to=data["to"],
                            item_count=len(recent))
        for ep in recent:
            status = "ended" if ep["ended_at"] else "active"
            key = _canonical_start(conn, slug, ep["started_at"])
            conn.execute(
                "INSERT INTO episodes (slug, started_at, ended_at, status, api_minutes,"
                " first_seen_at, last_seen_at, source_first, last_snapshot_id, in_stats)"
                " VALUES (?,?,?,?,?,?,?,'stats',?,1)"
                " ON CONFLICT(slug, started_at) DO UPDATE SET in_stats=1,"
                "  ended_at=COALESCE(excluded.ended_at, episodes.ended_at),"
                "  status=CASE WHEN COALESCE(excluded.ended_at, episodes.ended_at) IS NULL"
                "              THEN 'active' ELSE 'ended' END,"
                "  api_minutes=excluded.api_minutes, last_seen_at=excluded.last_seen_at,"
                "  last_snapshot_id=excluded.last_snapshot_id",
                (slug, key, ep["ended_at"], status, ep.get("minutes"),
                 fetched_at, fetched_at, sid))
        oldest = min((e["started_at"] for e in recent), default=None)
        conn.execute("INSERT INTO coverage VALUES (?,?,?,?,?,?)",
                     (sid, slug, period, alerts["count"], len(recent), oldest))
    return sid


def unresolved_slugs(conn) -> list[str]:
    """Регіони, де є епізоди без підтвердженого ended_at (їх варто оновити через /stats)."""
    rows = conn.execute(
        "SELECT DISTINCT slug FROM episodes WHERE ended_at IS NULL ORDER BY slug").fetchall()
    return [r["slug"] for r in rows]


def flag_suspicious(conn, now_iso: str) -> int:
    """Позначає записи для окремої перевірки. Вихідні часові мітки не змінює."""
    now = parse_utc(now_iso)
    changed = 0
    rows = conn.execute(
        "SELECT slug, started_at, ended_at, api_minutes, suspect FROM episodes").fetchall()
    with conn:
        for r in rows:
            start, reasons = parse_utc(r["started_at"]), []
            if r["ended_at"] is None:
                if (now - start).total_seconds() / 60 > SUSPECT_MINUTES:
                    reasons.append("long_active")
            else:
                minutes = (parse_utc(r["ended_at"]) - start).total_seconds() / 60
                if minutes < 0:
                    reasons.append("end_before_start")
                elif minutes > SUSPECT_MINUTES:
                    reasons.append("long_ended")
                if r["api_minutes"] is not None and abs(r["api_minutes"] - minutes) > MINUTES_TOLERANCE:
                    reasons.append("minutes_mismatch")
            value = ",".join(reasons) or None
            if value != r["suspect"]:
                conn.execute("UPDATE episodes SET suspect=? WHERE slug=? AND started_at=?",
                             (value, r["slug"], r["started_at"]))
                changed += 1
    return changed

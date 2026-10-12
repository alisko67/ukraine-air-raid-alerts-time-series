"""Історична статистика повітряних тривог для однієї території (область, район, громада, місто).

Запуск:
  python region.py --list [запит] [--type state|district|community] [--oblast SLUG]
  python region.py харків              # slug або частина назви (укр./англ.); при кількох збігах — вибір зі списку
  python region.py kupianskii-raion --hours 72
  python region.py                     # запитає назву інтерактивно

Принципи (щоб не змішувати рівні й не підміняти дані):
  * береться лише власний журнал території (її slug у /stats та /alerts); дані дочірніх чи батьківських
    територій не додаються й не підставляються. Епізоди області API подає як об'єднання районних — це
    інша сутність, ніж «кількість районів у тривозі», і з районними даними не сумується;
  * години до моменту `complete_from` (журнал API обмежений ~20 епізодами) показуються як неповні;
  * відсутність епізодів не трактується як відсутність тривог.
Час: у базі UTC, у звіті й на графіку — Europe/Kyiv.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import db
from analysis import HOUR, complete_from, data_asof, fmt_t, load_episodes
from collect import DEFAULT_DB
from timeutil import KYIV

MAX_HOURS = 168                 # /stats віддає вікно 7 діб, глибше історії немає
DEFAULT_HOURS = 72
MINUTE = pd.Timedelta(minutes=1)
KIND = {"district": "район", "community": "громада"}


# ============================== Вибір території ====================================
def norm(s: str) -> str:
    return (s or "").casefold().replace("’", "'").replace("ʼ", "'").replace("`", "'").strip()


def children(conn, slug) -> list:
    return [r["slug"] for r in conn.execute("SELECT slug FROM regions WHERE oblast_slug=?", (slug,))]


def kind_label(conn, row) -> str:
    if row["type"] in KIND:
        return KIND[row["type"]]
    return "область (має райони)" if children(conn, row["slug"]) else "область/місто без районів у даних"


def find_regions(conn, query=None, rtype=None, oblast=None) -> list:
    """Точний збіг slug має пріоритет; інакше — підрядок у slug, укр. або англ. назві."""
    rows = conn.execute("SELECT * FROM regions ORDER BY name_uk").fetchall()
    if rtype:
        rows = [r for r in rows if r["type"] == rtype]
    if oblast:
        rows = [r for r in rows if r["oblast_slug"] == oblast]
    if query:
        q = norm(query)
        exact = [r for r in rows if norm(r["slug"]) == q or norm(r["name_uk"]) == q or norm(r["name_en"]) == q]
        if exact:
            return exact
        rows = [r for r in rows if q in norm(r["slug"]) or q in norm(r["name_uk"]) or q in norm(r["name_en"])]
    return rows


def availability(conn, row, asof, hours) -> str:
    """Короткий статус даних для списку територій."""
    cov = conn.execute(
        "SELECT c.api_count, c.recent_len FROM coverage c WHERE c.snapshot_id ="
        " (SELECT MAX(snapshot_id) FROM coverage WHERE slug=?)", (row["slug"],)).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM episodes WHERE slug=?", (row["slug"],)).fetchone()[0]
    flagged = conn.execute("SELECT COUNT(*) FROM episodes WHERE slug=? AND suspect IS NOT NULL",
                           (row["slug"],)).fetchone()[0]
    if cov is None:
        return "немає знімка /stats"
    if n == 0:
        return "епізодів немає (не доказ спокою)"
    parts = []
    if cov["api_count"] == 0:
        parts.append("лише /alerts")
    elif cov["recent_len"] < cov["api_count"]:
        parts.append("журнал обрізаний")
    else:
        parts.append("журнал повний")
    if flagged:
        parts.append(f"підозрілих: {flagged}")
    return ", ".join(parts)


def print_list(conn, rows):
    asof = data_asof(conn)
    ws = max([len(r["slug"]) for r in rows] + [4]) + 2            # ширина стовпців за вмістом
    wn = max([len(r["name_uk"] or "") for r in rows] + [5]) + 2
    print(f"{'#':>3}  {'slug':<{ws}}{'тип':<36}{'назва':<{wn}}стан даних")
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {r['slug']:<{ws}}{kind_label(conn, r):<36}{(r['name_uk'] or ''):<{wn}}"
              f"{availability(conn, r, asof, DEFAULT_HOURS)}")
    print(f"Усього: {len(rows)}")


# ================================= Профіль =========================================
def hourly_minutes(eps: pd.DataFrame, hours: pd.DatetimeIndex) -> np.ndarray:
    """Хвилин у тривозі за кожну годину [h, h+1); епізоди однієї території не сумуються понад 60."""
    m = np.zeros(len(hours))
    for r in eps.itertuples():
        for i, h in enumerate(hours):
            lo, hi = max(r.start, h), min(r.end, h + HOUR)
            if hi > lo:
                m[i] += (hi - lo) / MINUTE
    return np.minimum(m, 60.0)


def region_profile(conn, slug: str, hours_n: int = DEFAULT_HOURS) -> dict:
    row = conn.execute("SELECT * FROM regions WHERE slug=?", (slug,)).fetchone()
    if row is None:
        raise KeyError(slug)
    asof = data_asof(conn)
    end_h = asof.floor("h")
    hours = pd.date_range(end_h - hours_n * HOUR, end_h - HOUR, freq="h")
    cf = complete_from(conn, [slug])[slug]
    cov = conn.execute(
        "SELECT c.api_count, c.recent_len, s.api_from, s.api_to FROM coverage c JOIN snapshots s ON s.id=c.snapshot_id"
        " WHERE c.snapshot_id=(SELECT MAX(snapshot_id) FROM coverage WHERE slug=?)", (slug,)).fetchone()

    untracked = bool(cov and cov["api_count"] == 0)
    if untracked:
        # /stats не віддає епізодів цієї території: «спокій» невідрізнити від «не відстежується».
        # Історію вважаємо відомою лише з моменту нашого першого знімка /alerts.
        first = conn.execute("SELECT MIN(fetched_at) FROM snapshots WHERE endpoint='/alerts' AND ok=1").fetchone()[0]
        cf = max(cf, pd.Timestamp(first)) if first else asof

    eps, _, _ = load_episodes(conn, [slug], asof, exclude_stuck=False)   # лише власний slug
    eps = eps[(eps["end"] > hours[0]) & (eps["start"] < asof)].sort_values("start").copy()
    eps["duration_min"] = ((eps["end"] - eps["start"]) / MINUTE).round(1)
    eps["flagged"] = eps["suspect"].notna()          # NaN у pandas «істинний», тому окрема булева колонка
    eps["before_complete"] = eps["end"] <= cf        # епізод повністю в ділянці неповної історії
    clean = eps[~eps["flagged"]]

    minutes = hourly_minutes(eps, hours)
    minutes_clean = hourly_minutes(clean, hours)
    known = np.array([cf <= h for h in hours])

    kh = int(known.sum())
    inside = eps[(eps["start"] >= hours[0]) & eps["ended_at"].notna()]
    stats = {
        "known_hours": kh,
        "alert_minutes": float(minutes[known].sum()),
        "alert_minutes_clean": float(minutes_clean[known].sum()),
        "share": float(minutes[known].sum() / (60 * kh)) if kh else None,
        "share_clean": float(minutes_clean[known].sum() / (60 * kh)) if kh else None,
        "hours_with_alert": int((minutes[known] > 0).sum()),
        "n_episodes": len(eps),
        "n_started_in_window": int((eps["start"] >= hours[0]).sum()),
        "n_ended": int(eps["ended_at"].notna().sum()),
        "n_ongoing": int((eps["status"] == "active").sum()),
        "n_unconfirmed": int((eps["status"] == "end_unconfirmed").sum()),
        "n_suspect": int(eps["flagged"].sum()),
        "ended_inside_n": len(inside),
        "ended_inside_median": float(inside["duration_min"].median()) if len(inside) else None,
        "ended_inside_max": float(inside["duration_min"].max()) if len(inside) else None,
    }
    prof = {"region": dict(row), "kind": kind_label(conn, row), "n_children": len(children(conn, slug)),
            "parent": row["oblast_slug"], "asof": asof, "hours": hours, "minutes": minutes,
            "minutes_clean": minutes_clean, "known": known, "complete_from": cf, "episodes": eps,
            "api": dict(cov) if cov else None, "stats": stats, "hours_n": hours_n, "untracked": untracked}
    prof["notes"] = notes_for(prof)
    return prof


def notes_for(p: dict) -> list:
    n, api, s, reg = [], p["api"], p["stats"], p["region"]
    cf, hours = p["complete_from"], p["hours"]
    if p["untracked"]:
        n.append("API /stats не повернуло епізодів цієї території за 7 діб. Це може означати і спокій, і те, що "
                 "територію не відстежено (для міст /alerts і /stats бувають неузгоджені). Відсутність записів НЕ є "
                 f"доказом відсутності тривог, тому історію вважаємо відомою лише від першого знімка /alerts "
                 f"({fmt_t(cf)}); статистика за довший період не обчислюється. Перевірте офіційну карту.")
    if api and api["recent_len"] < api["api_count"]:
        n.append(f"Журнал API обмежений: з {api['api_count']} епізодів за 7 діб отримано лише {api['recent_len']} "
                 f"найновіших. Історія повна від {fmt_t(cf)}; раніше значення — нижня межа (на графіку затінено).")
    if s["known_hours"] < len(hours) and not p["untracked"] and not (api and api["recent_len"] < api["api_count"]):
        n.append(f"Історія повна від {fmt_t(cf)}; години до цього моменту не покриті.")
    if s["n_suspect"]:
        n.append(f"Підозріло довгих/суперечливих епізодів: {s['n_suspect']} (позначені в таблиці). Вихідні мітки не "
                 "змінено. Частка часу в тривозі наведена і з ними, і без них.")
    if s["n_unconfirmed"]:
        n.append("Для епізодів «кінець не підтверджено» тривалість — нижня межа до останнього моменту, коли їх бачили.")
    if p["n_children"]:
        n.append(f"Це область із {p['n_children']} дочірніми територіями. Її епізоди API формує як об'єднання "
                 "епізодів районів/громад («тривога десь в області»). Вони не сумуються з даними районів: щоб "
                 f"побачити район, оберіть його окремо (--list --oblast {reg['slug']}).")
    if reg["type"] in ("community", "state") and not p["n_children"]:
        n.append("Міська/громадська одиниця: її дані окремі, у районні не підсумовуються й не підставляються.")
    if p["episodes"].empty and api and api["api_count"] > 0:
        n.append(f"У вибраному вікні ({p['hours_n']} год) епізодів немає, але за 7 діб вони були.")
    return n


# ================================== Звіт ==========================================
def fmt_dur(minutes) -> str:
    minutes = float(minutes)
    return f"{int(minutes // 60)} год {int(minutes % 60):02d} хв" if minutes >= 60 else f"{minutes:.0f} хв"


def format_report(p: dict) -> list:
    reg, s, hours = p["region"], p["stats"], p["hours"]
    L = []
    add = L.append
    add(f"ТЕРИТОРІЯ: {reg['name_uk']} ({reg['name_en']})")
    add(f"slug: {reg['slug']}; тип: {p['kind']}" + (f"; належить до: {p['parent']}" if p["parent"] else ""))
    add("Час у звіті — Europe/Kyiv (EET=UTC+2, EEST=UTC+3); у базі зберігається UTC.")
    add("Джерело: tryvoha.online/api/v1 (стороннє, неофіційне). Показано лише власні дані цієї території.")
    add("")
    add("== Доступність даних ==")
    add(f"Дані зібрано станом на {fmt_t(p['asof'])}")
    add(f"Вікно перегляду: {p['hours_n']} повних годин, {fmt_t(hours[0])} — {fmt_t(hours[-1] + HOUR)}")
    if p["api"]:
        a = p["api"]
        add(f"/stats за 7 діб: епізодів за API {a['api_count']}, отримано {a['recent_len']}"
            f" (вікно API {fmt_t(pd.Timestamp(a['api_from']))} — {fmt_t(pd.Timestamp(a['api_to']))})")
    add(f"Історія повна від: {fmt_t(p['complete_from'])}; повних годин у вікні: {s['known_hours']} із {p['hours_n']}")
    add("")
    add("== Статистика (лише години з повною історією) ==")
    if s["known_hours"]:
        add(f"Годин з тривогою: {s['hours_with_alert']} із {s['known_hours']}")
        add(f"Час у тривозі: {fmt_dur(s['alert_minutes'])} ({s['share']:.1%} часу)"
            + (f"; без підозрілих епізодів: {fmt_dur(s['alert_minutes_clean'])} ({s['share_clean']:.1%})"
               if s["n_suspect"] else ""))
    else:
        add("У вікні немає годин із повною історією, статистику не обчислено.")
    add(f"Епізодів, що перетинають вікно: {s['n_episodes']} (почалися у вікні: {s['n_started_in_window']}; "
        f"завершені: {s['n_ended']}, тривають: {s['n_ongoing']}, кінець не підтверджено: {s['n_unconfirmed']})")
    if s["ended_inside_n"]:
        add(f"Завершені епізоди, що почалися у вікні: {s['ended_inside_n']}, медіана {fmt_dur(s['ended_inside_median'])}, "
            f"найдовший {fmt_dur(s['ended_inside_max'])} (тривалі епізоди, що ще тривають, сюди не входять: зсув вниз)")
    add("")
    add("== Епізоди (час Europe/Kyiv) ==")
    eps = p["episodes"]
    if eps.empty:
        add("У вікні епізодів немає.")
    else:
        for r in eps.itertuples():
            end = fmt_t(r.end) if r.ended_at else ("триває" if r.status == "active" else f"кінець невідомий (бачили до {fmt_t(r.end)})")
            flag = f"  ⚠ {r.suspect}" if r.flagged else ""
            part = "  †" if r.before_complete else ""
            level = r.level if isinstance(r.level, str) else "—"
            add(f"  {fmt_t(r.start)} — {end:<44} {fmt_dur(r.duration_min):>12}  level={level}{flag}{part}")
        if eps["before_complete"].any():
            add("  † епізод із ділянки неповної історії: сусідні епізоди могли бути пропущені (див. примітки)")
    add("")
    add("== Примітки та обмеження ==")
    for line in p["notes"] or ["Особливих застережень немає."]:
        add("  - " + line)
    add("  - Навчальний аналіз історичних даних; не для рішень щодо безпеки. Прогноз для окремої території не будується "
        "(для цього замало подій).")
    return L


def write_outputs(p: dict, out: Path, plot=True) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    slug = p["region"]["slug"]
    text = "\n".join(format_report(p)) + "\n"
    (out / f"{slug}.txt").write_text(text, encoding="utf-8")
    pd.DataFrame({"hour_utc": p["hours"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "hour_kyiv": p["hours"].tz_convert(KYIV).strftime("%Y-%m-%d %H:%M %Z"),
                  "minutes_in_alert": p["minutes"].round(2),
                  "history_complete": p["known"]}).to_csv(out / f"{slug}_hourly.csv", index=False)
    if plot:
        import plots
        plots.plot_region(p, out / f"{slug}.png")
    print(text)
    return out


# ==================================== CLI =========================================
def choose(conn, matches, pick):
    if len(matches) == 1:
        return matches[0]
    print("Знайдено кілька територій:")
    print_list(conn, matches)
    if pick is None:
        try:
            pick = int(input("Номер території (або Enter для виходу): ").strip() or 0)
        except (EOFError, ValueError):
            pick = 0
    if not 1 <= pick <= len(matches):
        print("Територію не вибрано. Вкажіть точний slug або --pick N.")
        return None
    return matches[pick - 1]


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):  # при перенаправленні Windows бере cp1251, де немає ≥, ⚠ тощо
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("query", nargs="?", help="slug або частина назви території")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default="outputs/regions")
    p.add_argument("--list", action="store_true", help="показати доступні території (з фільтрами)")
    p.add_argument("--type", choices=["state", "district", "community"])
    p.add_argument("--oblast", help="slug області: показати лише її райони/громади")
    p.add_argument("--hours", type=int, default=DEFAULT_HOURS, help=f"вікно в годинах (до {MAX_HOURS})")
    p.add_argument("--pick", type=int, help="номер зі списку, якщо збігів кілька")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args(argv)
    if not 1 <= args.hours <= MAX_HOURS:
        p.error(f"--hours має бути від 1 до {MAX_HOURS} (глибше історії в API немає)")
    if not Path(args.db).exists():
        print(f"Базу {args.db} не знайдено. Спочатку: python collect.py backfill")
        return 1
    conn = db.connect(args.db)

    if args.list:
        print_list(conn, find_regions(conn, args.query, args.type, args.oblast))
        return 0
    query = args.query
    if not query:
        try:
            query = input("Назва або slug території (напр. харків): ").strip()
        except EOFError:
            query = ""
    matches = find_regions(conn, query, args.type, args.oblast) if query else []
    if not matches:
        print("Територію не знайдено. Подивіться перелік: python region.py --list")
        return 2
    chosen = choose(conn, matches, args.pick)
    if chosen is None:
        return 2
    write_outputs(region_profile(conn, chosen["slug"], args.hours), Path(args.out), plot=not args.no_plot)
    return 0


if __name__ == "__main__":
    sys.exit(main())

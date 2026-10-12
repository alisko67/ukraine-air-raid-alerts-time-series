"""Аналіз: SQLite -> погодинний ряд -> описова статистика -> baseline та AR(1) -> звіт і графіки.

Запуск: python analysis.py [--db data/alarms.sqlite] [--out outputs]

Показник (один основний): кількість «листових» територіальних одиниць (райони, громади,
міста/області без районів), у яких у цю годину діяла тривога (будь-який епізод перетинає
годину). Області, що мають райони, не беруться: їхні епізоди є об'єднанням районних.

Час: у базі UTC; усі години й графіки показуються в Europe/Kyiv. Межі годин у UTC і
Києві збігаються (зсув цілий), тому групування по годинах однакове.

Головне обмеження джерела: журнал подій обмежений ~20 останніми епізодами на регіон.
Для кожної одиниці ми знаємо момент `complete_from`, з якого її історія повна; до нього
значення ряду є лише нижньою межею. Звідси два ряди (див. build_dataset).
"""
import argparse
import hashlib
import sqlite3
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

import db
from timeutil import KYIV

# --- Параметри, зафіксовані ДО перегляду метрик прогнозу -----------------------------
WINDOW_HOURS = 72          # повних годин до моменту останнього збору
COVERAGE_MIN = 0.95        # основний ряд: лише години, де історія повна для >=95% одиниць
TEST_HOURS_PRIMARY = 12    # відкладений кінець основного ряду
TEST_HOURS_PANEL = 24      # відкладений кінець контрольного ряду
MIN_TRAIN = 24             # менше годин для навчання — не оцінюємо
HOUR = pd.Timedelta(hours=1)


# =============================== Побудова даних ===================================
@dataclass
class Dataset:
    hours: pd.DatetimeIndex      # початки годин, UTC
    units: list                  # slug листових одиниць
    active: np.ndarray           # [одиниця, година] True якщо була тривога
    known: np.ndarray            # [одиниця, година] True якщо історія одиниці в цю годину повна
    asof: pd.Timestamp           # момент, станом на який зібрані дані
    n_stuck_excluded: int        # «завислі» епізоди, виключені з ряду
    stuck_units: list
    fingerprint: str             # хеш вхідних епізодів для перевірки відтворюваності
    n_episodes: int


def leaf_units(conn) -> list:
    rows = conn.execute("SELECT slug, oblast_slug FROM regions").fetchall()
    parents = {r["oblast_slug"] for r in rows if r["oblast_slug"]}
    return sorted(r["slug"] for r in rows if r["slug"] not in parents)


def data_asof(conn) -> pd.Timestamp:
    """Найраніший з «останніх успішних знімків /stats» по регіонах: до цього моменту
    дані консистентні для всіх регіонів."""
    row = conn.execute(
        "SELECT MIN(m) FROM (SELECT MAX(fetched_at) m FROM snapshots"
        " WHERE ok=1 AND endpoint LIKE '/stats/%' GROUP BY endpoint)").fetchone()
    if row[0] is None:
        raise SystemExit("У базі немає знімків /stats. Спочатку: python collect.py backfill")
    return pd.Timestamp(row[0])


def complete_from(conn, units) -> pd.Series:
    """З якого моменту історія одиниці повна (UTC).
    Якщо API віддав усі епізоди вікна — з початку вікна; інакше — з початку найстарішого
    отриманого епізоду (усе раніше невідоме, бо `recent` містить лише найновіші)."""
    rows = conn.execute(
        "SELECT c.slug, c.api_count, c.recent_len, c.oldest_recent_started_at old, s.api_from"
        " FROM coverage c JOIN snapshots s ON s.id = c.snapshot_id"
        " WHERE c.snapshot_id = (SELECT MAX(snapshot_id) FROM coverage c2 WHERE c2.slug = c.slug)"
    ).fetchall()
    out = {}
    for r in rows:
        out[r["slug"]] = r["api_from"] if r["recent_len"] >= r["api_count"] else r["old"]
    missing = [u for u in units if u not in out]
    if missing:
        raise SystemExit(f"Немає даних про покриття для: {missing[:5]}...")
    return pd.Series({u: pd.Timestamp(out[u]) for u in units})


def load_episodes(conn, units, asof, exclude_stuck=True):
    """Епізоди листових одиниць із ефективним кінцем.
    Кінець: ended_at (факт від API); для active — момент збору (asof); для end_unconfirmed —
    останній момент, коли епізод бачили (нижня межа, бо кінець невідомий).
    Епізоди з позначкою long_active (понад добу без жодного кінця) виключаємо з ряду:
    вони дають константний зсув і не відрізняються від застарілих записів."""
    df = pd.read_sql_query(
        "SELECT slug, started_at, ended_at, status, last_seen_at, suspect, level, in_alerts, in_stats"
        " FROM episodes", conn)
    df = df[df["slug"].isin(units)].copy()
    stuck = df["suspect"].fillna("").str.contains("long_active")
    stuck_units = sorted(df.loc[stuck, "slug"].unique())
    n_stuck = int(stuck.sum())
    if exclude_stuck:                       # для окремої території (region.py) їх лишаємо й позначаємо
        df = df[~stuck]
    df["start"] = pd.to_datetime(df["started_at"], utc=True)
    end = pd.to_datetime(df["ended_at"], utc=True)
    unconfirmed = pd.to_datetime(df["last_seen_at"], utc=True)
    df["end"] = end.where(end.notna(), unconfirmed.where(df["status"] == "end_unconfirmed", asof))
    df["end"] = df["end"].clip(upper=asof)
    return df, n_stuck, stuck_units


def fingerprint(conn) -> str:
    h = hashlib.sha256()
    for r in conn.execute("SELECT slug, started_at, ended_at FROM episodes ORDER BY slug, started_at"):
        h.update(f"{r[0]}|{r[1]}|{r[2]};".encode())
    return h.hexdigest()[:16]


def unit_hour_matrix(episodes, units, hours) -> np.ndarray:
    """[одиниця, година] = True, якщо якийсь епізод одиниці перетинає годину [h, h+1).
    Кілька епізодів в одній годині рахуються один раз (булева матриця)."""
    idx = {u: i for i, u in enumerate(units)}
    active = np.zeros((len(units), len(hours)), dtype=bool)
    h0 = hours[0]
    for r in episodes.itertuples():
        first = max(0, int(np.floor((r.start - h0) / HOUR)))
        last = min(len(hours) - 1, int(np.ceil((r.end - h0) / HOUR)) - 1)
        if last >= first:
            active[idx[r.slug], first:last + 1] = True
    return active


def build_dataset(conn) -> Dataset:
    units = leaf_units(conn)
    asof = data_asof(conn)
    end_h = asof.floor("h")                       # остання година неповна, її не беремо
    hours = pd.date_range(end_h - WINDOW_HOURS * HOUR, end_h - HOUR, freq="h")
    episodes, n_stuck, stuck_units = load_episodes(conn, units, asof)
    cf = complete_from(conn, units)

    active = unit_hour_matrix(episodes, units, hours)
    # історія одиниці в годину h повна, якщо вся година лежить після complete_from
    known = np.array([[cf[u] <= h for h in hours] for u in units], dtype=bool)
    return Dataset(hours, units, active, known, asof, n_stuck, stuck_units,
                   fingerprint(conn), len(episodes))


def make_series(ds: Dataset) -> dict:
    """Три ряди з одного показника «одиниць у тривозі за годину»."""
    coverage = pd.Series(ds.known.mean(axis=0), index=ds.hours)
    full = pd.Series(ds.active.sum(axis=0), index=ds.hours)
    primary_mask = coverage >= COVERAGE_MIN
    panel_units = ds.known[:, 0]                        # повна історія від початку вікна
    panel = pd.Series(ds.active[panel_units].sum(axis=0), index=ds.hours)
    return {"full": full, "coverage": coverage, "primary": full[primary_mask],
            "panel": panel, "n_panel_units": int(panel_units.sum())}


# ============================== Описова статистика ================================
def describe(s: pd.Series, n_units: int) -> dict:
    d = s.diff().abs().dropna()
    return {
        "n_hours": len(s),
        "from_kyiv": s.index[0].tz_convert(KYIV), "to_kyiv": s.index[-1].tz_convert(KYIV),
        "mean": s.mean(), "std": s.std(), "min": s.min(), "median": s.median(),
        "p95": s.quantile(0.95), "max": s.max(),
        "peak_hour_kyiv": s.idxmax().tz_convert(KYIV),
        "zero_hours_share": (s == 0).mean(),
        "autocorr_lag1": s.autocorr(lag=1),
        "mean_abs_change": d.mean(),
        "biggest_jumps": s.diff().abs().nlargest(3).dropna(),
        "n_units": n_units,
    }


# ================================== Прогноз =======================================
def ar1_forecast(train: np.ndarray):
    """AR(1) із константою, перенавчається на кожному origin лише на train. Порядок
    зафіксований заздалегідь (не підбирався за тестовою помилкою). Повертає (прогноз, збій)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")          # попередження статмоделі вважаємо збоєм
            fit = ARIMA(train, order=(1, 0, 0), trend="c").fit()
        return float(fit.forecast(1)[0]), False
    except Exception:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                return float(ARIMA(train, order=(1, 0, 0), trend="c").fit().forecast(1)[0]), True
            except Exception:
                return float("nan"), True


def rolling_one_step(y: np.ndarray, test_n: int, upper: float) -> pd.DataFrame:
    """Прогноз на 1 годину вперед для кожної з останніх test_n годин.
    Для прогнозу години t використовується ЛИШЕ y[:t] (зріз), тому майбутнє недоступне."""
    rows = []
    for t in range(len(y) - test_n, len(y)):
        train = y[:t]
        ar, warned = ar1_forecast(train)
        rows.append({"pos": t, "actual": y[t], "naive": train[-1], "mean": train.mean(),
                     "ar1": float(np.clip(ar, 0, upper)), "ar1_warned": warned})
    return pd.DataFrame(rows)


def evaluate(series: pd.Series, test_n: int, n_units: int) -> dict:
    y = series.to_numpy(dtype=float)
    if len(y) - test_n < MIN_TRAIN:
        return {"enough": False, "n": len(y), "train_n": len(y) - test_n, "test_n": test_n}
    fc = rolling_one_step(y, test_n, upper=n_units)
    fc.index = series.index[fc["pos"]]
    metrics = {}
    for m in ("naive", "mean", "ar1"):
        e = (fc[m] - fc["actual"]).abs()
        metrics[m] = {"mae": e.mean(), "rmse": float(np.sqrt(((fc[m] - fc["actual"]) ** 2).mean()))}
    e_n, e_a = (fc["naive"] - fc["actual"]).abs(), (fc["ar1"] - fc["actual"]).abs()
    return {"enough": True, "n": len(y), "train_n": len(y) - test_n, "test_n": test_n,
            "forecasts": fc, "metrics": metrics,
            "ar1_better_hours": int((e_a < e_n).sum()), "naive_better_hours": int((e_n < e_a).sum()),
            "ties": int((e_a == e_n).sum()), "ar1_warned": int(fc["ar1_warned"].sum())}


# =================================== Звіт ========================================
def fmt_t(ts) -> str:
    return ts.tz_convert(KYIV).strftime("%Y-%m-%d %H:%M %Z") if ts.tzinfo else str(ts)


def build_report(ds: Dataset, S: dict, stats: dict, evals: dict) -> list:
    L = []
    add = L.append
    add("АНАЛІЗ ПОГОДИННОГО РЯДУ ПОВІТРЯНИХ ТРИВОГ (навчальне дослідження)")
    add("Джерело: tryvoha.online/api/v1 (стороннє, неофіційне, дані за умовами сервісу з посиланням на нього).")
    add("Час у звіті й на графіках — Europe/Kyiv (EET=UTC+2, EEST=UTC+3); у базі зберігається UTC.")
    add("")
    add("== 1. Вхідні дані ==")
    add(f"Дані зібрано станом на {fmt_t(ds.asof)}; відбиток вхідних епізодів: {ds.fingerprint}")
    add(f"Епізодів листових одиниць у розрахунку: {ds.n_episodes}; одиниць (райони, громади, міста без районів): {len(ds.units)}")
    add(f"Вікно аналізу: {WINDOW_HOURS} повних годин, {fmt_t(ds.hours[0])} — {fmt_t(ds.hours[-1] + HOUR)}")
    add(f"Виключено «завислих» епізодів (>24 год без відомого кінця): {ds.n_stuck_excluded} в одиницях {ds.stuck_units}. "
        f"Це константний зсув рівня ряду; на помилки прогнозу майже не впливає, але рівні (середнє, максимум) зсунуті.")
    add("Показник: кількість одиниць, у яких у цю годину діяла тривога. Області з районами не рахуються (подвійний облік).")
    add("Припущення (не перевірене): листові одиниці просторово не перетинаються.")
    cov = S["coverage"]
    add("")
    add("== 2. Повнота історії (головне обмеження) ==")
    add("API віддає лише ~20 останніх епізодів на регіон. Для кожної одиниці відомий момент, з якого її історія повна;")
    add("раніше значення ряду — лише нижня межа (реальних тривог було не менше, ніж видно).")
    for thr in (0.5, 0.8, 0.95, 0.99, 1.0):
        ok = cov[cov >= thr]
        add(f"  Година з повною історією для ≥{int(thr * 100)}% одиниць: " + (fmt_t(ok.index[0]) if len(ok) else "—"))
    add(f"Основний ряд: години з покриттям ≥{int(COVERAGE_MIN * 100)}% — {len(S['primary'])} год. "
        f"Решта {WINDOW_HOURS - len(S['primary'])} год із вікна — занижені, у прогнозі НЕ використовуються.")
    add(f"Контрольний ряд: фіксована панель із {S['n_panel_units']} одиниць, чия історія повна за всі {WINDOW_HOURS} год. "
        "УВАГА: у панель потрапляють переважно регіони з меншою кількістю тривог (активні регіони обрізані "
        "журналом), тож панель не репрезентує всю країну; вона потрібна лише для перевірки стійкості висновку.")
    add("")
    add("== 3. Описова статистика ==")
    for name, key in (("Основний ряд (усі одиниці, години з повною історією)", "primary"),
                      ("Контрольний ряд (фіксована панель, 72 год)", "panel")):
        d = stats[key]
        add(f"{name}:")
        add(f"  годин {d['n_hours']} ({fmt_t(d['from_kyiv'])} — {fmt_t(d['to_kyiv'])}), одиниць {d['n_units']}")
        add(f"  середнє {d['mean']:.1f}, ст.відх. {d['std']:.1f}, мін {d['min']:.0f}, медіана {d['median']:.0f}, "
            f"p95 {d['p95']:.0f}, макс {d['max']:.0f} (пік {fmt_t(d['peak_hour_kyiv'])})")
        add(f"  частка годин без тривог {d['zero_hours_share']:.0%}; автокореляція (лаг 1 год) {d['autocorr_lag1']:.2f}; "
            f"середня зміна за годину {d['mean_abs_change']:.1f}")
        add("  найрізкіші стрибки: " + "; ".join(f"{fmt_t(t)}: {v:.0f}" for t, v in d["biggest_jumps"].items()))
    add(f"Висновки про дні тижня чи сезонність не робляться: основний ряд охоплює {len(S['primary'])} год (~{len(S['primary']) / 24:.1f} доби), "
        f"контрольний — {WINDOW_HOURS} год (3 доби); повторюваність за днями тижня перевірити неможливо.")
    add("")
    add("== 4. Прогноз: baseline та AR(1) ==")
    add("Ціль: кількість одиниць у тривозі в наступну годину (горизонт 1 год). Розбиття за часом: навчання — раніші")
    add("години, відкладений тест — найновіші. Для кожної тестової години модель бачить лише попередні години")
    add("(rolling origin, AR(1) перенавчається на кожному кроці; порядок (1,0,0) вибрано заздалегідь, не за помилкою на тесті).")
    add("Методи: naive (значення = останнє відоме), mean (середнє відомих годин), AR(1) із константою (statsmodels ARIMA).")
    for name, key in (("Основний ряд", "primary"), ("Контрольний ряд (панель)", "panel")):
        e = evals[key]
        add("")
        add(f"{name}: годин {e['n']} (навчання {e['train_n']}, тест {e['test_n']})")
        if not e["enough"]:
            add(f"  Даних недостатньо: для навчання потрібно щонайменше {MIN_TRAIN} год. Оцінка не виконувалась.")
            continue
        add(f"  {'метод':<8}{'MAE':>8}{'RMSE':>8}")
        for m, label in (("naive", "naive"), ("mean", "mean"), ("ar1", "AR(1)")):
            add(f"  {label:<8}{e['metrics'][m]['mae']:>8.2f}{e['metrics'][m]['rmse']:>8.2f}")
        add(f"  AR(1) точніший за naive у {e['ar1_better_hours']} год, гірший у {e['naive_better_hours']}, однаково у {e['ties']}"
            f"; збоїв/попереджень збіжності AR(1): {e['ar1_warned']} із {e['test_n']}")
    add("")
    add("== 5. Висновок ==")
    add(conclusion(evals))
    add("")
    add("== 6. Обмеження ==")
    for line in LIMITS:
        add("  - " + line)
    return L


LIMITS = [
    "Історія джерела неповна: журнал подій обмежений ~20 епізодами на регіон; для активних регіонів години до їх 'complete_from' занижені.",
    "Надійних годин лише кілька десятків; відкладений тест — 12 і 24 точки. Це дослідницька оцінка, а не доведена якість моделі.",
    "Різниця в MAE на такій вибірці не є статистично підтвердженою; інший період міг би дати інший результат.",
    "Основний ряд охоплює ~1,6 доби, контрольний — 3 доби: сезонність, дні тижня та довготривалі закономірності оцінити неможливо.",
    "Дані сторонні й неофіційні; між ендпоїнтами спостерігались розбіжності (напр., місто Запоріжжя); область не рахується разом з районами.",
    "Це навчальний аналіз історичних даних; не можна використовувати для рішень щодо безпеки.",
]


def conclusion(evals: dict) -> str:
    parts = []
    verdict = []
    for name, key in (("основному", "primary"), ("контрольному", "panel")):
        e = evals[key]
        if not e["enough"]:
            parts.append(f"На {name} ряді даних недостатньо для оцінки.")
            continue
        m = e["metrics"]
        better = m["ar1"]["mae"] < m["naive"]["mae"]
        verdict.append(better)
        parts.append(f"На {name} ряді MAE: naive {m['naive']['mae']:.2f}, mean {m['mean']['mae']:.2f}, "
                     f"AR(1) {m['ar1']['mae']:.2f} — AR(1) {'дає менший' if better else 'не дає меншого'} MAE, ніж naive.")
    if len(verdict) == 2 and verdict[0] == verdict[1]:
        parts.append("Напрям результату збігається на обох рядах, але вибірка мала, тож це лише обережна ознака, а не доказ "
                     "переваги моделі; складніший метод не можна вважати надійно кращим, а прогноз — надійним.")
    elif len(verdict) == 2:
        parts.append("Напрям результату на двох рядах НЕ збігається, тобто перевага складнішого методу не підтверджується.")
    return " ".join(parts)


# ==================================== CLI ========================================
def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):  # при перенаправленні Windows бере cp1251, де немає ≥, ⚠ тощо
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/alarms.sqlite")
    p.add_argument("--out", default="outputs")
    args = p.parse_args(argv)
    if not Path(args.db).exists():
        raise SystemExit(f"Базу {args.db} не знайдено. Спочатку: python collect.py backfill")
    conn = db.connect(args.db)
    ds = build_dataset(conn)
    S = make_series(ds)
    stats = {"primary": describe(S["primary"], len(ds.units)),
             "panel": describe(S["panel"], S["n_panel_units"])}
    evals = {"primary": evaluate(S["primary"], TEST_HOURS_PRIMARY, len(ds.units)),
             "panel": evaluate(S["panel"], TEST_HOURS_PANEL, S["n_panel_units"])}
    lines = build_report(ds, S, stats, evals)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    (out / "report.txt").write_text(text, encoding="utf-8")
    hourly = pd.DataFrame({"hour_utc": S["full"].index.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "hour_kyiv": S["full"].index.tz_convert(KYIV).strftime("%Y-%m-%d %H:%M %Z"),
                           "units_in_alert_all": S["full"].to_numpy(),
                           "coverage_share": S["coverage"].round(4).to_numpy(),
                           "in_primary_series": (S["coverage"] >= COVERAGE_MIN).to_numpy(),
                           "units_in_alert_panel": S["panel"].to_numpy()})
    hourly.to_csv(out / "series_hourly.csv", index=False)
    for key in ("primary", "panel"):
        if evals[key]["enough"]:
            fc = evals[key]["forecasts"].drop(columns=["pos"]).copy()
            fc.index = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in fc.index]
            fc.index.name = "hour_utc"
            fc.round(4).to_csv(out / f"forecasts_{key}.csv")
    try:
        import plots
        plots.make_all(S, evals, ds, out)
    except ImportError as e:  # matplotlib не встановлено — звіт усе одно готовий
        print(f"Графіки пропущено: {e}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

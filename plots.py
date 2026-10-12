"""Графіки до analysis.py. Усі часові осі — Europe/Kyiv. Кольори: перші три слоти
валідованої палітри (blue/orange/aqua), фактичні значення — нейтральним чорнилом."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from timeutil import KYIV

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e3e2dd"
BLUE, ORANGE, GRAY_FILL = "#2a78d6", "#eb6834", "#c3c2b7"
SURFACE = "#fcfcfb"
TZ_LABEL = "Київський час (Europe/Kyiv)"


def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def _time_axis(ax, every=6):
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=every, tz=KYIV))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M", tz=KYIV))


def plot_series(S, ds, path):
    from analysis import COVERAGE_MIN, HOUR
    full, cov, primary = S["full"], S["coverage"], S["primary"]
    fig, (ax, axc) = plt.subplots(2, 1, figsize=(11, 6.2), sharex=True, facecolor=SURFACE,
                                  gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12})
    # step «post»: значення години h діє на інтервалі [h, h+1)
    cut = primary.index[0]
    ax.axvspan(full.index[0], cut, color=GRAY_FILL, alpha=0.35, linewidth=0)
    ax.step(full.index, full.values, where="post", color=INK, linewidth=1.6)
    ax.set_ylim(0, full.max() * 1.22)             # запас зверху для підписів, щоб не перетиналися з рядом
    top = ax.get_xaxis_transform()               # x у даних, y у частках висоти осі
    ax.text(full.index[0] + HOUR, 0.97, "неповна історія: значення занижені\n(нижня межа, у прогнозі не використано)",
            va="top", fontsize=9, color=MUTED, transform=top)
    ax.axvline(cut, color=MUTED, linewidth=0.8, linestyle=":")
    ax.text(cut + HOUR, 0.97, f"основний ряд: покриття ≥{int(COVERAGE_MIN * 100)}%",
            va="top", fontsize=9, color=MUTED, transform=top)
    ax.set_ylabel("одиниць у тривозі за годину", color=MUTED)
    ax.set_title("Скільки районів/громад були під тривогою щогодини (останні 72 повні години)",
                 loc="left", color=INK, fontsize=12)
    _style(ax)
    axc.step(cov.index, cov.values * 100, where="post", color=BLUE, linewidth=1.6)
    axc.axhline(COVERAGE_MIN * 100, color=MUTED, linewidth=0.8, linestyle=":")
    axc.set_ylim(0, 105)
    axc.set_ylabel("повна історія, % одиниць", color=MUTED, fontsize=9)
    axc.set_xlabel(TZ_LABEL, color=MUTED)
    _style(axc)
    _time_axis(axc)
    fig.text(0.01, 0.005, f"Дані: tryvoha.online (неофіційне джерело), станом на "
             f"{ds.asof.tz_convert(KYIV).strftime('%Y-%m-%d %H:%M %Z')}. Журнал подій обмежений ~20 епізодами на регіон.",
             fontsize=8, color=MUTED)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


def plot_forecasts(S, evals, path):
    keys = [(k, t) for k, t in (("primary", "Основний ряд (усі одиниці)"),
                                ("panel", "Контрольний ряд (панель; не репрезентує всю країну)"))
            if evals[k]["enough"]]
    if not keys:
        return
    fig, axes = plt.subplots(len(keys), 1, figsize=(11, 3.9 * len(keys)), facecolor=SURFACE, squeeze=False)
    for ax, (k, title) in zip(axes[:, 0], keys):
        series, e = S[k], evals[k]
        fc, m = e["forecasts"], e["metrics"]
        tail = series.iloc[max(0, e["train_n"] - 12):]
        ax.step(tail.index, tail.values, where="post", color=INK, linewidth=1.8, label="фактично")
        ax.step(fc.index, fc["naive"], where="post", color=ORANGE, linewidth=1.5,
                label=f"baseline naive (MAE {m['naive']['mae']:.2f})")
        ax.step(fc.index, fc["ar1"], where="post", color=BLUE, linewidth=1.5,
                label=f"AR(1) (MAE {m['ar1']['mae']:.2f})")
        ax.axvline(fc.index[0], color=MUTED, linewidth=0.8, linestyle=":")
        ax.text(fc.index[0], ax.get_ylim()[1], " тестовий відрізок →", va="top", fontsize=9, color=MUTED)
        ax.set_title(f"{title}: прогноз на 1 год вперед (навчання {e['train_n']} год, тест {e['test_n']} год)",
                     loc="left", color=INK, fontsize=11)
        ax.set_ylabel("одиниць у тривозі", color=MUTED)
        ax.legend(frameon=False, fontsize=9, loc="upper left", bbox_to_anchor=(0, 0.9))
        _style(ax)
        _time_axis(ax, every=3 if k == "primary" else 4)
    axes[-1, 0].set_xlabel(TZ_LABEL, color=MUTED)
    fig.text(0.01, -0.005, "Вибірка мала: різниця метрик — орієнтовна, не доведена перевага моделі.",
             fontsize=8, color=MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


def make_all(S, evals, ds, out):
    plot_series(S, ds, out / "01_series.png")
    plot_forecasts(S, evals, out / "02_forecasts.png")


def plot_region(p, path):
    """Графік однієї території: хронологія епізодів + хвилини в тривозі за годину.
    Дві окремі осі (різні величини), спільна вісь часу; неповна історія затінена."""
    from matplotlib.patches import Patch
    from analysis import HOUR
    hours, minutes, known, eps = p["hours"], p["minutes"], p["known"], p["episodes"]
    reg, s = p["region"], p["stats"]
    fig, (axt, axb) = plt.subplots(2, 1, figsize=(11, 5.6), sharex=True, facecolor=SURFACE,
                                   gridspec_kw={"height_ratios": [1, 2], "hspace": 0.1})
    x0, x1 = hours[0], hours[-1] + HOUR
    unknown_to = next((h for h, k in zip(hours, known) if k), x1)
    for ax in (axt, axb):
        if unknown_to > x0:
            ax.axvspan(x0, unknown_to, color=GRAY_FILL, alpha=0.35, linewidth=0)

    # --- хронологія епізодів
    color = {"ended": BLUE, "active": ORANGE, "end_unconfirmed": ORANGE}
    for r in eps.itertuples():
        a, b = max(r.start, x0), min(r.end, x1)
        if b <= a:
            continue
        w = mdates.date2num(b) - mdates.date2num(a)
        axt.broken_barh([(mdates.date2num(a), w)], (0.2, 0.6), facecolor=color.get(r.status, BLUE),
                        edgecolor=SURFACE, linewidth=1, hatch="///" if r.flagged else None)
    axt.set_ylim(0, 1)
    axt.set_yticks([])
    axt.set_ylabel("епізоди", color=MUTED)
    handles = [Patch(color=BLUE, label="завершена (є ended_at)"),
               Patch(color=ORANGE, label="триває / кінець не підтверджено")]
    if s["n_suspect"]:
        handles.append(Patch(facecolor=SURFACE, edgecolor=MUTED, hatch="///", label="підозріло довга (>24 год)"))
    axt.legend(handles=handles, frameon=False, fontsize=8, ncol=len(handles), loc="lower left",
               bbox_to_anchor=(0, 1.0))
    if eps.empty:
        axt.text(0.5, 0.5, "епізодів у вікні немає — це не доказ відсутності тривог", ha="center", va="center",
                 transform=axt.transAxes, fontsize=10, color=MUTED)
    _style(axt)
    axt.grid(False)

    # --- хвилини в тривозі за годину
    xs = mdates.date2num(hours.to_pydatetime())
    colors = [BLUE if k else GRAY_FILL for k in known]
    axb.bar(xs, minutes, width=1 / 24, align="edge", color=colors, edgecolor=SURFACE, linewidth=0.6)
    axb.set_ylim(0, 62)
    axb.set_yticks([0, 15, 30, 45, 60])
    axb.set_ylabel("хвилин у тривозі за годину", color=MUTED)
    axb.set_xlim(mdates.date2num(x0.to_pydatetime()), mdates.date2num(x1.to_pydatetime()))
    if unknown_to > x0:
        axb.text(mdates.date2num(x0.to_pydatetime()) + 0.02, 59, "неповна історія: значення занижені",
                 va="top", fontsize=9, color=MUTED)
    if s["n_suspect"]:
        axb.text(0.99, 0.04, "⚠ у значеннях є підозріло довгий епізод (штриховка вгорі)", transform=axb.transAxes,
                 ha="right", va="bottom", fontsize=9, color=INK,
                 bbox={"facecolor": SURFACE, "edgecolor": GRID, "alpha": 0.9, "boxstyle": "round,pad=0.3"})
    axb.set_xlabel(TZ_LABEL, color=MUTED)
    _style(axb)
    _time_axis(axb, every=3 if p["hours_n"] <= 48 else 6 if p["hours_n"] <= 96 else 12)
    axt.set_title(f"{reg['name_uk']}: повітряні тривоги за останні {p['hours_n']} год",
                  loc="left", color=INK, fontsize=12, pad=26)
    fig.text(0.01, -0.01, f"Дані: tryvoha.online (неофіційне джерело), станом на "
             f"{p['asof'].tz_convert(KYIV).strftime('%Y-%m-%d %H:%M %Z')}. Лише власні дані території, без об'єднання "
             f"з іншими рівнями.", fontsize=8, color=MUTED)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)

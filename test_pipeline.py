"""Перевірки без мережі: дедуплікація, статуси, підозрілі записи, повтори клієнта, київський час.

Дані в тестах штучні, але мають ту саму структуру, що й підтверджені відповіді API.
Запуск: python -m unittest -v
"""
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import client as client_mod
import db
from client import ApiError, Response, TryvohaClient
from timeutil import fmt_kyiv, to_kyiv

T0 = "2026-10-11T21:39:07.000000Z"
T1 = "2026-10-11T22:21:19.000000Z"


def alerts_resp(items, fetched="2026-10-11T23:00:00.000000Z"):
    return Response(200, {"updated_at": fetched, "active": len(items), "alerts": items}, fetched)


def alert(slug="xarkivskii-raion", started=T0, level="red"):
    return {"slug": slug, "type": "district", "level": level, "started_at": started}


def stats_resp(episodes, count=None, fetched="2026-10-11T23:05:00.000000Z"):
    recent = [{"started_at": s, "ended_at": e, "minutes": m} for s, e, m in episodes]
    return Response(200, {"period": "7d", "from": "2026-10-04T23:05:00.000000Z", "to": fetched,
                          "alerts": {"count": count if count is not None else len(recent),
                                     "recent": recent}}, fetched)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def episodes(self):
        return self.conn.execute("SELECT * FROM episodes ORDER BY started_at").fetchall()

    def test_repeated_snapshots_do_not_duplicate(self):
        for _ in range(3):
            db.record_alerts(self.conn, alerts_resp([alert()]))
        self.assertEqual(len(self.episodes()), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 3)

    def test_stats_sets_ended_at_and_keeps_started_at(self):
        db.record_alerts(self.conn, alerts_resp([alert()]))
        db.record_stats(self.conn, "xarkivskii-raion", "7d", stats_resp([(T0, T1, 42)]))
        (ep,) = self.episodes()
        self.assertEqual((ep["started_at"], ep["ended_at"], ep["status"]), (T0, T1, "ended"))
        self.assertEqual(ep["level"], "red")  # level з /alerts не губиться

    def test_ended_at_not_overwritten_by_null(self):
        db.record_stats(self.conn, "r", "7d", stats_resp([(T0, T1, 42)]))
        db.record_stats(self.conn, "r", "7d", stats_resp([(T0, None, 42)]))
        self.assertEqual(self.episodes()[0]["ended_at"], T1)

    def test_disappearing_from_alerts_is_not_an_end(self):
        db.record_alerts(self.conn, alerts_resp([alert()], "2026-10-11T23:00:00.000000Z"))
        db.record_alerts(self.conn, alerts_resp([], "2026-10-11T23:01:00.000000Z"))
        (ep,) = self.episodes()
        self.assertEqual(ep["status"], "end_unconfirmed")
        self.assertIsNone(ep["ended_at"])

    def test_unconfirmed_then_confirmed_by_stats(self):
        db.record_alerts(self.conn, alerts_resp([alert()]))
        db.record_alerts(self.conn, alerts_resp([]))
        db.record_stats(self.conn, "xarkivskii-raion", "7d", stats_resp([(T0, T1, 42)]))
        (ep,) = self.episodes()
        self.assertEqual((ep["status"], ep["ended_at"]), ("ended", T1))

    def test_failed_snapshot_does_not_demote_active(self):
        db.record_alerts(self.conn, alerts_resp([alert()]))
        db.record_failure(self.conn, "2026-10-11T23:01:00.000000Z", "/alerts", "timeout")
        db.record_alerts(self.conn, alerts_resp([alert()]))
        self.assertEqual(self.episodes()[0]["status"], "active")

    def test_one_second_mismatch_between_endpoints_is_one_episode(self):
        alt = "2026-10-11T21:39:08.000000Z"  # /stats на 1 с пізніше, ніж /alerts
        db.record_alerts(self.conn, alerts_resp([alert()]))
        db.record_stats(self.conn, "xarkivskii-raion", "7d", stats_resp([(alt, T1, 42)]))
        (ep,) = self.episodes()
        self.assertEqual((ep["started_at"], ep["started_at_alt"]), (T0, alt))  # первинна мітка збережена
        self.assertEqual((ep["ended_at"], ep["status"], ep["in_alerts"], ep["in_stats"]), (T1, "ended", 1, 1))

    def test_distinct_episodes_far_apart_stay_separate(self):
        db.record_stats(self.conn, "r", "7d", stats_resp([(T0, T1, 42), ("2026-10-11T23:00:00.000000Z", None, 5)]))
        self.assertEqual(len(self.episodes()), 2)

    def test_coverage_reports_truncation(self):
        db.record_stats(self.conn, "r", "7d", stats_resp([(T0, T1, 42)], count=68))
        row = self.conn.execute("SELECT * FROM coverage").fetchone()
        self.assertEqual((row["api_count"], row["recent_len"], row["oldest_recent_started_at"]),
                         (68, 1, T0))

    def test_suspicious_flags_do_not_touch_timestamps(self):
        db.record_alerts(self.conn, alerts_resp([alert("luhanska", "2022-04-04T16:45:00.000000Z")]))
        db.record_stats(self.conn, "r", "7d", stats_resp([(T0, T1, 99)]))  # 99 хв != 42 хв
        db.flag_suspicious(self.conn, "2026-10-12T00:00:00.000000Z")
        rows = {r["slug"]: r for r in self.episodes()}
        self.assertEqual(rows["luhanska"]["suspect"], "long_active")
        self.assertEqual(rows["luhanska"]["started_at"], "2022-04-04T16:45:00.000000Z")
        self.assertEqual(rows["r"]["suspect"], "minutes_mismatch")


class FakeHttp:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClientTests(unittest.TestCase):
    def make(self):
        self.sleeps = []
        return TryvohaClient(min_interval=0, max_retries=3, sleep=self.sleeps.append)

    def test_retries_empty_body_and_network_error_then_succeeds(self):
        seq = [FakeHttp(body=b""), URLError("boom"), FakeHttp(body=b'{"ok": 1}')]
        with patch.object(client_mod, "urlopen", side_effect=seq):
            self.assertEqual(self.make().get("/x").data, {"ok": 1})

    def test_client_error_is_not_retried(self):
        err = HTTPError("u", 404, "nf", {}, io.BytesIO(b""))
        with patch.object(client_mod, "urlopen", side_effect=err) as m:
            with self.assertRaises(ApiError) as ctx:
                self.make().get("/x")
        self.assertEqual((ctx.exception.status, m.call_count), (404, 1))

    def test_gives_up_after_retries(self):
        with patch.object(client_mod, "urlopen", side_effect=URLError("down")) as m:
            with self.assertRaises(ApiError):
                self.make().get("/x")
        self.assertEqual(m.call_count, 4)

    def test_missing_ended_at_key_is_rejected(self):
        body = b'{"from":"a","to":"b","alerts":{"count":1,"recent":[{"started_at":"x"}]}}'
        with patch.object(client_mod, "urlopen", return_value=FakeHttp(body=body)):
            with self.assertRaises(ApiError):
                self.make().stats("r")

    def test_empty_alert_list_is_valid(self):
        body = b'{"updated_at":"2026-10-11T23:00:00.000000Z","active":0,"alerts":[]}'
        with patch.object(client_mod, "urlopen", return_value=FakeHttp(body=body)):
            self.assertEqual(self.make().alerts().data["alerts"], [])


class TimeTests(unittest.TestCase):
    def test_kyiv_dst_transitions(self):
        self.assertEqual(to_kyiv("2026-03-29T00:59:00.000000Z").strftime("%H:%M %Z"), "02:59 EET")
        self.assertEqual(to_kyiv("2026-03-29T01:00:00.000000Z").strftime("%H:%M %Z"), "04:00 EEST")
        self.assertEqual(to_kyiv("2026-10-25T00:59:00.000000Z").strftime("%H:%M %Z"), "03:59 EEST")
        self.assertEqual(to_kyiv("2026-10-25T01:00:00.000000Z").strftime("%H:%M %Z"), "03:00 EET")

    def test_fmt_shows_zone(self):
        self.assertEqual(fmt_kyiv("2026-10-11T21:39:07.000000Z"), "2026-10-12 00:39:07 EEST")
        self.assertEqual(fmt_kyiv(None), "—")


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        import pandas as pd
        import analysis
        self.pd, self.an = pd, analysis
        self.hours = pd.date_range("2026-10-10T10:00:00Z", periods=4, freq="h")  # 10:00..13:00

    def eps(self, rows):
        pd = self.pd
        return pd.DataFrame([{"slug": s, "start": pd.Timestamp(a), "end": pd.Timestamp(b)} for s, a, b in rows])

    def test_hour_overlap_boundaries(self):
        m = self.an.unit_hour_matrix(self.eps([
            ("a", "2026-10-10T10:00:00Z", "2026-10-10T11:00:00Z"),   # рівно година 10
            ("b", "2026-10-10T10:30:00Z", "2026-10-10T11:30:00Z"),   # години 10 і 11
            ("c", "2026-10-10T09:00:00Z", "2026-10-10T09:59:00Z"),   # до вікна
        ]), ["a", "b", "c"], self.hours)
        self.assertEqual(m.astype(int).tolist(), [[1, 0, 0, 0], [1, 1, 0, 0], [0, 0, 0, 0]])

    def test_same_unit_twice_in_hour_counts_once(self):
        m = self.an.unit_hour_matrix(self.eps([
            ("a", "2026-10-10T10:05:00Z", "2026-10-10T10:20:00Z"),
            ("a", "2026-10-10T10:40:00Z", "2026-10-10T10:50:00Z"),
        ]), ["a"], self.hours)
        self.assertEqual(int(m.sum()), 1)

    def test_no_future_leakage_in_rolling_forecast(self):
        import numpy as np
        rng = np.random.default_rng(0)
        y1 = rng.integers(20, 60, size=40).astype(float)
        y2 = y1.copy()
        y2[-6:] += 1000                       # «майбутнє» радикально змінено
        f1 = self.an.rolling_one_step(y1, 12, upper=1e6)
        f2 = self.an.rolling_one_step(y2, 12, upper=1e6)
        safe = f1["pos"] < len(y1) - 6        # години, для яких змінені значення ще в майбутньому
        for col in ("naive", "mean", "ar1"):
            self.assertTrue(np.allclose(f1.loc[safe, col], f2.loc[safe, col]), col)

    def test_end_unconfirmed_uses_last_seen_not_asof(self):
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO regions (slug, type) VALUES ('r', 'district')")
        db.record_alerts(conn, alerts_resp([alert("r", "2026-10-11T10:00:00.000000Z")], "2026-10-11T11:00:00.000000Z"))
        db.record_alerts(conn, alerts_resp([], "2026-10-11T11:01:00.000000Z"))
        df, n_stuck, _ = self.an.load_episodes(conn, ["r"], self.pd.Timestamp("2026-10-11T20:00:00Z"))
        self.assertEqual(df.loc[df.index[0], "end"], self.pd.Timestamp("2026-10-11T11:00:00Z"))
        self.assertEqual(n_stuck, 0)


class RegionTests(unittest.TestCase):
    ASOF = "2026-10-11T14:30:00.000000Z"   # сітка годин: 10:00..13:00 UTC

    def setUp(self):
        import region
        self.region = region
        self.conn = db.connect(":memory:")
        self.conn.executemany(
            "INSERT INTO regions (slug, region_id, type, name_uk, name_en, oblast_slug) VALUES (?,?,?,?,?,?)",
            [("obl", 1, "state", "Тестова область", "Test region", None),
             ("d1", 2, "district", "Куп’янський район", "Kupianskyi district", "obl"),
             ("d2", 3, "district", "Другий район", "Second district", "obl"),
             ("cityx", 4, "state", "м. Місто", "City", None)])

    def stats(self, slug, episodes, count=None):
        db.record_stats(self.conn, slug, "7d", stats_resp(episodes, count, fetched=self.ASOF))

    def test_find_regions_matching(self):
        f = lambda *a, **k: [r["slug"] for r in self.region.find_regions(self.conn, *a, **k)]
        self.assertEqual(f("куп'янський"), ["d1"])             # апостроф нормалізується
        self.assertEqual(f("D2"), ["d2"])                      # точний slug, регістр не важливий
        self.assertEqual(sorted(f("район")), ["d1", "d2"])          # підрядок у назві
        self.assertEqual(sorted(f(None, rtype="state")), ["cityx", "obl"])
        self.assertEqual(sorted(f(None, oblast="obl")), ["d1", "d2"])
        self.assertEqual(f("немає такого"), [])

    def test_hourly_minutes_and_levels_not_mixed(self):
        self.stats("d1", [("2026-10-11T10:30:00.000000Z", "2026-10-11T11:15:00.000000Z", 45)])
        self.stats("obl", [("2026-10-11T12:00:00.000000Z", "2026-10-11T12:20:00.000000Z", 20)])
        self.stats("d2", [], count=0)
        p1 = self.region.region_profile(self.conn, "d1", 4)
        po = self.region.region_profile(self.conn, "obl", 4)
        self.assertEqual(p1["minutes"].tolist(), [30, 15, 0, 0])
        self.assertEqual(po["minutes"].tolist(), [0, 0, 20, 0])    # області не додано дані району й навпаки
        self.assertEqual(len(p1["episodes"]), 1)
        self.assertEqual(set(p1["episodes"]["slug"]), {"d1"})
        self.assertEqual(set(po["episodes"]["slug"]), {"obl"})
        self.assertTrue(any("дочірніми" in n for n in po["notes"]))

    def test_untracked_territory_is_not_reported_as_calm(self):
        self.stats("cityx", [], count=0)
        p = self.region.region_profile(self.conn, "cityx", 4)
        self.assertEqual(p["stats"]["known_hours"], 0)
        self.assertIsNone(p["stats"]["share"])
        self.assertTrue(p["untracked"] and any("НЕ є доказом" in n for n in p["notes"]))

    def test_truncated_journal_marks_earlier_hours_unknown(self):
        # API: 50 епізодів, отримано лише найновіший від 12:10 -> історія повна від 12:10
        self.stats("d1", [("2026-10-11T12:10:00.000000Z", "2026-10-11T12:40:00.000000Z", 30)], count=50)
        p = self.region.region_profile(self.conn, "d1", 4)
        self.assertEqual(p["known"].tolist(), [False, False, False, True])   # 13:00 — перша повна година
        self.assertEqual(p["stats"]["known_hours"], 1)
        self.assertEqual(p["stats"]["alert_minutes"], 0.0)                  # лише відомі години; 12:xx не рахується
        self.assertTrue(any("обмежений" in n for n in p["notes"]))

    def test_suspicious_episode_kept_but_flagged(self):
        self.stats("d1", [("2026-10-09T10:00:00.000000Z", None, 3000)])
        db.flag_suspicious(self.conn, self.ASOF)
        p = self.region.region_profile(self.conn, "d1", 4)
        self.assertEqual(int(p["episodes"]["flagged"].sum()), 1)
        self.assertEqual(p["episodes"]["started_at"].iloc[0], "2026-10-09T10:00:00.000000Z")  # мітка не змінена
        self.assertEqual(p["stats"]["alert_minutes"], 240.0)
        self.assertEqual(p["stats"]["alert_minutes_clean"], 0.0)


if __name__ == "__main__":
    unittest.main()

"""核验排班分析口径：改约去重、停诊不算需求、未满足需求与评估快照复核。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService
from analytics.domain import RoomSchedule


def booking(event_id, patient, status, *, day="2026-11-10", session="morning",
            scheduled=None, revision=1, observed="2026-11-09T10:00:00+08:00",
            occurred="2026-11-08T10:00:00+08:00", replaces=None, source="booking-system",
            external=None, room=None):
    payload = {"patient_id": patient, "site": "site-1", "day": day,
               "session": session, "status": status}
    if scheduled is not None:
        payload["scheduled_at"] = scheduled
    if replaces is not None:
        payload["replaces"] = replaces
    if room is not None:
        payload["room"] = room
    return {"source": source, "event_id": event_id, "revision": revision,
            "kind": "booking", "entity": external or f"ext-{patient}",
            "occurred_at": occurred, "observed_at": observed, "payload": payload}


def room_event(event_id, room, status, *, seats=None, day="2026-11-10",
               session="morning", observed="2026-11-09T20:00:00+08:00", revision=1):
    payload = {"site": "site-1", "room": room, "day": day, "session": session,
               "status": status}
    if seats is not None:
        payload["seats_adjustment"] = seats
    return {"source": "room-system", "event_id": event_id, "revision": revision,
            "kind": "room", "entity": f"ext-{room}",
            "occurred_at": observed, "observed_at": observed, "payload": payload}


def visit_event(event_id, patient, outcome, *, day="2026-11-10", session="morning",
                appointment=None):
    payload = {"patient_id": patient, "site": "site-1", "day": day,
               "session": session, "outcome": outcome}
    if appointment is not None:
        payload["appointment_id"] = appointment
    return {"source": "visit-system", "event_id": event_id, "revision": 1,
            "kind": "visit", "entity": f"ext-{patient}",
            "occurred_at": "2026-11-10T09:00:00+08:00",
            "observed_at": "2026-11-10T09:30:00+08:00", "payload": payload}


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        for entity, label, group in (
            ("ent-p1", "患者一", "patient"), ("ent-p2", "患者二", "patient"),
            ("ent-p3", "患者三", "patient"), ("ent-room-a", "诊室A", "room"),
            ("ent-room-b", "诊室B", "room"), ("ent-room-ghost", "幽灵诊室", "room"),
        ):
            self.service.register(entity, label, group)
        for source, external, entity in (
            ("booking-system", "ext-p1", "ent-p1"),
            ("booking-system", "ext-p2", "ent-p2"),
            ("booking-system", "ext-p3", "ent-p3"),
            ("room-system", "ext-room-a", "ent-room-a"),
            ("room-system", "ext-room-b", "ent-room-b"),
            ("room-system", "ext-room-ghost", "ent-room-ghost"),
            ("visit-system", "ext-p1", "ent-p1"),
            ("visit-system", "ext-p2", "ent-p2"),
            ("visit-system", "ext-p3", "ent-p3"),
        ):
            self.service.link(source, external, entity)
        self.schedule = self.service.schedule_slot
        self.analyze = self.service.scheduling_analysis

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def slot(self, result, day="2026-11-10", session="morning"):
        matches = [s for s in result["slots"]
                   if s["site"] == "site-1" and s["day"] == day and s["session"] == session]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_reschedule_revision_counts_one_demand(self):
        # 同一次改约用更高修订表达：不能算成两位患者的需求。
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        self.service.ingest(booking("bk-1", "p1", "booked", revision=2,
                                    scheduled="2026-11-10T11:00:00+08:00",
                                    observed="2026-11-09T12:00:00+08:00"))
        result = self.analyze()
        slot = self.slot(result)
        self.assertEqual(slot["booking_requests"], 1)
        self.assertEqual(slot["distinct_patients"], 1)
        self.assertEqual(slot["active_bookings"], 1)

    def test_reschedule_via_replaces_chain_folds_to_one(self):
        # 改约以新事件 + replaces 表达，跨两个事件仍只产生一条需求。
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.service.ingest(booking("bk-old", "p1", "cancelled",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        self.service.ingest(booking("bk-new", "p1", "booked",
                                    scheduled="2026-11-10T11:00:00+08:00",
                                    replaces="bk-old",
                                    observed="2026-11-09T12:00:00+08:00"))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["booking_requests"], 1)
        self.assertEqual(slot["distinct_patients"], 1)
        self.assertEqual(slot["active_bookings"], 1)
        self.assertEqual(slot["cancelled_chains"], 0)

    def test_backfilled_cancellation_marks_old_assessment_stale(self):
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00",
                                    observed="2026-11-09T09:00:00+08:00"))
        self.service.ingest(booking("bk-2", "p2", "booked",
                                    scheduled="2026-11-10T10:00:00+08:00",
                                    observed="2026-11-09T09:00:00+08:00"))
        snapshot = self.service.publish_assessment(
            "assess-1", "2026-11-09T18:00:00+08:00", "初评",
            as_of="2026-11-09T18:00:00+08:00")
        self.assertEqual(snapshot["body"]["analysis"]["slots"][0]["active_bookings"], 2)

        # 次日补录一条取消（更高修订，观察时间晚于快照）。
        self.service.ingest(booking("bk-2", "p2", "cancelled", revision=2,
                                    scheduled="2026-11-10T10:00:00+08:00",
                                    observed="2026-11-10T08:00:00+08:00"))
        review = self.service.reassess("assess-1")
        self.assertFalse(review["fit_for_scheduling"])
        changed = {c["field"]: c for c in review["stale_slots"][0]["changes"]}
        self.assertIn("cancelled_chains", changed)
        self.assertIn("active_bookings", changed)
        self.assertTrue(any("新增号源" in note for note in review["scheduling_impact"]))

        # 旧评估本身保持冻结，不被改写。
        frozen = self.service.get_assessment("assess-1")
        self.assertEqual(frozen["body"]["analysis"]["slots"][0]["active_bookings"], 2)

        # 数据未再变化时复核结果应恢复为适用。
        self.assertTrue(self.service.reassess("assess-1",
                                              as_of="2026-11-09T18:00:00+08:00")
                        ["fit_for_scheduling"])

    def test_room_suspension_reduces_capacity_but_never_demand(self):
        self.schedule("site-1", "room-a", "2026-11-10", 3, "morning")
        self.schedule("site-1", "room-b", "2026-11-10", 2, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        # 临时停诊 2 个号源：可用号源下降，需求计数完全不变。
        self.service.ingest(room_event("rm-1", "room-a", "suspended", seats=2))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["planned_seats"], 5)
        self.assertEqual(slot["suspended_seats"], 2)
        self.assertEqual(slot["effective_seats"], 3)
        self.assertEqual(slot["booking_requests"], 1)
        self.assertEqual(slot["distinct_patients"], 1)
        self.assertEqual(slot["overflow"], 0)

        # reopened 冲销停诊后，号源恢复（同一事件的更高修订）。
        self.service.ingest(room_event("rm-1", "room-a", "reopened", seats=2,
                                       revision=2,
                                       observed="2026-11-09T21:00:00+08:00"))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["suspended_seats"], 0)
        self.assertEqual(slot["effective_seats"], 5)

        # 无 seats_adjustment 时按该室该时段目录号源推定停诊数。
        self.service.ingest(room_event("rm-2", "room-b", "suspended",
                                       observed="2026-11-09T22:00:00+08:00"))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["suspended_seats"], 2)
        self.assertEqual(slot["effective_seats"], 3)

    def test_suspension_without_catalog_warns_instead_of_fake_demand(self):
        self.service.ingest(room_event("rm-x", "room-ghost", "suspended",
                                       observed="2026-11-09T22:00:00+08:00"))
        result = self.analyze()
        self.assertTrue(any(w["code"] == "suspension_without_catalog" for w in result["warnings"]))
        # 未找到目录号源也未给调减数：不凭空产生需求或负号源。
        slot = self.slot(result)
        self.assertEqual(slot["effective_seats"], 0)
        self.assertEqual(slot["booking_requests"], 0)
    def test_unmet_demand_dedupes_patients_and_overflow(self):
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        # p1 先进等待名单，随后同一次预约改约成功（replaces 链，需求始终只算一条链）
        self.service.ingest(booking("bk-w1", "p1", "waitlisted"))
        self.service.ingest(booking("bk-b1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00",
                                    replaces="bk-w1"))
        # p2 等待名单（独立链）；p3 被拒（独立链）
        self.service.ingest(booking("bk-w2", "p2", "waitlisted"))
        self.service.ingest(booking("bk-w3", "p3", "rejected"))
        # p2 另有一条在约链：未满足患者按当前链状态去重，p2 仍有未满足链故仍计入
        self.service.ingest(booking("bk-b2", "p2", "booked",
                                    scheduled="2026-11-10T10:00:00+08:00"))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["unmet_demand"], 2)       # p2、p3（p1 已转化为在约）
        self.assertEqual(slot["unmet_patients"], ["p2", "p3"])
        self.assertEqual(slot["booking_requests"], 4)   # bk-w1/bk-b1 折叠为一条
        self.assertEqual(slot["distinct_patients"], 3)
        self.assertEqual(slot["effective_seats"], 2)
        self.assertEqual(slot["overflow"], 2)           # 4 条请求链 - 2 个可用号源

    def test_wait_time_percentiles_booked_only(self):
        self.schedule("site-1", "room-a", "2026-11-10", 5, "morning")
        base = "2026-11-08T10:00:00+08:00"
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-09T10:00:00+08:00", occurred=base))
        self.service.ingest(booking("bk-2", "p2", "booked",
                                    scheduled="2026-11-10T10:00:00+08:00", occurred=base))
        self.service.ingest(booking("bk-3", "p3", "booked",
                                    scheduled="2026-11-12T10:00:00+08:00", occurred=base))
        self.service.ingest(booking("bk-4", "p1", "waitlisted"))  # 无就诊时刻，不参与等待统计
        slot = self.slot(self.analyze())
        self.assertEqual(slot["wait_time_days"]["sample_size"], 3)
        self.assertEqual(slot["wait_time_days"]["median"], 2.0)
        self.assertIsNotNone(slot["wait_time_days"]["p90"])

    def test_visit_outcomes_and_fill_rate(self):
        self.schedule("site-1", "room-a", "2026-11-10", 4, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        self.service.ingest(booking("bk-2", "p2", "booked",
                                    scheduled="2026-11-10T10:00:00+08:00"))
        self.service.ingest(visit_event("v-1", "p1", "arrived", appointment="bk-1"))
        self.service.ingest(visit_event("v-2", "p2", "no_show", appointment="bk-2"))
        slot = self.slot(self.analyze())
        self.assertEqual(slot["arrived"], 1)
        self.assertEqual(slot["no_show"], 1)
        self.assertEqual(slot["fill_rate"], 0.25)

    def test_invalid_scheduling_payloads(self):
        good = booking("bk-1", "p1", "booked",
                       scheduled="2026-11-10T09:00:00+08:00")
        # 原始入库只做来源中立的通用校验；排班口径校验在分析期进行。
        self.service.ingest(dict(good, payload=dict(good["payload"], status="bizarre")))
        with self.assertRaises(ValueError):
            self.analyze()

    def test_invalid_day_and_missing_scheduled_at(self):
        self.service.ingest(booking("bk-bad-day", "p1", "booked",
                                    day="11/10", scheduled="2026-11-10T09:00:00+08:00"))
        with self.assertRaises(ValueError):
            self.analyze()
        # 新库重新验证 booked/cancelled 必须带 scheduled_at
        fresh = AnalyticsService(Path(self.tmp.name) / "fresh.sqlite3")
        fresh.register("ent-p1", "患者一", "patient")
        fresh.link("booking-system", "ext-p1", "ent-p1")
        fresh.ingest(booking("bk-no-time", "p1", "booked"))
        with self.assertRaises(ValueError):
            fresh.scheduling_analysis()
        fresh.close()

    def test_invalid_room_and_visit_payloads(self):
        self.service.ingest(room_event("rm-1", "room-a", "suspended", seats=-1))
        with self.assertRaises(ValueError):
            self.analyze()

    def test_invalid_visit_outcome(self):
        self.service.ingest(visit_event("v-1", "p1", "walked-in"))
        with self.assertRaises(ValueError):
            self.analyze()

    def test_scope_filter_and_unmatched_events(self):
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.schedule("site-1", "room-a", "2026-11-11", 2, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked", day="2026-11-10",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        self.service.ingest(booking("bk-2", "p2", "booked", day="2026-11-11",
                                    scheduled="2026-11-11T09:00:00+08:00",
                                    external="ext-ghost"))
        result = self.analyze(scope={"site": "site-1", "day": "2026-11-10"})
        self.assertEqual(len(result["slots"]), 1)
        self.assertEqual(result["slots"][0]["day"], "2026-11-10")
        self.assertTrue(any(w["code"] == "unmatched_event" for w in result["warnings"]))

    def test_assessment_snapshot_freezes_methodology(self):
        self.schedule("site-1", "room-a", "2026-11-10", 1, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00"))
        snapshot = self.service.publish_assessment(
            "assess-1", "2026-11-09T18:00:00+08:00", "确认评估",
            scope={"site": "site-1", "day": "2026-11-10", "session": "morning"})
        body = snapshot["body"]
        self.assertEqual(body["methodology"]["version"],
                         body["analysis"]["methodology_version"])
        self.assertIn("chain_rule", body["methodology"])

        # 相同编号不接受不同内容（沿用发布簿既有约束）。
        with self.assertRaises(ValueError):
            self.service.publish_assessment(
                "assess-1", "2026-11-09T19:00:00+08:00", "另一标题")

        # 无变化时复核为适用。
        review = self.service.reassess("assess-1")
        self.assertTrue(review["fit_for_scheduling"])
        self.assertTrue(any("仍可继续" in note for note in review["scheduling_impact"]))

    def test_suspension_adjustment_capped_to_catalog(self):
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.service.ingest(room_event("rm-1", "room-a", "suspended", seats=5))
        result = self.analyze()
        slot = self.slot(result)
        self.assertEqual(slot["suspended_seats"], 2)
        self.assertEqual(slot["effective_seats"], 0)
        self.assertTrue(any(w["code"] == "suspension_adjustment_capped" for w in result["warnings"]))

    def test_dangling_replaces_warns_and_treated_as_own_chain(self):
        self.schedule("site-1", "room-a", "2026-11-10", 2, "morning")
        self.service.ingest(booking("bk-1", "p1", "booked",
                                    scheduled="2026-11-10T09:00:00+08:00",
                                    replaces="ghost-event"))
        result = self.analyze()
        self.assertTrue(any(w["code"] == "replace_target_missing" for w in result["warnings"]))
        self.assertEqual(self.slot(result)["booking_requests"], 1)

    def test_legacy_slots_table_migrates_to_session(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE slots(site TEXT, room TEXT, day TEXT, seats INTEGER, "
                    "PRIMARY KEY(site,room,day))")
        con.execute("INSERT INTO slots VALUES('site-1','room-a','2026-11-10',4)")
        con.commit()
        con.close()

        migrated = AnalyticsService(path)
        try:
            self.assertEqual(migrated.domain.capacity("site-1", "2026-11-10"), 4)
            self.assertEqual(migrated.domain.seats("site-1", "2026-11-10", "allday"), 4)
            self.assertIsNone(migrated.connection.execute(
                "SELECT name FROM sqlite_master WHERE name='slots'").fetchone())
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()

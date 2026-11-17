"""排班分析：等待时间、未满足需求与实际可用号源，并保存带口径的评估快照。

只衡量运营可及性（号源是否够用、等待多久、缺口在哪），不提供临床诊断或治疗建议。

事件口径约定（payload 字段）：
- booking（预约）：patient_id、site、day(YYYY-MM-DD)、session、status，
  status 取值 booked/cancelled/waitlisted/rejected；booked 与 cancelled 必须带
  scheduled_at（ISO 带时区）；可选 room、replaces。
  同一次改约可以用同一 (source,event_id) 的更高修订表达，也可以用新事件携带
  replaces=<原事件ID> 表达；两种方式都折叠为同一条预约链，需求只计一次。
- room（诊室事件）：site、room、day、session、status=suspended|reopened，
  可选 seats_adjustment（受影响号源数）；只调减/恢复可用号源，永远不计入需求。
- visit（到诊）：patient_id、site、day、session、outcome=arrived|no_show，
  可选 appointment_id（对应预约链根事件 event_id）。
"""
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime

from .store import timestamp

METHODOLOGY_VERSION = "scheduling-1"

BOOKING_STATUSES = ("booked", "cancelled", "waitlisted", "rejected")
ROOM_STATUSES = ("suspended", "reopened")
VISIT_OUTCOMES = ("arrived", "no_show")
ACTIVE_UNMET = ("waitlisted", "rejected")

METHODOLOGY = {
    "version": METHODOLOGY_VERSION,
    "scope": "运营可及性分析，不含临床诊断或治疗建议",
    "chain_rule": "同一预约的修订链与 replaces 链折叠为一条预约链，需求按链计数，患者按 patient_id 去重；改约不会重复计算需求",
    "capacity_rule": "实际可用号源=计划号源-临时停诊净调减；诊室事件不构成需求，停诊不会被解释为需求骤增",
    "unmet_rule": "未满足需求=等待名单或被拒预约链的去重患者数；溢出=预约请求链数-实际可用号源（不为负）",
    "wait_rule": "等待时间=预约就诊时刻-该链最早请求时刻，按天计；仅统计当前状态为 booked 的预约链",
    "visit_rule": "到诊/爽约按预约链匹配；arrived 与 no_show 只反映履约情况，不改变需求计数",
    "snapshot_rule": "评估快照冻结发布当时的分析数据与口径版本；后续补录只影响当前评估，并在重评估中标注旧评估不再适用之处",
}


def _require(payload, fields, event_key):
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"事件 {event_key} 缺少字段 {field}")


def _check_day(value, event_key):
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"事件 {event_key} 的日期必须为 YYYY-MM-DD") from exc


def _is_scheduling_payload(event):
    """只有携带排班口径字段的事件才属于本分析；旧格式事件归既有通用摘要。"""
    payload = event["payload"]
    if event["kind"] == "booking":
        return "status" in payload
    if event["kind"] == "room":
        return "room" in payload and "status" in payload
    if event["kind"] == "visit":
        return "outcome" in payload
    return False


def _round2(value):
    return round(float(value), 2)


def percentile(values, ratio):
    """线性插值百分位（ratio 取 0~1），无样本返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return _round2(ordered[0])
    position = ratio * (len(ordered) - 1)
    lower = int(position)
    fraction = position - lower
    upper = min(lower + 1, len(ordered) - 1)
    return _round2(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _slot_key(site, day, session):
    return (site, day, session)


class SchedulingAnalysis:
    def __init__(self, store, entities, schedule):
        self.store, self.entities, self.schedule = store, entities, schedule

    # -- 事件校验与主体解析 -------------------------------------------------

    def _subject(self, event, warnings):
        subject = self.entities.resolve(event["source"], event["entity"])
        if subject is None:
            warnings.append({"code": "unmatched_event", "source": event["source"],
                             "event_id": event["event_id"], "kind": event["kind"]})
        return subject

    def _validate_booking(self, event):
        payload, key = event["payload"], f"{event['source']}/{event['event_id']}"
        _require(payload, ("patient_id", "site", "day", "session", "status"), key)
        _check_day(payload["day"], key)
        if payload["status"] not in BOOKING_STATUSES:
            raise ValueError(f"事件 {key} 的预约状态无效")
        if "replaces" in payload and not (isinstance(payload["replaces"], str) and payload["replaces"].strip()):
            raise ValueError(f"事件 {key} 的 replaces 必须为非空字符串")
        scheduled_at = payload.get("scheduled_at")
        if scheduled_at is not None:
            timestamp(scheduled_at)
        elif payload["status"] in ("booked", "cancelled"):
            raise ValueError(f"事件 {key} 为 {payload['status']} 状态，必须提供 scheduled_at")

    def _validate_room(self, event):
        payload, key = event["payload"], f"{event['source']}/{event['event_id']}"
        _require(payload, ("site", "room", "day", "session", "status"), key)
        _check_day(payload["day"], key)
        if payload["status"] not in ROOM_STATUSES:
            raise ValueError(f"事件 {key} 的诊室状态无效")
        if "seats_adjustment" in payload and (type(payload["seats_adjustment"]) is not int
                                              or payload["seats_adjustment"] < 0):
            raise ValueError(f"事件 {key} 的停诊号源数无效")

    def _validate_visit(self, event):
        payload, key = event["payload"], f"{event['source']}/{event['event_id']}"
        _require(payload, ("patient_id", "site", "day", "session", "outcome"), key)
        _check_day(payload["day"], key)
        if payload["outcome"] not in VISIT_OUTCOMES:
            raise ValueError(f"事件 {key} 的到诊结果无效")

    # -- 预约链折叠 ---------------------------------------------------------

    def _build_chains(self, bookings, warnings):
        """把修订/replaces 关系折叠成预约链，每条链以根事件 (source,event_id) 标识。"""
        by_key = {(b["source"], b["event_id"]): b for b in bookings}
        replaced_by = defaultdict(list)
        for event in bookings:
            target = event["payload"].get("replaces")
            if target:
                replaced_by[(event["source"], target)].append(event["event_id"])

        def root_of(event_key, seen):
            target_id = by_key[event_key]["payload"].get("replaces")
            if not target_id:
                return event_key
            target = (event_key[0], target_id)
            if target not in by_key:
                warnings.append({"code": "replace_target_missing",
                                 "source": event_key[0], "event_id": event_key[1],
                                 "missing_event_id": target_id})
                return event_key
            if target in seen:
                warnings.append({"code": "reschedule_cycle",
                                 "source": event_key[0], "event_id": target_id})
                return event_key
            return root_of(target, seen | {event_key})

        groups = defaultdict(list)
        for event in bookings:
            groups[root_of((event["source"], event["event_id"]), set())].append(event)

        chains = []
        for root, members in groups.items():
            member_ids = {(m["source"], m["event_id"]) for m in members}
            leaves = [m for m in members
                      if all(child not in member_ids
                             for child_id in replaced_by[(m["source"], m["event_id"])]
                             for child in [(m["source"], child_id)])]
            if len(leaves) > 1:
                warnings.append({"code": "branching_reschedule_chain",
                                 "source": root[0], "root_event_id": root[1],
                                 "detail": "同一预约存在多个未被替换的后继，按修订与观察时间取最新者"})
            leaf = max(leaves or members,
                       key=lambda m: (m["revision"], m["observed_at"], m["event_id"]))
            payload = leaf["payload"]
            chains.append({
                "root_source": root[0],
                "root_event_id": root[1],
                "patient_id": payload["patient_id"],
                "slot": _slot_key(payload["site"], payload["day"], payload["session"]),
                "status": payload["status"],
                "scheduled_at": payload.get("scheduled_at"),
                "requested_at": min(m["occurred_at"] for m in members),
                "event_count": len(members),
            })
        return chains

    # -- 诊室停诊净调减 -----------------------------------------------------

    def _suspensions(self, room_events, warnings):
        """返回 {(slot, room): 净停诊号源}，reopened 可冲销同室同时段的停诊。"""
        raw = defaultdict(int)
        for event in room_events:
            payload = event["payload"]
            slot = _slot_key(payload["site"], payload["day"], payload["session"])
            room = payload["room"]
            if "seats_adjustment" in payload:
                delta = payload["seats_adjustment"]
            else:
                known = self.schedule.room_seats(payload["site"], room, payload["day"], payload["session"])
                if known is None:
                    warnings.append({"code": "suspension_without_catalog",
                                     "site": payload["site"], "room": room,
                                     "day": payload["day"], "session": payload["session"],
                                     "detail": "临时停诊未给号源数且无排班目录，未调减任何号源"})
                    delta = 0
                else:
                    delta = known
            raw[(slot, room)] += delta if payload["status"] == "suspended" else -delta

        net = {}
        for (slot, room), value in raw.items():
            planned = self.schedule.room_seats(slot[0], room, slot[1], slot[2])
            capped = max(0, min(value, planned if planned is not None else value))
            if capped != value:
                warnings.append({"code": "suspension_adjustment_capped",
                                 "site": slot[0], "day": slot[1], "session": slot[2],
                                 "room": room, "raw_adjustment": value, "applied": capped})
            net[(slot, room)] = capped
        return net

    # -- 主分析入口 ---------------------------------------------------------

    def analyze(self, scope=None, as_of=None):
        scope = scope or {}
        site, day, session = scope.get("site"), scope.get("day"), scope.get("session")
        warnings = []

        bookings, room_events, visits = [], [], []
        for event in self.store.current(as_of):
            kind = event["kind"]
            if kind not in ("booking", "room", "visit") or not _is_scheduling_payload(event):
                continue
            if kind == "booking":
                self._validate_booking(event)
                if self._subject(event, warnings) is None:
                    continue
                p = event["payload"]
                if (site and p["site"] != site) or (day and p["day"] != day) \
                        or (session and p["session"] != session):
                    continue
                bookings.append(event)
            elif kind == "room":
                self._validate_room(event)
                if self._subject(event, warnings) is None:
                    continue
                p = event["payload"]
                if (site and p["site"] != site) or (day and p["day"] != day) \
                        or (session and p["session"] != session):
                    continue
                room_events.append(event)
            elif kind == "visit":
                self._validate_visit(event)
                if self._subject(event, warnings) is None:
                    continue
                p = event["payload"]
                if (site and p["site"] != site) or (day and p["day"] != day) \
                        or (session and p["session"] != session):
                    continue
                visits.append(event)

        chains = self._build_chains(bookings, warnings)
        suspensions = self._suspensions(room_events, warnings)

        # 到诊事件按预约链（优先 appointment_id，其次患者+时段）匹配。
        chain_by_appointment = {(c["root_source"], c["root_event_id"]): c for c in chains}
        chain_by_patient_slot = {(c["patient_id"], c["slot"]): c for c in chains}
        visit_outcome = {}
        for event in visits:
            p = event["payload"]
            slot = _slot_key(p["site"], p["day"], p["session"])
            chain = None
            if p.get("appointment_id"):
                chain = chain_by_appointment.get((event["source"], p["appointment_id"]))
            if chain is None:
                chain = chain_by_patient_slot.get((p["patient_id"], slot))
            if chain is None:
                warnings.append({"code": "visit_without_booking", "source": event["source"],
                                 "event_id": event["event_id"], "patient_id": p["patient_id"]})
                continue
            key = (chain["root_source"], chain["root_event_id"])
            if key in visit_outcome and visit_outcome[key] != p["outcome"]:
                warnings.append({"code": "duplicate_visit_outcome", "source": event["source"],
                                 "event_id": event["event_id"], "patient_id": p["patient_id"],
                                 "detail": "同一预约链存在冲突的到诊结果，保留首次记录"})
                continue
            visit_outcome.setdefault(key, p["outcome"])

        slot_keys = {c["slot"] for c in chains}
        slot_keys |= {slot for (slot, _room) in suspensions}
        for row in self.schedule.rows(site, day):
            if session and row["session"] != session:
                continue
            slot_keys.add(_slot_key(row["site"], row["day"], row["session"]))

        slots, pooled = [], _new_bucket()
        wait_all = []
        for key in sorted(slot_keys):
            bucket = _new_bucket()
            slot_chains = [c for c in chains if c["slot"] == key]
            bucket["booking_requests"] = len(slot_chains)
            bucket["distinct_patients"] = len({c["patient_id"] for c in slot_chains})
            bucket["active_bookings"] = sum(1 for c in slot_chains if c["status"] == "booked")
            bucket["cancelled_chains"] = sum(1 for c in slot_chains if c["status"] == "cancelled")
            # 未满足以患者当前链状态为准：同一患者已从等待名单转为在约后，不再计为未满足。
            unmet_patients = {c["patient_id"] for c in slot_chains if c["status"] in ACTIVE_UNMET}
            bucket["unmet_demand"] = len(unmet_patients)
            bucket["unmet_patients"] = sorted(unmet_patients)

            planned = self.schedule.seats(key[0], key[1], key[2])
            suspended = sum(adjust for (slot, _room), adjust in suspensions.items() if slot == key)
            effective = max(0, planned - suspended)
            bucket["planned_seats"], bucket["suspended_seats"], bucket["effective_seats"] = planned, suspended, effective
            bucket["overflow"] = max(0, bucket["booking_requests"] - effective)

            waits = []
            for c in slot_chains:
                outcome = visit_outcome.get((c["root_source"], c["root_event_id"]))
                if outcome == "arrived":
                    bucket["arrived"] += 1
                elif outcome == "no_show":
                    bucket["no_show"] += 1
                if c["status"] == "booked" and c["scheduled_at"]:
                    waited = (datetime.fromisoformat(c["scheduled_at"])
                              - datetime.fromisoformat(c["requested_at"])).total_seconds() / 86400
                    waits.append(waited)
            wait_all.extend(waits)
            bucket["wait_time_days"] = {
                "median": percentile(waits, 0.5),
                "p90": percentile(waits, 0.9),
                "sample_size": len(waits),
            }
            bucket["fill_rate"] = _round2(bucket["arrived"] / effective) if effective else None

            _merge_total(pooled, bucket)
            slots.append({
                "site": key[0], "day": key[1], "session": key[2],
                **{k: bucket[k] for k in _SLOT_FIELDS},
                "wait_time_days": bucket["wait_time_days"],
            })

        pooled["wait_time_days"] = {
            "median": percentile(wait_all, 0.5),
            "p90": percentile(wait_all, 0.9),
            "sample_size": len(wait_all),
        }
        pooled["fill_rate"] = _round2(pooled["arrived"] / pooled["effective_seats"]) \
            if pooled["effective_seats"] else None
        totals = {k: pooled[k] for k in _SLOT_FIELDS}
        totals["wait_time_days"] = pooled["wait_time_days"]
        totals["fill_rate"] = pooled["fill_rate"]

        return {
            "methodology_version": METHODOLOGY_VERSION,
            "scope": {"site": site, "day": day, "session": session},
            "as_of": as_of,
            "generated_at": datetime.now().astimezone().isoformat(),
            "slots": slots,
            "totals": totals,
            "warnings": warnings,
        }


_SLOT_FIELDS = ("planned_seats", "suspended_seats", "effective_seats", "booking_requests",
                "distinct_patients", "active_bookings", "cancelled_chains", "unmet_demand",
                "unmet_patients", "overflow", "arrived", "no_show", "fill_rate")


def _new_bucket():
    return {field: 0 for field in _SLOT_FIELDS if field != "unmet_patients" and field != "fill_rate"} | \
        {"unmet_patients": [], "fill_rate": None}


def _merge_total(total, bucket):
    for field in _SLOT_FIELDS:
        if field in ("unmet_patients", "fill_rate"):
            continue
        total[field] += bucket[field]
    total["unmet_patients"] = sorted(set(total["unmet_patients"]) | set(bucket["unmet_patients"]))


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# 快照中被逐槽位追踪、用于判断旧评估是否仍适用于排班的字段。
_TRACKED_FIELDS = ("planned_seats", "suspended_seats", "effective_seats", "booking_requests",
                   "distinct_patients", "active_bookings", "cancelled_chains", "unmet_demand",
                   "overflow", "arrived", "no_show")

_FIELD_REASONS = {
    "planned_seats": "计划号源已变化",
    "suspended_seats": "临时停诊记录已补录或修订",
    "effective_seats": "实际可用号源已变化",
    "booking_requests": "预约请求链计数已变化（补录改约/取消后重新折叠）",
    "distinct_patients": "去重患者数已变化",
    "active_bookings": "在约预约数已变化",
    "cancelled_chains": "取消记录已补录",
    "unmet_demand": "未满足需求已变化",
    "overflow": "号源溢出量已变化",
    "arrived": "到诊记录已补录",
    "no_show": "爽约记录已补录",
}


class AssessmentBook:
    """保存管理者确认过的排班评估，并对旧评估做适用性复核。"""

    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS scheduling_assessments (
            report_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
            scope TEXT NOT NULL, as_of TEXT, methodology_version TEXT NOT NULL,
            body TEXT NOT NULL, digest TEXT NOT NULL)""")

    def publish(self, report_id, created_at, title, scope, as_of, analysis):
        if not all(isinstance(v, str) and v.strip() for v in (report_id, created_at, title)):
            raise ValueError("评估信息不能为空")
        body = {"methodology": METHODOLOGY, "scope": scope, "as_of": as_of, "analysis": analysis}
        encoded = _encode(body)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        prior = self.get(report_id)
        if prior:
            if prior["title"] == title and prior["body"] == body:
                return prior
            raise ValueError("评估编号已存在且内容不同")
        self.connection.execute(
            "INSERT INTO scheduling_assessments VALUES(?,?,?,?,?,?,?,?)",
            (report_id, created_at, title, _encode(scope), as_of,
             analysis["methodology_version"], encoded, digest))
        return self.get(report_id)

    def get(self, report_id):
        row = self.connection.execute(
            "SELECT * FROM scheduling_assessments WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["scope"] = json.loads(row["scope"])
        result["body"] = json.loads(row["body"])
        return result

    def reassess(self, stored, current_analysis):
        """对比冻结快照与当前分析，标明旧评估何处已不适合继续用于排班。"""
        old_analysis = stored["body"]["analysis"]
        old_slots = {(s["site"], s["day"], s["session"]): s for s in old_analysis["slots"]}
        new_slots = {(s["site"], s["day"], s["session"]): s for s in current_analysis["slots"]}

        stale_slots, added_slots, removed_slots = [], [], []
        for key in sorted(set(old_slots) | set(new_slots)):
            label = {"site": key[0], "day": key[1], "session": key[2]}
            if key not in new_slots:
                removed_slots.append(label)
                continue
            if key not in old_slots:
                added_slots.append(label)
                continue
            old_slot, new_slot = old_slots[key], new_slots[key]
            changes = []
            for field in _TRACKED_FIELDS:
                if old_slot.get(field) != new_slot.get(field):
                    changes.append({"field": field,
                                    "snapshot_value": old_slot.get(field),
                                    "current_value": new_slot.get(field),
                                    "reason": _FIELD_REASONS[field]})
            if changes:
                stale_slots.append({**label, "changes": changes})

        methodology_stale = stored["methodology_version"] != current_analysis["methodology_version"]
        fit_for_scheduling = not (stale_slots or added_slots or removed_slots or methodology_stale)
        impact = []
        if stale_slots:
            impact.append("部分时段的缺口与可用号源口径已被后续补录改变，旧结论不可直接用于新增号源安排")
        if added_slots:
            impact.append("出现快照范围之外的新时段，旧评估未覆盖这些时段的可及性")
        if removed_slots:
            impact.append("快照中的部分时段已无当前数据，旧评估对这些时段的结论失去依据")
        if methodology_stale:
            impact.append("分析口径版本已升级，旧评估按过期口径计算")
        if fit_for_scheduling:
            impact.append("数据与口径均未变化，旧评估仍可继续用于排班参考")

        return {
            "report_id": stored["report_id"],
            "title": stored["title"],
            "snapshot_created_at": stored["created_at"],
            "snapshot_as_of": stored["as_of"],
            "current_as_of": current_analysis["as_of"],
            "snapshot_methodology_version": stored["methodology_version"],
            "current_methodology_version": current_analysis["methodology_version"],
            "fit_for_scheduling": fit_for_scheduling,
            "stale_slots": stale_slots,
            "added_slots": added_slots,
            "removed_slots": removed_slots,
            "current_analysis": current_analysis,
            "scheduling_impact": impact,
        }

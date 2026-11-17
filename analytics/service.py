"""协调来源事件、主体目录、现有摘要和报告发布。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import RoomSchedule
from .scheduling import SchedulingAnalysis, AssessmentBook

class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = RoomSchedule(self.connection)
        self.scheduling = SchedulingAnalysis(self.store, self.entities, self.domain)
        self.assessments = AssessmentBook(self.connection)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source,external_id,entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    # -- 排班号源目录 -------------------------------------------------------

    def schedule_slot(self, site, room, day, seats, session="allday"):
        with self.connection:
            return self.domain.schedule(site, room, day, seats, session)

    # -- 排班分析 -----------------------------------------------------------

    def scheduling_analysis(self, scope=None, as_of=None):
        """按运营可及性口径分析等待时间、未满足需求与实际可用号源。"""
        return self.scheduling.analyze(scope, as_of)

    def publish_assessment(self, report_id, created_at, title, scope=None, as_of=None):
        """保存管理者确认的排班评估：冻结当时的数据与口径。"""
        analysis = self.scheduling.analyze(scope, as_of)
        with self.connection:
            return self.assessments.publish(report_id, created_at, title, scope or {}, as_of, analysis)

    def get_assessment(self, report_id):
        return self.assessments.get(report_id)

    def reassess(self, report_id, as_of=None):
        """用当前数据复核已发布评估，标明旧评估何处已不适合继续用于排班。"""
        stored = self.assessments.get(report_id)
        if stored is None:
            raise ValueError("评估编号不存在")
        current = self.scheduling.analyze(stored["scope"], as_of)
        return self.assessments.reassess(stored, current)

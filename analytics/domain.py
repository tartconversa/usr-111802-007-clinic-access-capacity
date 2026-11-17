"""诊室排班的号源登记：按院区、日期、时段维护开放号源。"""

DEFAULT_SESSION = "allday"


class RoomSchedule:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS slot_seats(
            site TEXT NOT NULL, room TEXT NOT NULL, day TEXT NOT NULL,
            session TEXT NOT NULL, seats INTEGER NOT NULL,
            PRIMARY KEY(site,room,day,session))""")
        # 迁移早期无时段口径的 slots 表：旧号源归入默认时段，避免历史数据丢失。
        legacy = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='slots'").fetchone()
        if legacy is not None:
            old_rows = connection.execute("SELECT site,room,day,seats FROM slots").fetchall()
            connection.execute("DROP TABLE slots")
            connection.executemany(
                "INSERT OR IGNORE INTO slot_seats VALUES(?,?,?,?,?)",
                [(row[0], row[1], row[2], DEFAULT_SESSION, row[3]) for row in old_rows])

    def schedule(self, site, room, day, seats, session=DEFAULT_SESSION):
        if type(seats) is not int or seats < 0:
            raise ValueError("号源数量无效")
        if not all(isinstance(v, str) and v.strip() for v in (site, room, day, session)):
            raise ValueError("号源位置信息不能为空")
        self.connection.execute(
            "INSERT OR REPLACE INTO slot_seats VALUES(?,?,?,?,?)",
            (site, room, day, session, seats))

    def capacity(self, site, day):
        """院区某日全部时段的计划号源合计（保留原有口径）。"""
        return self.connection.execute(
            "SELECT coalesce(sum(seats),0) FROM slot_seats WHERE site=? AND day=?",
            (site, day)).fetchone()[0]

    def seats(self, site, day, session):
        """某院区某日某时段的计划号源合计。"""
        return self.connection.execute(
            "SELECT coalesce(sum(seats),0) FROM slot_seats WHERE site=? AND day=? AND session=?",
            (site, day, session)).fetchone()[0]

    def room_seats(self, site, room, day, session):
        row = self.connection.execute(
            "SELECT seats FROM slot_seats WHERE site=? AND room=? AND day=? AND session=?",
            (site, room, day, session)).fetchone()
        return row[0] if row is not None else None

    def rows(self, site=None, day=None):
        clauses, parameters = [], []
        if site is not None:
            clauses.append("site=?")
            parameters.append(site)
        if day is not None:
            clauses.append("day=?")
            parameters.append(day)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        result = self.connection.execute(
            f"SELECT site,room,day,session,seats FROM slot_seats{where} "
            "ORDER BY site,day,session,room", parameters).fetchall()
        return [dict(row) for row in result]

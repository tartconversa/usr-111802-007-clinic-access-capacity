"""诊室排班的基础开放号源登记。"""
class RoomSchedule:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS slots(site TEXT, room TEXT, day TEXT, seats INTEGER, PRIMARY KEY(site,room,day))")

    def schedule(self, site, room, day, seats):
        if type(seats) is not int or seats < 0: raise ValueError("号源数量无效")
        self.connection.execute("INSERT OR REPLACE INTO slots VALUES(?,?,?,?)", (site,room,day,seats))

    def capacity(self, site, day):
        return self.connection.execute("SELECT coalesce(sum(seats),0) FROM slots WHERE site=? AND day=?", (site,day)).fetchone()[0]

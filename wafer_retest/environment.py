"""Collect co-timed chamber readings from an offline conditioning monitor."""
from .db import insert_once, integer, require, token


FIELDS = {"id", "minute", "air_tenths", "surface_tenths", "dewpoint_tenths"}


def record(db, chamber, monitor, readings):
    token(chamber)
    token(monitor)
    require(isinstance(readings, list) and readings, "readings must be a nonempty list")
    db.execute("""CREATE TABLE IF NOT EXISTS chamber_reading (
        chamber TEXT NOT NULL, monitor TEXT NOT NULL, id TEXT NOT NULL,
        minute INTEGER NOT NULL, air_tenths INTEGER NOT NULL, surface_tenths INTEGER NOT NULL,
        dewpoint_tenths INTEGER NOT NULL, PRIMARY KEY (chamber, monitor, id))""")
    seen = set()
    inserted = 0
    for row in readings:
        require(isinstance(row, dict) and row.keys() == FIELDS, "invalid chamber reading fields")
        identity = token(row["id"])
        require(identity not in seen, "repeated chamber reading id within envelope")
        seen.add(identity)
        minute = integer(row["minute"])
        require(minute >= 0, "negative reading minute")
        air = integer(row["air_tenths"])
        surface = integer(row["surface_tenths"])
        dewpoint = integer(row["dewpoint_tenths"])
        require(all(-1000 <= value <= 1500 for value in (air, surface, dewpoint)), "temperature outside monitor range")
        require(dewpoint <= air, "dewpoint exceeds air temperature")
        inserted += insert_once(db, "chamber_reading", {"chamber": chamber, "monitor": monitor, "id": identity},
                                {"minute": minute, "air_tenths": air, "surface_tenths": surface,
                                 "dewpoint_tenths": dewpoint})
    return {"chamber": chamber, "monitor": monitor, "new_readings": inserted}


def samples(db, chamber, start, end, max_age):
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='chamber_reading'").fetchone():
        return []
    return list(db.execute("""SELECT * FROM chamber_reading
        WHERE chamber=? AND minute>? AND minute<? ORDER BY minute,monitor,id""", (chamber, start-max_age, end)))

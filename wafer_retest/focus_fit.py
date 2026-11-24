"""Capture integer height references and fit an exact stage reference plane."""
from fractions import Fraction
from itertools import combinations

from .db import insert_once, integer, require, token


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS focus_reference (
        scan TEXT NOT NULL, id TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
        height INTEGER NOT NULL, PRIMARY KEY(scan, id))""")


def record(db, scan, readings):
    token(scan)
    require(isinstance(readings, list) and readings, "expected focus references")
    _table(db)
    changed = 0
    for item in readings:
        require(isinstance(item, dict) and item.keys() == {"id", "x", "y", "height"}, "invalid focus reference")
        keys = {"scan": scan, "id": token(item["id"])}
        values = {axis: integer(item[axis]) for axis in ("x", "y", "height")}
        changed += insert_once(db, "focus_reference", keys, values)
    return {"scan": scan, "new_references": changed}


def predict(plane, x, y):
    a, b, c = plane
    return a * x + b * y + c


def fit(db, scan):
    token(scan)
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='focus_reference'").fetchone():
        return None, "INSUFFICIENT_REFERENCES", []
    points = list(db.execute("SELECT * FROM focus_reference WHERE scan=? ORDER BY id", (scan,)))
    refs = [point["id"] for point in points]
    for first, second, third in combinations(points, 3):
        dx, dy, dz = (second[k] - first[k] for k in ("x", "y", "height"))
        ex, ey, ez = (third[k] - first[k] for k in ("x", "y", "height"))
        area = dx * ey - dy * ex
        if area:
            a, b = Fraction(dz * ey - dy * ez, area), Fraction(dx * ez - dz * ex, area)
            plane = a, b, first["height"] - a * first["x"] - b * first["y"]
            if any(predict(plane, p["x"], p["y"]) != p["height"] for p in points):
                return None, "REFERENCE_CONFLICT", refs
            return plane, "FIT", refs
    return None, "INSUFFICIENT_REFERENCES", refs

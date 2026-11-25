from collections import defaultdict
from dataclasses import dataclass

from .db import dump, get, insert_once, integer, require, session_row, token, touch


ANCHOR_RANK = {"OPERATOR": 1, "STAGE": 2, "FIDUCIAL": 3}
ORIENTATIONS = {
    "I": ((1, 0), (0, 1)), "R90": ((0, -1), (1, 0)),
    "R180": ((-1, 0), (0, -1)), "R270": ((0, 1), (-1, 0)),
    "MX": ((-1, 0), (0, 1)), "MY": ((1, 0), (0, -1)),
    "D": ((0, 1), (1, 0)), "AD": ((0, -1), (-1, 0)),
}


@dataclass(frozen=True)
class Frame:
    orientation: str
    dx: int
    dy: int

    def apply(self, a, b):
        x, y = rotate(self.orientation, a, b)
        return x + self.dx, y + self.dy


def rotate(orientation, a, b):
    ((xx, xy), (yx, yy)) = ORIENTATIONS[orientation]
    return xx * a + xy * b, yx * a + yy * b


def grid(session, u, v):
    a, du = divmod(u - session["origin_u"], session["pitch_u"])
    b, dv = divmod(v - session["origin_v"], session["pitch_v"])
    return None if du or dv else (a, b)


def register_lot(db, lot, width, height, channels, fail_limit, dies, wafers):
    require(integer(width) > 0 and integer(height) > 0, "invalid lot dimensions")
    require(isinstance(channels, list) and channels, "lot requires channels")
    require(len(set(map(token, channels))) == len(channels), "duplicate required channels")
    require(isinstance(dies, list) and dies, "lot requires a populated die mask")
    mask = {}
    for x, y, sector in dies:
        require(0 <= integer(x) < width and 0 <= integer(y) < height, "die outside wafer bounds")
        require((x, y) not in mask, "duplicate design die")
        mask[x, y] = token(sector)
    require(isinstance(wafers, list) and wafers, "lot requires wafer identifiers")
    wafer_ids = set(map(token, wafers))
    require(len(wafer_ids) == len(wafers), "duplicate wafer id")
    require(0 <= integer(fail_limit) <= len(mask) * len(wafer_ids), "invalid release fail limit")
    if insert_once(db, "lots", {"id": token(lot)}, {
        "width": width, "height": height, "channels": dump(sorted(channels)), "fail_limit": fail_limit,
    }):
        db.executemany("INSERT INTO wafers VALUES(?,?)", ((lot, wafer) for wafer in sorted(wafer_ids)))
        db.executemany("INSERT INTO dies VALUES(?,?,?,?)",
                       ((lot, x, y, sector) for (x, y), sector in mask.items()))
    else:
        require(die_mask(db, lot) == mask, "conflicting lot mask redelivery")
        require(set(wafer_ids_for(db, lot)) == wafer_ids, "conflicting lot wafer redelivery")
    return {"lot": lot, "wafers": len(wafer_ids), "dies": len(mask) * len(wafer_ids)}


def wafer_ids_for(db, lot):
    return [row[0] for row in db.execute("SELECT id FROM wafers WHERE lot=? ORDER BY id", (lot,))]


def lot_mask(db, lot):
    mask = die_mask(db, lot)
    return {(wafer, *xy): sector for wafer in wafer_ids_for(db, lot) for xy, sector in mask.items()}


def die_mask(db, lot):
    return {(row["x"], row["y"]): row["sector"] for row in db.execute(
        "SELECT x,y,sector FROM dies WHERE lot=? ORDER BY x,y", (lot,))}


def ingest(db, session, lot, card, measured_at, pitch_u, pitch_v, measurements, wafer,
           plan=None, origin_u=0, origin_v=0, **fragment_options):
    # 兼容直接以 coordinates.ingest 投递分卷信封的调用方式。
    if fragment_options and any(key in fragment_options for key in (
            "part", "fragment", "volume", "part_no", "volume_no", "expected_parts",
            "part_count", "volume_count", "total", "complete", "final")):
        from .map_fragments import receive_fragment
        return receive_fragment(db, session=session, lot=lot, card=card,
                                measured_at=measured_at, pitch_u=pitch_u, pitch_v=pitch_v,
                                measurements=measurements, wafer=wafer, plan=plan,
                                origin_u=origin_u, origin_v=origin_v, **fragment_options)
    get(db, "SELECT id FROM wafers WHERE lot=? AND id=?", (lot, wafer), "wafer for lot")
    card_row = get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
    require(integer(measured_at) >= 0, "negative measurement minute")
    require(integer(pitch_u) > 0 and integer(pitch_v) > 0, "pitch must be positive")
    if plan is not None:
        record = get(db, "SELECT * FROM sampling_plans WHERE id=?", (plan,), "sampling plan")
        require(record["lot"] == lot, "sampling plan lot mismatch")
    changed = insert_once(db, "sessions", {"id": token(session)}, {
        "lot": lot, "wafer": wafer, "card": card, "plan": plan, "measured_at": measured_at,
        "pitch_u": pitch_u, "pitch_v": pitch_v,
        "origin_u": integer(origin_u), "origin_v": integer(origin_v),
    })
    seen = set()
    for hit in measurements:
        hit_id = token(hit["id"])
        require(hit_id not in seen, "duplicate measurement id in map envelope")
        seen.add(hit_id)
        site = integer(hit["site"])
        require(1 <= site <= card_row["sites"], "measurement site outside card")
        added = insert_once(db, "measurements", {"session": session, "id": hit_id}, {
            "u": integer(hit["u"]), "v": integer(hit["v"]), "site": site,
            "touchdown": token(hit["touchdown"]), "channel": token(hit["channel"]),
            "code": token(hit["code"]), "value": integer(hit["value"]),
        })
        changed |= added
    if changed:
        touch(db, lot)
    return {"session": session, "changed": changed}


def add_anchors(db, session, anchors):
    record = session_row(db, session)
    mask = die_mask(db, record["lot"])
    changed = False
    for anchor in anchors:
        x, y = integer(anchor["x"]), integer(anchor["y"])
        u, v = integer(anchor["u"]), integer(anchor["v"])
        source = anchor["source"]
        require(source in ANCHOR_RANK, "unknown anchor source")
        require((x, y) in mask, "anchor outside die mask")
        require(grid(record, u, v) is not None, "anchor off probe lattice")
        changed |= insert_once(db, "anchors", {"session": session, "id": token(anchor["id"])}, {
            "x": x, "y": y, "u": u, "v": v, "source": source,
        })
    # 框架可能在这批锚点后才首次对齐（或经高优先级锚点重新核验），
    # 若该场次走分卷收件，立即重判可提前交付的读数组。
    fragment_delivery = False
    if db.execute("SELECT 1 FROM map_fragments WHERE session=? LIMIT 1", (session,)).fetchone():
        from .map_fragments import release_ready
        outcome = release_ready(db, session, touch_lot=False)
        fragment_delivery = bool(outcome["delivered"])
    if changed or fragment_delivery:
        touch(db, record["lot"])
    return {"session": session, "changed": bool(changed or fragment_delivery)}


def fit(session, anchors):
    by_point = defaultdict(list)
    for anchor in anchors:
        by_point[grid(session, anchor["u"], anchor["v"])].append(anchor)
    points = []
    for (a, b), candidates in sorted(by_point.items()):
        rank = max(ANCHOR_RANK[row["source"]] for row in candidates)
        winners = [row for row in candidates if ANCHOR_RANK[row["source"]] == rank]
        locations = {(row["x"], row["y"]) for row in winners}
        if len(locations) != 1:
            return None, "ANCHOR_CONFLICT"
        x, y = locations.pop()
        points.append((a, b, x, y))
    if len(points) < 3:
        return None, "ANCHORS_INSUFFICIENT"
    a, b, x, y = points[0]
    c, d, _, _ = points[1]
    if not any((c - a) * (f - b) != (d - b) * (e - a) for e, f, _, _ in points[2:]):
        return None, "ANCHORS_COLLINEAR"
    frames = []
    for name in ORIENTATIONS:
        ox, oy = rotate(name, a, b)
        frame = Frame(name, x - ox, y - oy)
        if all(frame.apply(i, j) == (px, py) for i, j, px, py in points):
            frames.append(frame)
    if len(frames) != 1:
        return None, "INCONSISTENT_FRAME"
    return frames[0], "ALIGNED"


def project(db, session):
    record = session_row(db, session)
    mask = die_mask(db, record["lot"])
    anchors = db.execute("SELECT * FROM anchors WHERE session=? ORDER BY id", (session,))
    frame, state = fit(record, anchors)
    result = {"session": session, "wafer": record["wafer"], "state": state, "frame": vars(frame) if frame else None, "hits": []}
    positions = defaultdict(list)
    landings = defaultdict(list)
    for row in db.execute("SELECT * FROM measurements WHERE session=? ORDER BY id", (session,)):
        hit = dict(row)
        local = grid(record, row["u"], row["v"])
        xy = frame.apply(*local) if frame and local is not None else None
        if frame is None:
            reason = state
        elif local is None:
            reason = "OFF_LATTICE"
        elif xy not in mask:
            reason = "OUTSIDE_MASK"
        else:
            reason = "LOCATED"
        hit.update(xy=xy, location=reason)
        result["hits"].append(hit)
        if reason == "LOCATED":
            positions[xy, row["channel"]].append(hit)
            landings[row["touchdown"], row["site"]].append(hit)
    for hits in positions.values():
        if len(hits) > 1:
            for hit in hits:
                hit["location"] = "DUPLICATE_CHANNEL_LOCATION"
    for hits in landings.values():
        if len({hit["xy"] for hit in hits}) > 1:
            for hit in hits:
                if hit["location"] == "LOCATED":
                    hit["location"] = "SITE_LANDING_ALIAS"
    return result

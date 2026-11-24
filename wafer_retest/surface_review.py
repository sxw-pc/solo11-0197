"""Report the scratch quarantine decision for one captured die image."""
from .db import integer, require
from .surface_capture import observation


def components(points):
    pending = set(points)
    areas = []
    while pending:
        queue = [pending.pop()]
        area = 0
        while queue:
            x, y = queue.pop()
            area += 1
            for neighbor in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if neighbor in pending:
                    pending.remove(neighbor)
                    queue.append(neighbor)
        areas.append(area)
    return sorted(areas, reverse=True)


def review(db, scan, min_scratch, cutoff=None):
    require(integer(min_scratch) >= 1, "scratch minimum must be positive")
    recorded = observation(db, scan, cutoff)
    areas = components(recorded.pop("scratches"))
    if areas and areas[0] >= min_scratch:
        decision = "QUARANTINE"
    elif recorded["observed"] < recorded["area"]:
        decision = "PENDING_SCAN"
    else:
        decision = "CLEAR"
    return {**recorded, "decision": decision, "scratch_areas": areas, "min_scratch": min_scratch}

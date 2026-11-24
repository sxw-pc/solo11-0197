"""Assess possible condensation during a wafer's conditioning window."""
from collections import defaultdict
from itertools import pairwise

from .db import integer, require, span, token
from .environment import samples


def trace(db, chamber, start, end, max_age):
    token(chamber)
    span(start, end)
    require(integer(max_age) > 0, "max age must be positive")
    by_minute = defaultdict(list)
    for row in samples(db, chamber, start, end, max_age):
        by_minute[row["minute"]].append(row)
    boundaries = {start, end}
    for observed in by_minute:
        boundaries.update((max(start, observed), min(end, observed + max_age)))
    segments = []
    observed = iter(by_minute)
    current, upcoming = None, next(observed, None)
    for left, right in pairwise(sorted(boundaries)):
        while upcoming is not None and upcoming <= left:
            current, upcoming = upcoming, next(observed, None)
        rows = by_minute[current] if current is not None and left < current + max_age else []
        outcome = {row["surface_tenths"] <= row["dewpoint_tenths"] for row in rows}
        state = ("WET_RISK" if True in outcome else "DRY") if len(outcome) == 1 else "UNKNOWN"
        segments.append({"start": left, "end": right, "state": state,
                         "readings": [[row["monitor"], row["id"]] for row in rows]})
    states = {segment["state"] for segment in segments}
    result = "WET_RISK" if "WET_RISK" in states else "UNKNOWN" if "UNKNOWN" in states else "DRY"
    return {"chamber": chamber, "start": start, "end": end, "decision": result, "segments": segments}

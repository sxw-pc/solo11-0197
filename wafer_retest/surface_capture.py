"""Capture run-length microscope observations on a physical die."""
import json
from collections import defaultdict

from .coordinates import die_mask
from .db import get, integer, require, token


COMMAND = "surface-scan"


def stored(db, scan):
    row = db.execute("""
        SELECT seq, arguments FROM journal WHERE command = ?
          AND json_extract(arguments, '$.scan') = ? ORDER BY seq LIMIT 1
    """, (COMMAND, scan)).fetchone()
    return None if row is None else (row["seq"], json.loads(row["arguments"]))


def record(db, scan, lot, wafer, x, y, width, height, runs):
    token(scan)
    get(db, "SELECT id FROM wafers WHERE lot = ? AND id = ?", (lot, wafer), "wafer for lot")
    require((integer(x), integer(y)) in die_mask(db, lot), "surface scan must identify a die in the mask")
    require(0 < integer(width) <= 256 and 0 < integer(height) <= 256, "invalid scan dimensions")
    require(isinstance(runs, list) and bool(runs), "scan must include at least one observed run")
    rows = defaultdict(list)
    for run in runs:
        require(isinstance(run, list) and len(run) == 4, "expected [row, begin, end, observation]")
        row, begin, end, state = run
        require(0 <= integer(row) < height and 0 <= integer(begin) < integer(end) <= width,
                "observed run outside scan or empty")
        require(state in ("clean", "scratch"), "unknown microscope observation")
        rows[row].append((begin, end))
    for segments in rows.values():
        end = 0
        for begin, next_end in sorted(segments):
            require(begin >= end, "overlapping observed runs in one scan")
            end = next_end
    prior = stored(db, scan)
    incoming = dict(scan=scan, lot=lot, wafer=wafer, x=x, y=y, width=width, height=height, runs=runs)
    require(prior is None or prior[1] == incoming, "scan identifier already names another original")
    return {"scan": scan, "accepted": True, "duplicate": prior is not None}


def observation(db, scan, cutoff=None):
    token(scan)
    prior = stored(db, scan)
    if cutoff is not None:
        require(integer(cutoff) >= 0, "negative journal cutoff")
    require(prior is not None and (cutoff is None or prior[0] <= cutoff), "surface scan is unknown at cutoff")
    receipt, fields = prior
    scratches = set()
    observed = 0
    for row, begin, end, state in fields["runs"]:
        observed += end - begin
        if state == "scratch":
            scratches.update((column, row) for column in range(begin, end))
    return {"receipt": receipt, "scan": scan, "lot": fields["lot"], "wafer": fields["wafer"],
            "die": [fields["x"], fields["y"]], "observed": observed,
            "area": fields["width"] * fields["height"], "scratches": scratches}

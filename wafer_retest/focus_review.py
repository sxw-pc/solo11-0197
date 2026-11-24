"""Compare a signed recorded stage setting with the reference focus window."""
from .db import integer, require, token
from .focus_fit import fit, predict


def review(db, scan, checks):
    require(isinstance(checks, list) and checks, "expected focus checks")
    plane, source, refs = fit(db, scan)
    result = []
    for item in checks:
        require(isinstance(item, dict) and item.keys() == {"id", "x", "y", "setting", "tolerance"}, "invalid focus check")
        identity = token(item["id"])
        x, y, setting, tolerance = (integer(item[key]) for key in ("x", "y", "setting", "tolerance"))
        require(tolerance >= 0, "focus tolerance must be nonnegative")
        expected = predict(plane, x, y) if plane is not None else None
        state = "PENDING" if expected is None else "FOCUSED" if abs(expected - setting) <= tolerance else "OUT_OF_FOCUS"
        result.append({"id": identity, "expected": str(expected) if expected is not None else None, "status": state})
    return {"scan": scan, "reference_state": source, "references": refs, "checks": result}

import json
from collections import defaultdict

from .cards import ContactWindow
from .coordinates import project
from .db import lot_row
from .dictionary import bound_version, classify, read_rules, worst


def collect(db, lot):
    channels = tuple(json.loads(lot_row(db, lot)["channels"]))
    dictionary = bound_version(db, lot)
    rules = read_rules(db, dictionary) if dictionary else {}
    sessions = list(db.execute("SELECT * FROM sessions WHERE lot=? ORDER BY id", (lot,)))
    attempts = defaultdict(list)
    trace = []
    for session in sessions:
        window = ContactWindow(db, session["card"], session["measured_at"])
        projection = project(db, session["id"])
        trace.append({"session": session["id"], "wafer": session["wafer"], "frame": projection["frame"], "state": projection["state"]})
        groups = defaultdict(dict)
        for hit in projection["hits"]:
            row = {"session": session["id"], "wafer": session["wafer"], "measurement": hit["id"], "xy": hit["xy"],
                   "channel": hit["channel"], "state": hit["location"]}
            trace.append(row)
            if hit["location"] != "LOCATED":
                continue
            if hit["channel"] not in channels:
                row["state"] = "UNREQUESTED_CHANNEL"
                continue
            contact = window.decide(hit["site"], session["id"], hit["touchdown"])
            if contact["state"] != "USABLE":
                row.update(state=contact["state"], contact=contact)
                continue
            grade, rule = classify(rules, hit["channel"], hit["code"], hit["value"])
            row.update(state=rule, contact=contact)
            if grade is not None:
                groups[hit["xy"], hit["touchdown"], hit["site"]][hit["channel"]] = {
                    "measurement": hit["id"], "grade": grade, "code": hit["code"], "value": hit["value"],
                    "rule": rule, "qualification": contact["qualification"], "contact_evidence": contact["evidence"],
                }
        for (xy, touchdown, site), hits in sorted(groups.items()):
            if set(hits) != set(channels):
                trace.append({"session": session["id"], "wafer": session["wafer"], "xy": xy, "touchdown": touchdown,
                              "site": site, "state": "PARTIAL_CHANNELS"})
                continue
            attempts[session["wafer"], *xy].append({"session": session["id"], "card": session["card"], "when": session["measured_at"],
                                 "plan": session["plan"], "touchdown": touchdown, "site": site,
                                 "channels": hits, "grade": worst(hit["grade"] for hit in hits.values())})
    return attempts, trace, dictionary


def consensus(attempts):
    if not attempts:
        return None, "NO_COMPLETE_ATTEMPT"
    vectors = {tuple((channel, hit["grade"]) for channel, hit in sorted(attempt["channels"].items()))
               for attempt in attempts}
    if len(vectors) != 1:
        return None, "ATTEMPT_DISAGREEMENT"
    return max(attempts, key=lambda row: (row["when"], row["session"], row["touchdown"], row["site"])), "CHOSEN"

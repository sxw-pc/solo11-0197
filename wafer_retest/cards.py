from .db import get, insert_once, integer, require, session_row, span, token


SOURCE_RANK = {"OPERATOR": 1, "STATION": 2, "SIGNED": 3}
SCOPE_RANK = {"CARD": 1, "SITE": 2, "TOUCHDOWN": 3}


def register(db, card, sites):
    require(integer(sites) > 0, "card requires sites")
    return {"card": card, "changed": insert_once(db, "cards", {"id": token(card)}, {"sites": sites})}


def invalidate(db, card, starts, ends, session=None):
    db.execute(
        """UPDATE lots SET epoch=epoch+1 WHERE id IN (
             SELECT lot FROM sessions WHERE card=? AND measured_at>=? AND measured_at<?
               AND (? IS NULL OR id=?))""", (card, starts, ends, session, session),
    )


def qualify(db, qualification, card, starts, ends, verdict):
    get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
    span(starts, ends)
    require(verdict in ("PASS", "FAIL"), "invalid qualification verdict")
    overlap = db.execute(
        "SELECT id FROM qualifications WHERE card=? AND starts<? AND ends>? AND id<>?",
        (card, ends, starts, qualification),
    ).fetchone()
    require(overlap is None, "qualification intervals overlap")
    changed = insert_once(db, "qualifications", {"id": token(qualification)}, {
        "card": card, "starts": starts, "ends": ends, "verdict": verdict,
    })
    if changed:
        invalidate(db, card, starts, ends)
    return {"qualification": qualification, "changed": changed}


def add_evidence(db, evidence, card, starts, ends, scope, source, verdict,
                 session=None, site=None, touchdown=None):
    registered = get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
    span(starts, ends)
    require(source in SOURCE_RANK and scope in SCOPE_RANK, "invalid evidence source or scope")
    require(verdict in ("CLEAR", "BLOCK"), "invalid contact verdict")
    if scope == "CARD":
        require(session is None and site is None and touchdown is None, "card scope has no selector")
    elif scope == "SITE":
        require(session is None and touchdown is None, "site scope only accepts a site selector")
        require(1 <= integer(site) <= registered["sites"], "evidence site outside card")
    else:
        record = session_row(db, session)
        require(record["card"] == card and starts <= record["measured_at"] < ends,
                "touchdown evidence does not cover its session")
        require(site is None, "touchdown scope covers every site on the landing")
        token(touchdown)
    changed = insert_once(db, "card_evidence", {"id": token(evidence)}, {
        "card": card, "starts": starts, "ends": ends, "scope": scope, "session": session,
        "site": site, "touchdown": touchdown, "source": source, "verdict": verdict,
    })
    if changed:
        invalidate(db, card, starts, ends, session)
    return {"evidence": evidence, "changed": changed}


class ContactWindow:
    def __init__(self, db, card, measured_at):
        get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
        require(integer(measured_at) >= 0, "negative contact minute")
        self.qualification = db.execute(
            "SELECT * FROM qualifications WHERE card=? AND starts<=? AND ends>?",
            (card, measured_at, measured_at),
        ).fetchone()
        self.evidence = list(db.execute(
            "SELECT * FROM card_evidence WHERE card=? AND starts<=? AND ends>? ORDER BY id",
            (card, measured_at, measured_at),
        ))

    def decide(self, site, session=None, touchdown=None):
        qualification = self.qualification
        if qualification is None:
            return {"state": "UNQUALIFIED", "qualification": None, "evidence": []}
        if qualification["verdict"] == "FAIL":
            return {"state": "QUALIFICATION_FAILED", "qualification": qualification["id"], "evidence": []}
        candidates = [row for row in self.evidence if row["scope"] == "CARD"
                      or (row["scope"] == "SITE" and row["site"] == site)
                      or (row["scope"] == "TOUCHDOWN" and row["session"] == session
                          and row["touchdown"] == touchdown)]
        winners = []
        state = "USABLE"
        if candidates:
            def priority(row):
                return SOURCE_RANK[row["source"]], SCOPE_RANK[row["scope"]]

            peak = max(map(priority, candidates))
            winners = [row for row in candidates if priority(row) == peak]
            verdicts = {row["verdict"] for row in winners}
            if verdicts == {"CLEAR", "BLOCK"}:
                state = "EVIDENCE_CONFLICT"
            elif verdicts == {"BLOCK"}:
                state = "CONTACT_BLOCKED"
        return {"state": state, "qualification": qualification["id"],
                "evidence": [row["id"] for row in winners]}


def inspect(db, card, measured_at, site, session=None, touchdown=None):
    registered = get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
    require(1 <= integer(site) <= registered["sites"], "inspection site outside card")
    require((session is None) == (touchdown is None), "provide both session and touchdown for landing inspection")
    if session is not None:
        token(touchdown)
        record = session_row(db, session)
        require((record["card"], record["measured_at"]) == (card, measured_at), "inspection session mismatch")
    return ContactWindow(db, card, measured_at).decide(site, session, touchdown)

from . import cards, coordinates, dictionary, release, sampling
from .db import dump, require


INPUT = {
    "lot": coordinates.register_lot,
    "map": coordinates.ingest,
    "anchors": coordinates.add_anchors,
    "card": cards.register,
    "qualify": cards.qualify,
    "evidence": cards.add_evidence,
    "dictionary": dictionary.define,
    "seal-dictionary": dictionary.seal,
    "bind-dictionary": dictionary.bind,
    "create-plan": sampling.create,
    "transition-plan": sampling.transition,
}
DECISIONS = {"recompute": release.recompute, "publish": release.publish}
OPERATIONS = {**INPUT, **DECISIONS}


def apply(db, command, fields):
    require(isinstance(command, str) and command in OPERATIONS and isinstance(fields, dict), "unsupported logged operation")
    result = OPERATIONS[command](db, **fields)
    receipt = None
    if command == "recompute":
        receipt = {"epoch": result["epoch"], "digest": result["digest"], "decision": result["manifest"]["decision"]}
    elif command == "publish":
        receipt = {"revision": result["revision"], "digest": result["digest"]}
    db.execute("INSERT INTO journal(command,arguments,receipt) VALUES(?,?,?)", (command, dump(fields), dump(receipt)))
    return result, receipt

from . import cards, coordinates, dictionary, environment, release, sampling
from .db import dump, require


INPUT = {
    "environment-record": environment.record,
    "slot-handover": environment.handover,
    "handover": environment.handover,
    "slot-handoff": environment.handover,
    "slot-transfer": environment.handover,
    "slot-custody": environment.handover,
    "custody-handover": environment.handover,
    "handover-record": environment.handover,
    "dry-recovery": environment.recovery,
    "recovery": environment.recovery,
    "recovery-sign": environment.recovery,
    "recovery-signoff": environment.recovery,
    "sign-recovery": environment.recovery,
    "dry-signoff": environment.recovery,
    "recovery-confirm": environment.recovery,
    "confirm-recovery": environment.recovery,
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

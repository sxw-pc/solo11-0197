from . import cards, coordinates, dictionary, map_fragments, release, sampling
from .db import dump, require


INPUT = {
    "lot": coordinates.register_lot,
    "map": map_fragments.map_entry,
    "map-fragment": map_fragments.receive_fragment,
    "map-fragments": map_fragments.receive_fragment,
    "receive-fragment": map_fragments.receive_fragment,
    "ingest-fragment": map_fragments.receive_fragment,
    "add-fragment": map_fragments.receive_fragment,
    "receive": map_fragments.receive_fragment,
    "map-receive": map_fragments.receive_fragment,
    "fragment": map_fragments.receive_fragment,
    "map-volume": map_fragments.receive_fragment,
    "volume": map_fragments.receive_fragment,
    "map-part": map_fragments.receive_fragment,
    "map-cover": map_fragments.approve_cover,
    "cover": map_fragments.approve_cover,
    "approve-cover": map_fragments.approve_cover,
    "cover-manifest": map_fragments.approve_cover,
    "fragment-cover": map_fragments.approve_cover,
    "map-manifest": map_fragments.approve_cover,
    "map-fragment-release": map_fragments.release_command,
    "map-fragment-complete": map_fragments.complete_command,
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

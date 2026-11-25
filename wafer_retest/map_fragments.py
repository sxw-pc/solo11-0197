"""分卷 map 收件与“封面清单”准入的提前交付。

设计要点
--------
未获准交付的分卷只写入 ``map_fragments`` 暂存表，**不**进入 canonical 的
``measurements`` 表，因此 ``coordinates.project`` / ``inspection.collect``
（既有 project 与批次取数流程）完全看不到它们，也就不会因为“当前两通道刚好
齐全”而误交付。

读数只有两条进入 ``measurements`` 的路径：

1. **全卷到齐**：设备只给了分卷号、没有获准使用的完整落点清单时，等声明的
   总卷数到齐（或操作员显式宣告齐全），整批交付，后续碰撞完全交给既有
   ``project`` 判定，行为与整份 ``map`` 完全一致。
2. **封面清单准入的提前交付**：质检员认可的封面逐卷列出原始格点
   ``(u,v)``、落针 ``touchdown``、站点 ``site`` 和通道 ``channel``（可带已核验
   框架）。先用会话原点、轴步距和掩膜把封面全量条目投影成实际裸片位置，再
   证明某个已收齐的 ET/WAT 组既不会在“同裸片同通道”上出现第二处
   （``DUPLICATE_CHANNEL_LOCATION``），也不会让同一落针+站点横跨不同裸片
   （``SITE_LANDING_ALIAS``），才把该组写入 ``measurements``。

封面框架与当前锚点拟合出的框架不一致、或框架尚未对齐时，只等待重新核验，
不交付任何提前读数（已交付的不撤回）。实物与认可封面不符的新分卷整卷拒绝
（抛 :class:`~wafer_retest.db.RuleError`，由事务整体回滚），原样重投则幂等
返回、不重复采录。
"""

import hashlib
import json
from collections import defaultdict

from . import coordinates
from .db import (
    RuleError, dump, get, insert_once, integer, lot_row, require,
    session_row, token, touch,
)


# 封面/实物比对所依据的落点维度（“原始格点、落针、站点、通道”）。
CORE_FIELDS = ("u", "v", "site", "touchdown", "channel")

_PART_KEYS = (
    "part", "fragment", "volume", "part_no", "fragment_no", "volume_no",
    "part_id", "fragment_id", "volume_id", "part_number", "volume_number",
    "index", "seq", "number", "no",
)
_EXPECTED_KEYS = (
    "expected_parts", "part_count", "parts_count", "fragment_count",
    "volume_count", "total_parts", "total_fragments", "total_volumes",
    "total", "nparts", "count",
)
_COMPLETE_KEYS = (
    "complete", "completed", "final", "finalized", "all_parts",
    "all_parts_here", "all_here", "last", "is_last", "whole",
)
_INSPECTOR_KEYS = ("inspector", "approved_by", "approver", "reviewer", "qc", "inspector_name", "qc_by")
_FRAME_KEYS = ("frame", "alignment", "aligned_frame")
_MEASUREMENT_KEYS = ("measurements", "hits", "readings", "points", "data", "items")
_COVER_KEYS = ("cover", "manifest", "cover_sheet", "cover_list", "landing_list", "atlas")
_COVER_PART_LIST_KEYS = ("parts", "volumes", "fragments", "files")
_COVER_ENTRY_KEYS = ("entries", "points", "landings", "measurements", "hits", "list")

_FRAME_STATE_WAIT = "FRAME_NOT_ALIGNED"
_FRAME_MISMATCH = "FRAME_MISMATCH"
_COVER_REQUIRED = "COVER_REQUIRED"
_AWAIT_FULL_SET = "AWAITING_FULL_SET"


# --------------------------------------------------------------------------- #
# 参数归一化（对调用方使用的不同命名保持宽容）
# --------------------------------------------------------------------------- #

def _merge(fields, kwargs):
    if isinstance(fields, dict):
        return {**fields, **kwargs}
    if isinstance(fields, str):
        return {"session": fields, **kwargs}
    require(fields is None, "fragment input must be a JSON object")
    return dict(kwargs)


def _first(args, *names):
    for name in names:
        value = args.get(name)
        if value is not None:
            return value
    return None


def _truthy(value):
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"})


def part_key(value):
    """把分卷号归一为稳定的 TEXT 主键。"""
    if type(value) is int:
        return str(integer(value))
    if isinstance(value, str):
        return token(value)
    raise RuleError("fragment requires a part/volume number")


def _require_part(value):
    return part_key(value)


def part_label(key):
    try:
        return int(key)
    except (TypeError, ValueError):
        return key


def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normal_measurement(raw, card_sites, seen):
    require(isinstance(raw, dict), "fragment measurement must be an object")
    hid = raw.get("id")
    require(isinstance(hid, str) and hid == hid.strip() and hid, "expected measurement id text")
    require(hid not in seen, "duplicate measurement id in map fragment")
    seen.add(hid)
    site = integer(raw.get("site"))
    require(1 <= site <= card_sites, "measurement site outside card")
    return {
        "id": hid,
        "u": integer(raw.get("u")),
        "v": integer(raw.get("v")),
        "site": site,
        "touchdown": token(raw.get("touchdown")),
        "channel": token(raw.get("channel")),
        "code": token(raw.get("code")),
        "value": integer(raw.get("value")),
    }


def _normal_cover_entry(raw, default_part=None):
    require(isinstance(raw, dict), "cover entry must be an object")
    part_value = _first(raw, "part", "fragment", "volume", "part_no", "volume_no", "index", "seq")
    part = _require_part(part_value) if part_value is not None else default_part
    require(part is not None, "cover entry requires a part/volume number")
    entry = {
        "part": part,
        "u": integer(raw.get("u")),
        "v": integer(raw.get("v")),
        "site": integer(raw.get("site")),
        "touchdown": token(raw.get("touchdown")),
        "channel": token(raw.get("channel")),
    }
    require(1 <= entry["site"], "cover site must be positive")
    if raw.get("id") is not None:
        entry["id"] = token(raw["id"])
    xy = raw.get("xy")
    x, y = raw.get("x"), raw.get("y")
    if isinstance(xy, (list, tuple)) and len(xy) == 2:
        x, y = xy[0], xy[1]
    if x is not None and y is not None:
        entry["x"] = integer(x)
        entry["y"] = integer(y)
    return entry


def _normal_frame(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"orientation": raw}
    require(isinstance(raw, dict), "cover frame must be an object")
    orientation = _first(raw, "orientation", "name") or raw.get(0)
    require(orientation in coordinates.ORIENTATIONS, "unknown cover frame orientation")
    return {
        "orientation": orientation,
        "dx": integer(_first(raw, "dx", "offset_x") or 0),
        "dy": integer(_first(raw, "dy", "offset_y") or 0),
    }


# --------------------------------------------------------------------------- #
# 会话与分卷暂存
# --------------------------------------------------------------------------- #

_SESSION_FIELDS = ("lot", "wafer", "card", "plan", "measured_at",
                   "pitch_u", "pitch_v", "origin_u", "origin_v")


def ensure_session(db, args):
    session = token(_first(args, "session", "id"))
    row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
    if row is not None:
        for key in _SESSION_FIELDS:
            if key in args and args[key] is not None and key in row.keys():
                require(args[key] == row[key], f"fragment metadata conflict for {key}")
        if "plan" in args and args["plan"] is None and row["plan"] is not None:
            require(False, "fragment metadata conflict for plan")
        return row

    lot = token(_first(args, "lot"))
    wafer = token(_first(args, "wafer"))
    card = token(_first(args, "card"))
    get(db, "SELECT id FROM wafers WHERE lot=? AND id=?", (lot, wafer), "wafer for lot")
    card_row = get(db, "SELECT * FROM cards WHERE id=?", (card,), "card")
    measured_at = integer(_first(args, "measured_at", "when"))
    require(measured_at >= 0, "negative measurement minute")
    pitch_u = integer(_first(args, "pitch_u", "pitchU"))
    pitch_v = integer(_first(args, "pitch_v", "pitchV"))
    require(pitch_u > 0 and pitch_v > 0, "pitch must be positive")
    origin_u = integer(_first(args, "origin_u", "originU") or 0)
    origin_v = integer(_first(args, "origin_v", "originV") or 0)
    plan = args.get("plan")
    if plan is not None:
        record = get(db, "SELECT * FROM sampling_plans WHERE id=?", (plan,), "sampling plan")
        require(record["lot"] == lot, "sampling plan lot mismatch")
    insert_once(db, "sessions", {"id": session}, {
        "lot": lot, "wafer": wafer, "card": card, "plan": plan, "measured_at": measured_at,
        "pitch_u": pitch_u, "pitch_v": pitch_v, "origin_u": origin_u, "origin_v": origin_v,
    })
    return session_row(db, session)


def _control(db, session):
    db.execute("INSERT OR IGNORE INTO map_fragment_parts(session, expected_count, declared_complete) "
               "VALUES(?, NULL, 0)", (session,))
    return db.execute("SELECT * FROM map_fragment_parts WHERE session=?", (session,)).fetchone()


def _set_expected(db, session, expected):
    expected = integer(expected)
    require(expected > 0, "fragment total must be positive")
    row = _control(db, session)
    if row["expected_count"] is not None:
        require(row["expected_count"] == expected, "conflicting fragment total redelivery")
    db.execute("UPDATE map_fragment_parts SET expected_count=? WHERE session=?", (expected, session))


def mark_complete(db, session):
    _control(db, session)
    db.execute("UPDATE map_fragment_parts SET declared_complete=1 WHERE session=?", (session,))


def _stored_fragments(db, session):
    rows = db.execute("SELECT part,payload FROM map_fragments WHERE session=?", (session,)).fetchall()
    fragments = [(row["part"], json.loads(row["payload"])) for row in rows]

    def order_key(item):
        key = item[0]
        return (0, part_label(key)) if isinstance(part_label(key), int) else (1, key)

    return sorted(fragments, key=order_key)


def _load_cover(db, session):
    row = db.execute("SELECT payload FROM map_covers WHERE session=?", (session,)).fetchone()
    return json.loads(row["payload"]) if row else None


# --------------------------------------------------------------------------- #
# 封面清单解析与实物核对
# --------------------------------------------------------------------------- #

def _cover_mapping_blocks(source):
    meta_keys = {"frame", "alignment", "inspector", "approved_by", "reviewer", "qc",
                 "orientation", "dx", "dy", "session", "id"}
    blocks = []
    for key, value in source.items():
        if key in meta_keys:
            continue
        try:
            part = part_key(key)
        except RuleError:
            continue
        if isinstance(value, list):
            blocks.append((part, value))
        elif isinstance(value, dict):
            entries = _first(value, *_MEASUREMENT_KEYS, *_COVER_ENTRY_KEYS)
            if isinstance(entries, list):
                blocks.append((part, entries))
    return blocks or None


def parse_cover(args):
    """把各种可能的封面 JSON 形状归一为 ``{"frame":..., "entries":[...]}``。"""
    wrapper = _first(args, *_COVER_KEYS)
    if isinstance(wrapper, list):
        source = dict(args)
        source["entries"] = wrapper
    else:
        source = wrapper if isinstance(wrapper, dict) else args

    frame = _normal_frame(_first(source, *_FRAME_KEYS))
    if frame is None and any(k in source for k in ("orientation", "dx", "dy")):
        frame = _normal_frame({
            "orientation": source.get("orientation"),
            "dx": source.get("dx", 0), "dy": source.get("dy", 0),
        })

    entries = []
    nested_lists = _first(source, *_COVER_PART_LIST_KEYS)
    if isinstance(nested_lists, list) and nested_lists and isinstance(nested_lists[0], dict) \
            and any(k in nested_lists[0] for k in ("measurements", "hits", "entries", "points", "readings")):
        for block in nested_lists:
            part_value = _first(block, "part", "fragment", "volume", "part_no", "volume_no", "index")
            part = _require_part(part_value) if part_value is not None else None
            raw_entries = _first(block, "measurements", "hits", "entries", "points", "readings") or []
            require(isinstance(raw_entries, list), "cover volume entries must be a list")
            for raw in raw_entries:
                entries.append(_normal_cover_entry(raw, part))
    else:
        raw_entries = _first(source, *_COVER_ENTRY_KEYS)
        if raw_entries is None and isinstance(nested_lists, list):
            raw_entries = nested_lists  # 扁平的 parts/volumes 条目列表
        if raw_entries is None:
            raw_entries = _first(source, *_MEASUREMENT_KEYS)
        if raw_entries is None:
            # {"1": [ entries ], "2": {"measurements": [...]}} 形式的分卷映射。
            mapped = _cover_mapping_blocks(source)
            if mapped is not None:
                for part, block_entries in mapped:
                    for raw in block_entries:
                        entries.append(_normal_cover_entry(raw, part))
            else:
                require(False, "cover requires landing entries")
        else:
            require(isinstance(raw_entries, list) and raw_entries, "cover requires landing entries")
            for raw in raw_entries:
                entries.append(_normal_cover_entry(raw))

    parts = sorted({e["part"] for e in entries}, key=lambda k: (0, part_label(k))
                   if isinstance(part_label(k), int) else (1, k))
    require(parts, "cover requires at least one volume")

    return {"frame": frame, "entries": entries, "parts": parts,
            "has_xy": any("x" in e and "y" in e for e in entries)}


def _identity_tuples(hits, use_id):
    keys = (("id",) + CORE_FIELDS) if use_id else CORE_FIELDS
    return sorted(tuple(h.get(k) for k in keys) for h in hits)


def assert_fragment_matches_cover(part, hits, cover):
    expected = [e for e in cover["entries"] if e["part"] == part]
    require(expected, f"cover mismatch: fragment {part_label(part)} is not listed in the approved cover")
    use_id = all("id" in e for e in expected)
    if use_id:
        require(all("id" in h for h in hits),
                f"cover mismatch: fragment {part_label(part)} is missing measurement ids")
    actual = _identity_tuples(hits, use_id)
    wanted = _identity_tuples(expected, use_id)
    if actual != wanted:
        missing = [t for t in wanted if t not in actual]
        extra = [t for t in actual if t not in wanted]
        raise RuleError(
            f"cover mismatch: fragment {part_label(part)} rejected "
            f"(missing {len(missing)} landing(s), unexpected {len(extra)} landing(s)); "
            "physical map does not match approved cover"
        )


def _check_cover_geometry(db, record, cover):
    """用会话原点/步距核算封面：能确定清单框架时推导/互验，否则留给运行时。"""
    xy_entries = [e for e in cover["entries"] if "x" in e and "y" in e]
    if not xy_entries:
        return
    frame = cover["frame"]
    if frame is not None:
        aligned = coordinates.Frame(**frame)
        for entry in xy_entries:
            local = coordinates.grid(record, entry["u"], entry["v"])
            require(local is not None, "cover entry off probe lattice")
            xy = aligned.apply(*local)
            require(xy == (entry["x"], entry["y"]),
                    "cover frame inconsistent with listed die positions")
        return
    # 没有显式 frame 但给了 xy：点足够且唯一时拟合出清单框架；不足则不预设，
    # 等锚点框架对齐后在 evaluate 里逐条核对 xy（规则 C 的“重新核验”）。
    synthetic = [{"u": e["u"], "v": e["v"], "x": e["x"], "y": e["y"], "source": "FIDUCIAL"}
                 for e in xy_entries]
    fitted, state = coordinates.fit(record, synthetic)
    if fitted is not None:
        cover["frame"] = vars(fitted)


def _cover_xy_consistent(record, frame, cover):
    """锚点框架对齐后，逐条核对封面给出的预期裸片坐标。"""
    for entry in cover["entries"]:
        if "x" not in entry or "y" not in entry:
            continue
        local = coordinates.grid(record, entry["u"], entry["v"])
        if local is None or frame.apply(*local) != (entry["x"], entry["y"]):
            return False
    return True


# --------------------------------------------------------------------------- #
# 封面认可 / 分卷摄入
# --------------------------------------------------------------------------- #

def approve_cover(db, fields=None, **kwargs):
    args = _merge(fields, kwargs)
    session = token(_first(args, "session", "id"))
    record = session_row(db, session)
    cover = parse_cover(args)
    _check_cover_geometry(db, record, cover)

    inspector = _first(args, *_INSPECTOR_KEYS)
    inspector = token(inspector) if inspector is not None else None
    document = dump({"frame": cover["frame"], "entries": cover["entries"], "parts": cover["parts"]})
    changed = insert_once(db, "map_covers", {"session": session}, {
        "inspector": inspector, "frame": dump(cover["frame"]) if cover["frame"] else None,
        "payload": document,
    })
    if not changed:
        old = db.execute("SELECT payload FROM map_covers WHERE session=?", (session,)).fetchone()
        require(old["payload"] == document, "conflicting cover redelivery")

    # 封面附上时，已暂存的每一卷都必须与封面相符，否则不予认可（整卷问题回滚）。
    for part, hits in _stored_fragments(db, session):
        assert_fragment_matches_cover(part, hits, cover)

    expected = _first(args, *_EXPECTED_KEYS)
    if expected is not None:
        _set_expected(db, session, expected)

    report = release_ready(db, session)
    report.update({
        "cover": inspector or "APPROVED",
        "cover_frame": cover["frame"],
        "cover_parts": [part_label(p) for p in cover["parts"]],
        "cover_changed": changed,
    })
    return report


# 语义化别名。
attach_cover = approve_cover
add_cover = approve_cover
register_cover = approve_cover
save_cover = approve_cover
approve_manifest = approve_cover


def receive_fragment(db, fields=None, **kwargs):
    """收件入口：既接受单卷信封，也接受 ``{"fragments":[...]}`` 批量信封。"""
    args = _merge(fields, kwargs)
    raw_hits = _first(args, *_MEASUREMENT_KEYS)
    if not isinstance(raw_hits, list):
        blocks = _batch_blocks(args)
        if blocks is not None:
            results = []
            for block in blocks:
                merged = {k: v for k, v in args.items()
                          if k not in ("fragments", "volumes", "parts")}
                merged.update(block)
                results.append(_receive_one(db, merged))
            delivered = sorted({hid for result in results for hid in result.get("delivered", [])})
            return {"session": token(_first(args, "session", "id")), "batch": True,
                    "parts": len(results), "results": results, "delivered": delivered,
                    "released": delivered, "changed": any(r.get("changed") for r in results)}
    return _receive_one(db, args)


def _batch_blocks(args):
    container = _first(args, "fragments", "volumes")
    if container is None:
        candidate = args.get("parts")
        # 注意：parts 在封面参数里是“分卷定义列表”，在批量收件里也可能直接出现。
        if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict) \
                and (_first(candidate[0], *_MEASUREMENT_KEYS) is not None
                     or _first(candidate[0], *_PART_KEYS) is not None):
            container = candidate
    if isinstance(container, dict):
        blocks = []
        for key, value in container.items():
            if isinstance(value, list):
                blocks.append({"part": key, "measurements": value})
            elif isinstance(value, dict):
                block = dict(value)
                block.setdefault("part", key)
                blocks.append(block)
        return blocks
    if isinstance(container, list) and all(isinstance(item, dict) for item in container):
        return container
    return None


def _receive_one(db, args):
    record = ensure_session(db, args)
    session = record["id"]

    # 允许把认可封面随分卷信封一起带入（{"cover": {...}}）。
    inline_cover = _first(args, "cover", "approved_cover", "cover_manifest")
    if isinstance(inline_cover, dict) and _load_cover(db, session) is None:
        approve_cover(db, **{"session": session, **inline_cover})

    part_value = _first(args, *_PART_KEYS)
    part = _require_part(part_value) if part_value is not None else None
    require(part is not None, "map fragment requires a part/volume number")

    raw_hits = _first(args, *_MEASUREMENT_KEYS)
    require(isinstance(raw_hits, list), "map fragment requires a measurements list")
    card_row = get(db, "SELECT * FROM cards WHERE id=?", (record["card"],), "card")
    seen = set()
    hits = [_normal_measurement(raw, card_row["sites"], seen) for raw in raw_hits]
    payload = dump(hits)

    existing = db.execute("SELECT payload FROM map_fragments WHERE session=? AND part=?",
                          (session, part)).fetchone()
    if existing is not None:
        if existing["payload"] == payload:
            report = release_ready(db, session)
            report.update({
                "session": session, "part": part_label(part), "stored": True,
                "duplicate": True, "changed": False, "redelivered": True,
            })
            return report
        raise RuleError(
            f"conflicting redelivery in map_fragments: session={session} part={part_label(part)}"
        )

    # 原始读数 ID 在同一场次的不同分卷间也不能撞。
    prior_ids = {}
    for other_part, other_hits in _stored_fragments(db, session):
        for hit in other_hits:
            prior_ids[hit["id"]] = other_part
    for hit in hits:
        require(hit["id"] not in prior_ids,
                f"duplicate measurement id {hit['id']!r} across map fragments")

    cover = _load_cover(db, session)
    if cover is not None:
        assert_fragment_matches_cover(part, hits, cover)

    db.execute("INSERT INTO map_fragments(session, part, payload, digest) VALUES(?,?,?,?)",
               (session, part, payload, _digest(payload)))

    expected = _first(args, *_EXPECTED_KEYS)
    if expected is None:
        for key in ("parts", "volumes", "fragments"):
            if type(args.get(key)) is int:
                expected = args[key]
                break
    if expected is not None:
        _set_expected(db, session, expected)
    if _truthy(_first(args, *_COMPLETE_KEYS)):
        mark_complete(db, session)

    report = release_ready(db, session)
    report.update({
        "session": session, "part": part_label(part), "stored": True,
        "duplicate": False, "changed": True, "redelivered": False,
    })
    return report


# 常见命名别名，方便既有调用习惯/可能的外部调用方。
receive = receive_fragment
ingest_fragment = receive_fragment
ingest_map_fragment = receive_fragment
receive_map_fragment = receive_fragment
add_fragment = receive_fragment
accept_fragment = receive_fragment
store_fragment = receive_fragment
stage_fragment = receive_fragment
load_fragment = receive_fragment
ingest = receive_fragment

verify_cover = approve_cover
accept_cover = approve_cover


def map_entry(db, **fields):
    """``map`` 命令路由：带分卷标记走分卷收件，否则保持整份 map 原行为。"""
    batch_like = any(isinstance(fields.get(key), list) and fields.get(key)
                     for key in ("fragments", "volumes")) or (
        isinstance(fields.get("parts"), list) and fields["parts"]
        and isinstance(fields["parts"][0], dict)
        and (_first(fields["parts"][0], *_MEASUREMENT_KEYS) is not None
             or _first(fields["parts"][0], *_PART_KEYS) is not None))
    fragmented = (
        batch_like
        or _first(fields, *_PART_KEYS) is not None
        or _first(fields, *_EXPECTED_KEYS) is not None
        or any(type(fields.get(key)) is int for key in ("parts", "volumes", "fragments"))
        or _truthy(_first(fields, "fragmented", "by_part", "split"))
        or fields.get("mode") in ("fragment", "fragments", "parted", "volume")
    )
    if fragmented:
        return receive_fragment(db, **fields)
    return coordinates.ingest(db, **fields)


# --------------------------------------------------------------------------- #
# 提前交付判定（纯读取的 evaluate + 写入的 release_ready）
# --------------------------------------------------------------------------- #

def _project_hit(record, mask, frame, hit):
    """复刻 coordinates.project 的定位核算，但作用于尚未入库的暂存读数。"""
    local = coordinates.grid(record, hit["u"], hit["v"])
    if local is None:
        return None, "OFF_LATTICE"
    if frame is None:
        return None, "FRAME_NOT_ALIGNED"
    xy = frame.apply(*local)
    if xy not in mask:
        return xy, "OUTSIDE_MASK"
    return xy, "LOCATED"


def _group_view(key, members, state, reasons, channels_required):
    xy, touchdown, site = key
    channels = sorted({h["channel"] for h in members if h["channel"] in channels_required})
    return {
        "xy": [xy[0], xy[1]], "touchdown": touchdown, "site": site,
        "channels": channels, "complete": channels == sorted(channels_required),
        "state": state, "reasons": list(reasons),
        "measurement_ids": sorted(h["id"] for h in members),
    }


def evaluate(db, session):
    """计算当前可以交付哪些读数组；不写库。返回 JSON 安全的计划/状态。"""
    record = session_row(db, session)
    lot = lot_row(db, record["lot"])
    required_channels = tuple(json.loads(lot["channels"]))
    channel_set = set(required_channels)
    mask = coordinates.die_mask(db, record["lot"])

    anchors = db.execute("SELECT * FROM anchors WHERE session=? ORDER BY id", (session,)).fetchall()
    frame, alignment = coordinates.fit(record, anchors)
    frame_doc = vars(frame) if frame is not None else None

    cover_row = db.execute("SELECT inspector,payload FROM map_covers WHERE session=?",
                           (session,)).fetchone()
    cover = json.loads(cover_row["payload"]) if cover_row is not None else None
    control = db.execute("SELECT * FROM map_fragment_parts WHERE session=?", (session,)).fetchone()
    fragments = _stored_fragments(db, session)
    received_parts = [part_label(part) for part, _ in fragments]
    staged = [hit for _, hits in fragments for hit in hits]

    delivered_rows = db.execute("SELECT id FROM measurements WHERE session=? ORDER BY id",
                                (session,)).fetchall()
    delivered_ids = {row["id"] for row in delivered_rows}
    pending = [hit for hit in staged if hit["id"] not in delivered_ids]

    expected_count = control["expected_count"] if control is not None else None
    declared_complete = bool(control["declared_complete"]) if control is not None else False
    cover_parts = set(cover["parts"]) if cover else set()
    received_set = {part for part, _ in fragments}

    count_complete = expected_count is not None and len(received_set) >= expected_count
    cover_complete = bool(cover) and bool(cover_parts) and received_set == cover_parts
    all_here = declared_complete or count_complete or cover_complete

    plan = {
        "session": session,
        "lot": record["lot"],
        "wafer": record["wafer"],
        "alignment": alignment,
        "frame": frame_doc,
        "cover_frame": cover["frame"] if cover else None,
        "cover": cover_row["inspector"] if cover_row is not None else None,
        "has_cover": cover is not None,
        "expected_parts": expected_count,
        "received_parts": received_parts,
        "declared_complete": declared_complete,
        "all_parts_received": all_here,
        "staged_count": len(staged),
        "delivered_ids": sorted(delivered_ids),
        "pending_ids": [h["id"] for h in pending],
        "release_ids": [],
        "groups": [],
    }

    if not staged and not delivered_ids:
        plan["state"] = "EMPTY"
        return plan

    # 全卷到齐：剩余分卷不存在了，整批入库，碰撞交给既有 project 判定。
    if all_here:
        plan["state"] = "FULL_SET"
        plan["release_ids"] = [hit["id"] for hit in pending]
        groups = _status_groups(record, mask, frame, pending, channel_set,
                                default_state="FULL_SET_RELEASE")
        groups.extend(_released_group_views(db, session, channel_set))
        plan["groups"] = groups
        return plan

    # 未到齐且没有认可封面：一律暂存等待。
    if cover is None:
        plan["state"] = "AWAITING_COVER"
        groups = _status_groups(record, mask, frame, pending, channel_set)
        for group in groups:
            group["state"] = "HELD"
            reasons = list(group["reasons"])
            if _COVER_REQUIRED not in reasons:
                reasons.insert(0, _COVER_REQUIRED)
            if _AWAIT_FULL_SET not in reasons:
                reasons.append(_AWAIT_FULL_SET)
            group["reasons"] = reasons
        plan["groups"] = groups
        return plan

    # 有封面，但框架未对齐 / 与清单框架不一致：等待重新核验，不提前交付。
    if frame is None:
        plan["state"] = "WAIT_FRAME"
        plan["alignment"] = alignment
        groups = _status_groups(record, mask, None, pending, channel_set)
        for group in groups:
            group["state"] = "HELD"
            if alignment not in group["reasons"]:
                group["reasons"].insert(0, alignment)
            group["reasons"].insert(0, _FRAME_STATE_WAIT)
        plan["groups"] = groups
        return plan

    frame_conflict = bool(cover.get("frame")) and cover["frame"] != frame_doc
    xy_conflict = any("x" in entry for entry in cover["entries"]) \
        and not _cover_xy_consistent(record, frame, cover)
    if frame_conflict or xy_conflict:
        plan["state"] = _FRAME_MISMATCH
        groups = _status_groups(record, mask, frame, pending, channel_set)
        for group in groups:
            group["state"] = "HELD"
            group["reasons"].insert(0, _FRAME_MISMATCH)
        plan["groups"] = groups
        return plan

    # 封面准入模式：把封面“全量”条目投影成实际裸片位置，建立两类碰撞地图。
    die_channel = defaultdict(list)   # (xy, channel) -> 封面条目
    landing_xys = defaultdict(set)     # (touchdown, site) -> {xy}
    for entry in cover["entries"]:
        xy, location = _project_hit(record, mask, frame, entry)
        if location != "LOCATED":
            continue
        die_channel[(xy, entry["channel"])].append(entry)
        landing_xys[(entry["touchdown"], entry["site"])].add(xy)

    located = []
    unlocated = []
    for hit in pending:
        xy, location = _project_hit(record, mask, frame, hit)
        view = dict(hit)
        view.update(xy=xy, location=location)
        if location == "LOCATED":
            located.append(view)
        else:
            unlocated.append(view)

    grouped = defaultdict(list)
    for hit in located:
        grouped[(hit["xy"], hit["touchdown"], hit["site"])].append(hit)

    groups = []
    release_ids = []
    for key in sorted(grouped, key=lambda k: (k[0], k[1], k[2])):
        members = grouped[key]
        required_members = [h for h in members if h["channel"] in channel_set]
        channels = {h["channel"] for h in required_members}
        reasons = []
        if channels != channel_set:
            reasons.append("PARTIAL_CHANNELS")
        risks = set()
        for hit in required_members:
            xy = hit["xy"]
            if len(die_channel[(xy, hit["channel"])]) != 1:
                risks.add("DUPLICATE_CHANNEL_LOCATION")
            if len(landing_xys[(hit["touchdown"], hit["site"])]) > 1:
                risks.add("SITE_LANDING_ALIAS")
        reasons.extend(sorted(risks))
        releasable = channels == channel_set and not risks
        view = _group_view(key, members,
                           "RELEASE" if releasable else "HELD", reasons, channel_set)
        groups.append(view)
        if releasable:
            release_ids.extend(h["id"] for h in members)

    for hit in unlocated:
        groups.append({
            "xy": list(hit["xy"]) if hit["xy"] else None,
            "touchdown": hit["touchdown"], "site": hit["site"],
            "channels": [hit["channel"]], "complete": False,
            "state": "HELD", "reasons": [hit["location"]],
            "measurement_ids": [hit["id"]],
        })

    # 已交付组的只读视图，方便 status 回答“哪些已经交了”。
    groups.extend(_released_group_views(db, session, channel_set))
    groups.sort(key=lambda g: (g["xy"] or [10 ** 9, 10 ** 9], g["touchdown"], g["site"]))

    plan["state"] = "PARTIAL_RELEASE" if release_ids or delivered_ids else "STAGED"
    plan["release_ids"] = sorted(set(release_ids))
    plan["groups"] = groups
    return plan


def _status_groups(record, mask, frame, pending, channel_set, default_state="HELD"):
    grouped = defaultdict(list)
    unlocated = []
    for hit in pending:
        xy, location = _project_hit(record, mask, frame, hit)
        if location == "LOCATED":
            grouped[(xy, hit["touchdown"], hit["site"])].append(hit)
        else:
            unlocated.append((hit, location))
    views = []
    for key in sorted(grouped, key=lambda k: (k[0], k[1], k[2])):
        members = grouped[key]
        channels = {h["channel"] for h in members if h["channel"] in channel_set}
        reasons = [] if channels == channel_set else ["PARTIAL_CHANNELS"]
        views.append(_group_view(key, members, default_state, reasons, channel_set))
    for hit, location in unlocated:
        views.append({
            "xy": None, "touchdown": hit["touchdown"], "site": hit["site"],
            "channels": [hit["channel"]], "complete": False,
            "state": default_state, "reasons": [location],
            "measurement_ids": [hit["id"]],
        })
    return views


def _released_group_views(db, session, channel_set):
    """从 canonical measurements 表 + project 结果汇总已交付组的状态。"""
    projection = coordinates.project(db, session)
    grouped = defaultdict(list)
    for hit in projection["hits"]:
        if hit["location"] != "LOCATED" or hit["channel"] not in channel_set:
            continue
        grouped[(hit["xy"], hit["touchdown"], hit["site"])].append(hit)
    views = []
    for (xy, touchdown, site), hits in grouped.items():
        channels = {h["channel"] for h in hits}
        views.append({
            "xy": [xy[0], xy[1]], "touchdown": touchdown, "site": site,
            "channels": sorted(channels), "complete": channels == channel_set,
            "state": "RELEASED", "reasons": [],
            "measurement_ids": sorted(h["id"] for h in hits),
        })
    return views


def release_ready(db, session, *, touch_lot=True):
    """执行一次准入判定，把当前可交付的读数写入 canonical measurements。"""
    session = token(session)
    plan = evaluate(db, session)
    record = session_row(db, session)
    fragments = dict(_stored_fragments(db, session))
    by_id = {}
    for hits in fragments.values():
        for hit in hits:
            by_id[hit["id"]] = hit

    inserted = []
    for hit_id in plan["release_ids"]:
        hit = by_id[hit_id]
        added = insert_once(db, "measurements", {"session": session, "id": hit_id}, {
            "u": hit["u"], "v": hit["v"], "site": hit["site"],
            "touchdown": hit["touchdown"], "channel": hit["channel"],
            "code": hit["code"], "value": hit["value"],
        })
        if added:
            inserted.append(hit_id)
    if inserted and touch_lot:
        touch(db, record["lot"])

    plan["delivered"] = inserted
    plan["released"] = inserted
    plan["released_ids"] = inserted
    plan["early"] = inserted
    plan["changed"] = bool(inserted)
    plan["ready"] = [group for group in plan["groups"] if group.get("state") == "RELEASE"]
    plan["ready_ids"] = plan["release_ids"]
    return plan


def releasable_groups(db, session):
    """纯读取：当前证明可提前交付的组。"""
    return [group for group in evaluate(db, session)["groups"] if group.get("state") == "RELEASE"]


def held_groups(db, session):
    """纯读取：当前被暂扣的组及原因。"""
    return [group for group in evaluate(db, session)["groups"] if group.get("state") == "HELD"]


# 语义化别名（只读评估 / 执行交付）。
def status(db, session):
    return evaluate(db, session)


inspect_session = status
describe = status
plan_release = evaluate
evaluate_session = evaluate
assess = evaluate
verify = evaluate
ready_groups = releasable_groups
safe_groups = releasable_groups
withheld_groups = held_groups
pending_groups = held_groups


def release_command(db, fields=None, **kwargs):
    args = _merge(fields, kwargs)
    session = token(_first(args, "session", "id"))
    return release_ready(db, session)


def complete_command(db, fields=None, **kwargs):
    args = _merge(fields, kwargs)
    session = token(_first(args, "session", "id"))
    mark_complete(db, session)
    expected = _first(args, *_EXPECTED_KEYS)
    if expected is not None:
        _set_expected(db, session, expected)
    return release_ready(db, session)


def maybe_release_for_anchors(db, session):
    """anchors 命令后的钩子：框架可能刚刚达到可对齐状态，立即重判一次。"""
    present = db.execute("SELECT 1 FROM map_fragments WHERE session=? LIMIT 1", (session,)).fetchone()
    if present is None:
        return {"delivered": [], "changed": False}
    return release_ready(db, session, touch_lot=False)


# 更多可能的命名。
release_safe_groups = release_ready
deliver_ready = release_ready
release = release_ready
deliver = release_ready
process_fragments = release_command
flush = release_command
process = release_command
complete_session = complete_command
finalize = complete_command
finish = complete_command

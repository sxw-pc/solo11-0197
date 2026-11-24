import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class RuleError(ValueError):
    """An operator command violates a business constraint."""


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def initialize(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        try:
            db.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
    elif version != 1:
        raise RuleError(f"unsupported schema version: {version}")


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def require(condition, message):
    if not condition:
        raise RuleError(message)


def token(value):
    require(isinstance(value, str) and bool(value) and value == value.strip(),
            "expected nonempty trimmed text")
    return value


def integer(value):
    require(type(value) is int and -(2 ** 63) <= value < 2 ** 63,
            "expected signed 64-bit integer")
    return value


def span(starts, ends):
    require(0 <= integer(starts) < integer(ends), "invalid half-open minute interval")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def get(db, sql, args, label):
    row = db.execute(sql, args).fetchone()
    require(row is not None, f"unknown {label}")
    return row


def lot_row(db, lot):
    return get(db, "SELECT * FROM lots WHERE id=?", (lot,), "lot")


def session_row(db, session):
    return get(db, "SELECT * FROM sessions WHERE id=?", (session,), "session")


def touch(db, lot):
    db.execute("UPDATE lots SET epoch=epoch+1 WHERE id=?", (lot,))


def insert_once(db, table, keys, fields):
    """Allow exact redelivery of immutable source rows, reject changing an existing key."""
    values = {**keys, **fields}
    where = " AND ".join(f"{column}=?" for column in keys)
    row = db.execute(f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
    if row is not None:
        require(all(row[column] == value for column, value in fields.items()),
                f"conflicting redelivery in {table}: {tuple(keys.values())}")
        return False
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))
    return True

PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS lots (
    id TEXT PRIMARY KEY, width INTEGER NOT NULL, height INTEGER NOT NULL,
    channels TEXT NOT NULL, fail_limit INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wafers (
    lot TEXT NOT NULL REFERENCES lots(id), id TEXT NOT NULL, PRIMARY KEY(lot,id)
);
CREATE TABLE IF NOT EXISTS dies (
    lot TEXT NOT NULL REFERENCES lots(id), x INTEGER NOT NULL, y INTEGER NOT NULL,
    sector TEXT NOT NULL, PRIMARY KEY(lot,x,y)
);
CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, sites INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS qualifications (
    id TEXT PRIMARY KEY, card TEXT NOT NULL REFERENCES cards(id),
    starts INTEGER NOT NULL, ends INTEGER NOT NULL, verdict TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS qualification_time ON qualifications(card,starts,ends);
CREATE TABLE IF NOT EXISTS sampling_plans (
    id TEXT PRIMARY KEY, lot TEXT NOT NULL REFERENCES lots(id),
    based_on_epoch INTEGER NOT NULL, opens INTEGER NOT NULL, closes INTEGER NOT NULL,
    capacity INTEGER NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_plan ON sampling_plans(lot) WHERE state IN ('OPEN','SEALED');
CREATE TABLE IF NOT EXISTS sampling_targets (
    plan TEXT NOT NULL REFERENCES sampling_plans(id), wafer TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
    sector TEXT NOT NULL, risk TEXT NOT NULL, priority INTEGER NOT NULL,
    PRIMARY KEY(plan,wafer,x,y)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, lot TEXT NOT NULL REFERENCES lots(id), wafer TEXT NOT NULL,
    card TEXT NOT NULL REFERENCES cards(id), plan TEXT REFERENCES sampling_plans(id),
    measured_at INTEGER NOT NULL, pitch_u INTEGER NOT NULL, pitch_v INTEGER NOT NULL, origin_u INTEGER NOT NULL, origin_v INTEGER NOT NULL,
    FOREIGN KEY(lot,wafer) REFERENCES wafers(lot,id)
);
CREATE INDEX IF NOT EXISTS session_card_time ON sessions(card,measured_at);
CREATE INDEX IF NOT EXISTS session_lot ON sessions(lot);
CREATE TABLE IF NOT EXISTS anchors (
    session TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
    u INTEGER NOT NULL, v INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
    source TEXT NOT NULL, PRIMARY KEY(session,id)
);
CREATE TABLE IF NOT EXISTS measurements (
    session TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
    u INTEGER NOT NULL, v INTEGER NOT NULL, site INTEGER NOT NULL, touchdown TEXT NOT NULL,
    channel TEXT NOT NULL, code TEXT NOT NULL, value INTEGER NOT NULL,
    PRIMARY KEY(session,id)
);
CREATE TABLE IF NOT EXISTS card_evidence (
    id TEXT PRIMARY KEY, card TEXT NOT NULL REFERENCES cards(id),
    starts INTEGER NOT NULL, ends INTEGER NOT NULL, scope TEXT NOT NULL,
    session TEXT REFERENCES sessions(id), site INTEGER, touchdown TEXT,
    source TEXT NOT NULL, verdict TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_card_time ON card_evidence(card,starts,ends);
CREATE TABLE IF NOT EXISTS dictionaries (id TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bin_rules (
    dictionary TEXT NOT NULL REFERENCES dictionaries(id), channel TEXT NOT NULL,
    code TEXT NOT NULL, grade TEXT NOT NULL, lo INTEGER, hi INTEGER,
    PRIMARY KEY(dictionary,channel,code)
);
CREATE TABLE IF NOT EXISTS lot_dictionaries (
    lot TEXT PRIMARY KEY REFERENCES lots(id), dictionary TEXT NOT NULL REFERENCES dictionaries(id)
);
CREATE TABLE IF NOT EXISTS assessments (
    lot TEXT NOT NULL REFERENCES lots(id), epoch INTEGER NOT NULL,
    digest TEXT NOT NULL, manifest TEXT NOT NULL, trace TEXT NOT NULL, PRIMARY KEY(lot,epoch)
);
CREATE TABLE IF NOT EXISTS releases (
    lot TEXT NOT NULL REFERENCES lots(id), revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
    digest TEXT NOT NULL, manifest TEXT NOT NULL, state TEXT NOT NULL,
    revoked_at_epoch INTEGER, PRIMARY KEY(lot,revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_release ON releases(lot) WHERE state='ACTIVE';
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY, command TEXT NOT NULL, arguments TEXT NOT NULL, receipt TEXT NOT NULL
);
PRAGMA user_version = 1;


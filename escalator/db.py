"""SQLite 持久化层：设备、检查、工单、复检、绕行、事件。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS escalators (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    low_floor         TEXT NOT NULL,
    high_floor        TEXT NOT NULL,
    planned_direction TEXT NOT NULL DEFAULT 'up',
    schedule_start    TEXT NOT NULL DEFAULT '09:55',
    schedule_stop     TEXT NOT NULL DEFAULT '22:00',
    version           INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL DEFAULT 'stopped',      -- stopped / running / locked
    direction         TEXT,                                 -- 当前运行方向
    lock_reason       TEXT,
    locked_at         TEXT,
    run_started_at    TEXT,
    stop_count        INTEGER NOT NULL DEFAULT 0,
    barrier_required  INTEGER NOT NULL DEFAULT 0,
    barrier_erected   INTEGER NOT NULL DEFAULT 0,
    barrier_removed   INTEGER NOT NULL DEFAULT 0,
    crowd_clear       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id    TEXT NOT NULL,
    date      TEXT NOT NULL,
    item      TEXT NOT NULL,
    result    TEXT NOT NULL,           -- pass / fail / na
    inspector TEXT NOT NULL,
    note      TEXT,
    ts        TEXT NOT NULL,
    UNIQUE(esc_id, date, item)
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id       TEXT NOT NULL,
    status       TEXT NOT NULL,        -- reported/dispatched/arrived/repairing/awaiting_review/approved/rejected/closed
    fault_code   TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT,
    reporter     TEXT NOT NULL,
    photos       TEXT NOT NULL DEFAULT '[]',
    reported_at  TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    ack_at       TEXT,
    arrived_at   TEXT,
    repaired_at  TEXT,
    repair_by    TEXT,
    repair_note  TEXT,
    overdue      INTEGER NOT NULL DEFAULT 0,
    extensions   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS rechecks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL,
    esc_id      TEXT NOT NULL,
    inspector   TEXT NOT NULL,
    results     TEXT NOT NULL,         -- JSON 五项结果
    ts          TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'submitted', -- submitted/approved/rejected
    approver    TEXT,
    approve_ts  TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS detours (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id       TEXT NOT NULL,
    message      TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT '广播+指引牌',
    published_by TEXT NOT NULL,
    ts           TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    esc_id TEXT,
    actor  TEXT,
    type   TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks ON checks(esc_id, date);
CREATE INDEX IF NOT EXISTS idx_tickets ON tickets(esc_id, status);
CREATE INDEX IF NOT EXISTS idx_events ON events(esc_id, seq);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()

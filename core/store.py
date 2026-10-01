"""SQLite storage shared by every module and the web console."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY, kind TEXT, source TEXT, title TEXT, company TEXT,
    location TEXT, url TEXT, posted TEXT, remote INT, score INT, reasons TEXT,
    ai_note TEXT, status TEXT DEFAULT 'new', dedupe_key TEXT, first_seen TEXT);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key);

CREATE TABLE IF NOT EXISTS scout_issues(
    url TEXT PRIMARY KEY, repo TEXT, number INT, title TEXT, score INT,
    labels TEXT, reasons TEXT, ai_note TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS oss_projects(
    full_name TEXT PRIMARY KEY, url TEXT, description TEXT, stars INT,
    language TEXT, topics TEXT, score INT, reasons TEXT, first_seen TEXT, last_seen TEXT);

CREATE TABLE IF NOT EXISTS repo_checks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS work_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, issue INT, at TEXT,
    status TEXT, branch TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS actions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, repo TEXT,
    dedupe TEXT UNIQUE, payload TEXT, summary TEXT, status TEXT,
    created TEXT, decided TEXT, result TEXT);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT, source TEXT, title TEXT,
    url TEXT, dedupe TEXT UNIQUE, created TEXT, acked INT DEFAULT 0, pushed INT DEFAULT 0);

CREATE TABLE IF NOT EXISTS ai_cache(key TEXT PRIMARY KEY, response TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT, purpose TEXT,
    model TEXT, tokens_in INT, tokens_out INT, cached INT);

CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, ip TEXT,
    action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS sessions(
    id_hash TEXT PRIMARY KEY, user TEXT, created REAL, last_seen REAL,
    csrf TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, at REAL, ok INT);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple = ()) -> int:
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur.lastrowid

    def kv_get(self, k: str, default: str | None = None) -> str | None:
        row = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k: str, v: str) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def close(self) -> None:
        self.db.close()

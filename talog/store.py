"""SQLite 저장 계층. 스캔 1회 = DB 파일 1개."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from .events import Event
from .fileclass import FileInfo

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files(
    id INTEGER PRIMARY KEY, path TEXT, name TEXT, category TEXT,
    alg_kind TEXT, alg_idx INTEGER, channel TEXT, size INTEGER,
    parsed INTEGER DEFAULT 0, records INTEGER DEFAULT 0, events INTEGER DEFAULT 0);
CREATE TABLE events(
    id INTEGER PRIMARY KEY, file_id INTEGER, ts REAL, ts_text TEXT, kind TEXT,
    level TEXT, obj_id TEXT, inner_id TEXT, product_id TEXT, roi_idx INTEGER,
    alg_idx INTEGER, block TEXT, model TEXT, value REAL, status TEXT,
    name TEXT, alg_list TEXT, extra TEXT, line_no INTEGER);
CREATE INDEX ix_events_kind ON events(kind);
CREATE INDEX ix_events_inner ON events(inner_id);
CREATE INDEX ix_events_ts ON events(ts);
CREATE TABLE inspections(
    inner_id TEXT PRIMARY KEY, product_id TEXT, start_ts REAL, start_text TEXT,
    wait_threads INTEGER, ack_status TEXT, end_ts REAL, end_text TEXT,
    end_result TEXT, status TEXT, duration_s REAL,
    n_fed INTEGER, n_done INTEGER, n_lost INTEGER, n_nofeed INTEGER,
    n_skipped INTEGER, n_zones INTEGER, n_zones_done INTEGER,
    lost_channels TEXT, nofeed_channels TEXT,
    remain_list TEXT, gen_id INTEGER);
CREATE TABLE model_loads(
    id INTEGER PRIMARY KEY, kind TEXT, ts REAL, ts_text TEXT, name TEXT,
    alg_idx INTEGER, dur_s REAL, status TEXT);
CREATE TABLE channel_runs(
    id INTEGER PRIMARY KEY, inner_id TEXT, alg_idx INTEGER, channel TEXT,
    exec_no INTEGER, feed_ts REAL, feed_text TEXT, roi_idx INTEGER,
    pre_ms REAL, infer_start_ts REAL, infer_end_ts REAL, infer_ms REAL,
    post_ms REAL, model TEXT, status TEXT);
CREATE INDEX ix_runs_inner ON channel_runs(inner_id);
CREATE INDEX ix_runs_alg ON channel_runs(alg_idx);
CREATE TABLE process_gens(
    id INTEGER PRIMARY KEY, start_ts REAL, start_text TEXT,
    end_ts REAL, end_text TEXT, end_cause TEXT);
CREATE TABLE recipe_algs(
    idx INTEGER PRIMARY KEY, name TEXT, alg_id TEXT, alg_type TEXT,
    roi_idx TEXT, dl_model TEXT);
CREATE TABLE recipe_models(
    idx INTEGER PRIMARY KEY, name TEXT, model_file TEXT,
    dev_index INTEGER, instance_count INTEGER, infer_dll TEXT);
"""


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=OFF;")
    con.executescript(_SCHEMA)
    return con


def insert_file(con: sqlite3.Connection, fi: FileInfo, parsed: bool,
                records: int, n_events: int) -> int:
    cur = con.execute(
        "INSERT INTO files(path,name,category,alg_kind,alg_idx,channel,size,parsed,records,events) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (fi.path, fi.name, fi.category, fi.alg_kind, fi.alg_idx, fi.channel,
         fi.size, int(parsed), records, n_events))
    return cur.lastrowid


def insert_events(con: sqlite3.Connection, evs: Iterable[Event]):
    con.executemany(
        "INSERT INTO events(file_id,ts,ts_text,kind,level,obj_id,inner_id,product_id,"
        "roi_idx,alg_idx,block,model,value,status,name,alg_list,extra,line_no) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(e.file_id, e.ts, e.ts_text, e.kind, e.level, e.obj_id, e.inner_id,
          e.product_id, e.roi_idx, e.alg_idx, e.block, e.model, e.value,
          e.status, e.name, e.alg_list, e.extra, e.line_no) for e in evs])

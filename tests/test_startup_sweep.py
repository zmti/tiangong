"""启动清扫:后端重启时把遗留 running 孤儿标记 interrupted。

核心防复发逻辑:后端崩溃/重启会让 in-flight run 永远停在 running(无启动清扫),
前端恢复时读到 running 而非终态 → 永远 loading。该测试验证 _sweep_orphan_runs
能安全改写,且不依赖外部状态(异常被吞,不影响启动)。
"""
import os
import sqlite3
import time

import pytest


def _seed(db_path: str, rows):
    """构造 checkpoints.db 并插入若干 run(state 顶层 status 与列 status 一致)。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            "run_id TEXT PRIMARY KEY, node TEXT NOT NULL, status TEXT NOT NULL, "
            "session_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS checkpoint_versions ("
            "run_id TEXT NOT NULL, version INTEGER NOT NULL, node TEXT NOT NULL, "
            "status TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        now = time.time()
        for i, (rid, status, age) in enumerate(rows):
            state = '{"status": "%s", "answer": "", "metadata": {}}' % status
            con.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(run_id,node,status,session_id,state,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, "llm", status, "s", state, 1, now - age, now - age),
            )
        con.commit()
    finally:
        con.close()


def test_sweep_marks_orphan_running_interrupted(tmp_path, monkeypatch):
    from app import main as m

    db = tmp_path / "checkpoints.db"
    # 三个孤儿(远超 5s 阈值) + 一个很新的 run(不应被清)
    _seed(
        str(db),
        [
            ("old1", "running", 3600),
            ("old2", "running", 120),
            ("old3", "running", 1000),
            ("fresh", "running", 1),  # updated_at 在 now-5s 内,保留
            ("done1", "done", 3600),
        ],
    )
    m._sweep_orphan_runs(str(db))

    con = sqlite3.connect(str(db))
    try:
        res = {
            r[0]: r[1]
            for r in con.execute("select run_id, status from checkpoints")
        }
    finally:
        con.close()
    assert res["old1"] == "interrupted"
    assert res["old2"] == "interrupted"
    assert res["old3"] == "interrupted"
    assert res["fresh"] == "running"  # 最近 5s 内的 run 不被误伤
    assert res["done1"] == "done"

    # state JSON 顶层 status 也同步改写
    con = sqlite3.connect(str(db))
    try:
        st = con.execute(
            "select json_extract(state,'$.status') from checkpoints where run_id='old1'"
        ).fetchone()[0]
        swept = con.execute(
            "select json_extract(state,'$.metadata.swept_by_startup') from checkpoints where run_id='old1'"
        ).fetchone()[0]
    finally:
        con.close()
    assert st == "interrupted"
    assert swept == 1


def test_sweep_does_not_raise_when_db_missing(tmp_path, monkeypatch):
    """库文件不存在时静默跳过,不抛异常(不影响启动)。"""
    from app import main as m

    # 不应抛异常
    m._sweep_orphan_runs(str(tmp_path / "nonexistent.db"))


def test_sweep_swallows_errors(tmp_path, monkeypatch):
    """db 路径指向一个目录(无法打开)时,异常被吞,不影响启动。"""
    from app import main as m

    # 把 db 路径指向一个已存在的目录,迫使 sqlite3.connect 失败 → 异常被吞
    bad = tmp_path / "dir_as_db"
    bad.mkdir()
    m._sweep_orphan_runs(str(bad))

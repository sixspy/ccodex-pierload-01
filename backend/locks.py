"""码头锁与争用登记（写入路径嵌入）。"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])

# 进程内按锁键串行化，闭合 SQLite 读-插竞态窗口（库文件路径保持原样）
_key_guards: dict[str, threading.RLock] = {}
_guards_guard = threading.Lock()


class LockBusy(RuntimeError):
    """非阻塞抢锁失败：地磅正被另一会话占用。"""


def _guard_for(lock_key: str) -> threading.RLock:
    with _guards_guard:
        g = _key_guards.get(lock_key)
        if g is None:
            g = threading.RLock()
            _key_guards[lock_key] = g
        return g


@contextmanager
def key_section(lock_key: str):
    guard = _guard_for(lock_key)
    guard.acquire()
    try:
        yield
    finally:
        guard.release()


@contextmanager
def try_key_section(lock_key: str):
    """非阻塞临界区：抢不到立即抛 LockBusy，后到会话不排队、只落争用痕。"""
    guard = _guard_for(lock_key)
    if not guard.acquire(blocking=False):
        raise LockBusy(lock_key)
    try:
        yield
    finally:
        guard.release()


def log_contention(conn, lock_key: str, actor: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor, "争用", detail, now_iso()),
    )


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO pier_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        return True
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM pier_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pier_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        with key_section(body.lock_key):
            ok = try_acquire(conn, body.lock_key, user)
            write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
            conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        with key_section(body.lock_key):
            release(conn, body.lock_key, user)
            write_audit(conn, user, "lock_release", body.lock_key, {})
            conn.commit()
        return {"ok": True}
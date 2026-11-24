"""过期入票命运册（港闸案甲）。

甲路径：校准失效地磅的过磅写口整笔回滚拒收，正式册零增；
拒收命运在同一事务内落入本册并按当班 shift_key 锁死：
- 同班次同地磅同票号唯一，禁止重写（偏航冲击只记审计，不写本册）；
- 不提供任何改写/删除出口，命运一旦落册当班不可变。
"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect, now_iso

router = APIRouter(tags=["expired_fates"])


def current_shift_key() -> str:
    """当班键：自然日班次。落册后该班次内命运唯一锁死。"""
    return f"shift:{now_iso()[:10]}"


def record_expired_fate(
    conn,
    *,
    bridge_code: str,
    lane_code: str,
    ticket_code: str,
    bl_ref: str,
    gross_t: float | None,
    reason: str,
    actor: str,
) -> tuple[bool, str]:
    """返回 (是否落册, 决策说明)。偏航（重复冲击）拒写命运册。"""
    shift_key = current_shift_key()
    exists = conn.execute(
        """SELECT id FROM expired_ticket_fates
           WHERE shift_key=? AND bridge_code=? AND ticket_code=?""",
        (shift_key, bridge_code, ticket_code),
    ).fetchone()
    if exists:
        return False, "命运册当班已锁，偏航拒写"
    conn.execute(
        """INSERT INTO expired_ticket_fates(shift_key,bridge_code,lane_code,ticket_code,
           bl_ref,gross_t,decision,path_taken,reason,actor,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            shift_key,
            bridge_code,
            lane_code,
            ticket_code,
            bl_ref,
            gross_t,
            "拒收",
            "甲",
            reason,
            actor,
            now_iso(),
        ),
    )
    return True, "拒收命运已落册并当班锁死"


@router.get("/api/fates/expired")
def list_expired_fates(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expired_ticket_fates ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

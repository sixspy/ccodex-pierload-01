"""过磅票：轴重采集与裁定入闸。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from expired_fate_svc import record_expired_fate
from locks import LockBusy, log_contention, release, try_acquire, try_key_section
from models import TicketIn
from validators import (
    adjudicate_overload,
    calibration_expired,
    validate_axle_count,
    validate_axle_weights,
    validate_bridge_calibrated,
    validate_gross_cap,
    validate_lane_open,
)

router = APIRouter(tags=["tickets"])

# 并发缝：测试可注入持锁窗口回调，生产为 None（空操作）
bridge_lock_barrier = None


def _next_ticket_code(conn, lane_id: int) -> str:
    lane = conn.execute(
        "SELECT lane_code FROM weigh_lanes WHERE id=?", (lane_id,)
    ).fetchone()
    prefix = lane["lane_code"] if lane else f"LN-{lane_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE lane_id=?", (lane_id,)
    ).fetchone()["c"]
    return f"WT-{prefix}-{n + 1:03d}"


@router.get("/api/tickets")
def list_tickets(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT t.*, l.lane_code, b.bridge_code,
                      b.calibration_status, b.calibration_due
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id"""
        if status:
            rows = conn.execute(
                q + " WHERE t.status=? ORDER BY t.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY t.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["axle_weights"] = json.loads(d["axle_weights"] or "[]")
            d["bridge_calibration_expired"] = calibration_expired(d)
            items.append(d)
        return {"items": items}


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT t.*, l.lane_code, l.overload_threshold_t, b.name bridge_name,
                      b.bridge_code, b.calibration_status, b.calibration_due
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE t.id=?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "过磅票不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        data["bridge_calibration_expired"] = calibration_expired(data)
        holds = conn.execute(
            "SELECT * FROM overload_holds WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM gate_releases WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        return {
            "ticket": data,
            "overload_holds": [dict(h) for h in holds],
            "gate_seals": [dict(s) for s in seals],
            "gate_releases": [dict(r) for r in releases],
        }


@router.post("/api/tickets")
def create_ticket(body: TicketIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        # 先定位地磅，抢占地磅级锁（同磅双会话只一单成功）
        anchor = conn.execute(
            """SELECT b.id bridge_id
               FROM weigh_lanes l JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE l.id=?""",
            (body.lane_id,),
        ).fetchone()
        if not anchor:
            raise HTTPException(404, "车道不存在")
        bridge_id = anchor["bridge_id"]
        lock_key = f"bridge:{bridge_id}"
        try:
            # 非阻塞抢地磅级 RLock：同磅后到会话不排队，当场 409 只落争用痕
            with try_key_section(lock_key):
                # 锁行短事务立即落盘并提交，不长持库写锁
                acquired = try_acquire(conn, lock_key, user)
                conn.commit()
                if not acquired:
                    raise HTTPException(409, "争用：后到只记争用，不写业务")
                if bridge_lock_barrier is not None:
                    bridge_lock_barrier()
                try:
                    lane = conn.execute(
                        """SELECT l.*, b.bridge_code, b.max_gross_t, b.axle_limit,
                                  b.calibration_status, b.calibration_due
                           FROM weigh_lanes l
                           JOIN weigh_bridges b ON b.id=l.bridge_id WHERE l.id=?""",
                        (body.lane_id,),
                    ).fetchone()
                    lane_d = dict(lane)

                    # 甲路径闸口：校准失效 → 整笔回滚拒收，正式册零增
                    if calibration_expired(lane_d):
                        code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
                        axles = [a.model_dump() for a in body.axle_weights]
                        try:
                            gross = round(sum(float(a["w"]) for a in axles), 3)
                        except (TypeError, ValueError):
                            gross = None
                        reason = (
                            f"地磅{lane_d['bridge_code']}校准截止"
                            f"{lane_d.get('calibration_due') or '未登记'}已失效，禁入正式册"
                        )
                        landed, fate_note = record_expired_fate(
                            conn,
                            bridge_code=lane_d["bridge_code"],
                            lane_code=lane_d["lane_code"],
                            ticket_code=code,
                            bl_ref=body.bl_ref,
                            gross_t=gross,
                            reason=reason,
                            actor=user,
                        )
                        action = (
                            "expired_ticket_reject" if landed
                            else "expired_ticket_yaw"
                        )
                        write_audit(
                            conn,
                            user,
                            action,
                            code,
                            {
                                "bridge_code": lane_d["bridge_code"],
                                "lane_code": lane_d["lane_code"],
                                "gross_t": gross,
                                "path_taken": "甲",
                                "fate": fate_note,
                            },
                        )
                        # 先落盘命运与审计、释放地磅锁，再抛拒收（业务零写）
                        conn.commit()
                        release(conn, lock_key, user)
                        conn.commit()
                        raise HTTPException(
                            409,
                            f"{reason}；{fate_note}，正式册零增整笔拒收",
                        )

                    validate_bridge_calibrated(lane_d)
                    validate_lane_open(lane_d)
                    axles = [a.model_dump() for a in body.axle_weights]
                    validate_axle_count(axles, int(lane["axle_limit"]))
                    gross, _peak, _spread = validate_axle_weights(axles)
                    validate_gross_cap(gross, float(lane["max_gross_t"]))
                    bl = conn.execute(
                        "SELECT * FROM bl_manifests WHERE bl_code=?", (body.bl_ref,)
                    ).fetchone()
                    if not bl:
                        raise HTTPException(422, "提单册未登记")
                    is_over, overweight = adjudicate_overload(
                        gross, float(lane["overload_threshold_t"])
                    )
                    code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
                    if conn.execute(
                        "SELECT id FROM weigh_tickets WHERE ticket_code=?", (code,)
                    ).fetchone():
                        raise HTTPException(409, "票号已存在")
                    now = now_iso()
                    if is_over:
                        status = "旁路挂起"
                    else:
                        status = "已裁定"
                    cur = conn.execute(
                        """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                           axle_weights,gross_t,generation,status,adjudicated_at,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            code,
                            body.lane_id,
                            body.bl_ref,
                            body.vehicle_plate,
                            json.dumps(axles, ensure_ascii=False),
                            gross,
                            1,
                            status,
                            now if status == "已裁定" else None,
                            now,
                        ),
                    )
                    ticket_id = cur.lastrowid
                    if is_over:
                        hold_key = f"HOLD-{code}"
                        conn.execute(
                            """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
                               status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
                            (
                                ticket_id,
                                hold_key,
                                overweight,
                                float(lane["overload_threshold_t"]),
                                "开放",
                                now,
                                lane["side_lane_ref"] or "旁路待驳",
                            ),
                        )
                    write_audit(
                        conn,
                        user,
                        "ticket_weigh",
                        code,
                        {
                            "ticket_id": ticket_id,
                            "gross_t": gross,
                            "status": status,
                            "overload": is_over,
                        },
                    )
                    conn.commit()
                    return {
                        "id": ticket_id,
                        "ticket_code": code,
                        "gross_t": gross,
                        "status": status,
                        "overload": is_over,
                    }
                finally:
                    release(conn, lock_key, user)
                    conn.commit()
        except LockBusy:
            # 进程内非阻塞抢锁失败：后到只落争用痕，正式册零写。
            # 复用当前连接（不另抢库写锁，避免与持锁会话跨连接等待）
            log_contention(conn, lock_key, user, "地磅占用：非阻塞抢锁失败")
            conn.commit()
            raise HTTPException(409, "争用：地磅被占用，后到只记争用，不写业务")

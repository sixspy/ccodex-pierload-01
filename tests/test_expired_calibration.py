"""港闸案甲：地磅校准过期禁入票（红测链第 1 号文件）。

口径（甲路径，禁止与乙路径影子草稿混用）：
- 过期地磅过磅写口整笔回滚拒收，正式册 weigh_tickets 零增；
- 拒收命运写入 expired_ticket_fates，当班 shift_key 锁死，偏航只记审计、拒写命运册；
- 同磅双会话抢建须占地磅级锁，后到只落 contention_log；
- 总览/地磅/过磅/对账/车道五页账实同源，不见过期入票假绿。
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

GROSS = 36.6
PAYLOAD_AXLES = [
    {"axle": 1, "w": 9.0},
    {"axle": 2, "w": 9.6},
    {"axle": 3, "w": 9.2},
    {"axle": 4, "w": 8.8},
]


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _bridge_and_lane(c: TestClient, h: dict):
    bridge = next(
        b for b in c.get("/api/bridges", headers=h).json()["items"]
        if b["bridge_code"] == "BR-W01"
    )
    lane = next(
        l for l in c.get("/api/lanes", headers=h).json()["items"]
        if l["lane_code"] == "LN-W01-IN"
    )
    return bridge, lane


def _post_pl_a01(c: TestClient, h: dict, lane_id: int):
    return c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane_id,
            "bl_ref": "BL-2026-0105",
            "vehicle_plate": "港A·W01试",
            "axle_weights": PAYLOAD_AXLES,
            "ticket_code": "PL-A01",
        },
    )


def _official_count() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM weigh_tickets WHERE ticket_code='PL-A01'"
        ).fetchone()["c"]


def _fate_rows() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expired_ticket_fates WHERE ticket_code='PL-A01' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 红测甲：过期地磅过磅，正式册零增并整笔回滚拒收（失败断言：过期仍增正式行）
# ---------------------------------------------------------------------------
def test_expired_calibration_rejects_ticket_official_ledger_unchanged():
    c = TestClient(app)
    h = _auth(c)
    _bridge, lane = _bridge_and_lane(c, h)

    before = c.get("/api/dashboard", headers=h).json()["tickets"]
    res = _post_pl_a01(c, h, lane["id"])

    assert res.status_code == 409, res.text
    assert "校准" in res.json()["detail"]
    # 正式册零增：PL-A01 不得落入 weigh_tickets
    assert _official_count() == 0
    after = c.get("/api/dashboard", headers=h).json()["tickets"]
    assert after == before
    # 命运册当班锁死：同一班次仅一条拒收命运，不可重写
    fates = _fate_rows()
    assert len(fates) == 1
    fate = fates[0]
    assert fate["bridge_code"] == "BR-W01"
    assert fate["decision"] == "拒收"
    assert fate["path_taken"] == "甲"
    assert abs(float(fate["gross_t"]) - GROSS) < 1e-6
    assert fate["shift_key"]


def test_expired_ticket_gross_not_accumulated_in_reconcile():
    """红测乙：失败断言对账仍累计该毛重。"""
    c = TestClient(app)
    h = _auth(c)
    _bridge, lane = _bridge_and_lane(c, h)

    res = _post_pl_a01(c, h, lane["id"])
    assert res.status_code == 409

    recon = c.get("/api/reconcile/BL-2026-0105", headers=h).json()
    assert recon["ticket_sum_t"] == 0
    codes = [t["ticket_code"] for t in recon["tickets"]]
    assert "PL-A01" not in codes
    assert all(abs(float(t.get("gross_t", 0)) - GROSS) > 1e-6 for t in recon["tickets"])
    # 单票证明同样查无此票
    assert c.get("/api/proof/ticket/PL-A01", headers=h).status_code == 404


def test_concurrent_ticket_creation_requires_bridge_lock():
    """争用红测：双会话同磅抢建，须占地磅级锁；后到只落争用痕，无锁双成即红。"""
    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    h2 = _auth(c, "writer", "writer123")
    bridge, lane = _bridge_and_lane(c, h1)
    bridge_lock = f"bridge:{bridge['id']}"

    # 会话一先占该地磅锁（模拟两路并发中先到的一路持锁建票）
    got = c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": bridge_lock}
    )
    assert got.status_code == 200

    # 会话二后到同磅建票：必须 409，不得写任何业务行
    res = _post_pl_a01(c, h2, lane["id"])
    assert res.status_code == 409, res.text
    assert _official_count() == 0

    contention = c.get("/api/contention", headers=h1).json()["items"]
    hits = [x for x in contention if x["lock_key"] == bridge_lock]
    assert len(hits) == 1
    assert hits[0]["actor"] == "writer"

    # 锁释放后同磅恢复可争用（锁确实是地磅级、可释放，不残留）
    assert c.post(
        "/api/locks/release", headers=h1, json={"lock_key": bridge_lock}
    ).status_code == 200
    assert _official_count() == 0


def test_two_threads_racing_same_bridge_only_one_wins(monkeypatch):
    """真并发：两路真实 HTTP 请求同磅同时建票，后到非阻塞抢锁失败只落争用痕。"""
    import httpx
    import uvicorn
    from uvicorn import Config

    import tickets_svc

    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    # 换一台校准有效地磅，才能测出“成功单”与“争用单”的分野
    healthy_lane = next(
        l for l in c.get("/api/lanes", headers=h1).json()["items"]
        if l["lane_code"] == "LN-N-01-IN"
    )

    # 并发缝：两事件构造确定性重叠——t1 持锁等待，t2 抢锁被拒后放行 t1
    entered = threading.Event()      # t1 已占地磅锁、停在持锁窗口
    second_done = threading.Event()   # t2 已完成（预期 409）

    def _barrier():
        entered.set()
        second_done.wait(timeout=10)

    monkeypatch.setattr(tickets_svc, "bridge_lock_barrier", _barrier)

    class _NoSignalServer(uvicorn.Server):
        def install_signal_handlers(self):
            pass

    server = _NoSignalServer(
        Config(app=app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

        def token(user, password):
            r = httpx.post(
                f"{base}/api/login",
                json={"username": user, "password": password}, timeout=10,
            )
            return {"Authorization": "Bearer " + r.json()["token"]}

        headers_a = token("admin", "admin123")
        headers_b = token("writer", "writer123")
        results: list[int] = []

        def post(auth_header: dict, code: str):
            r = httpx.post(
                f"{base}/api/tickets",
                headers=auth_header,
                json={
                    "lane_id": healthy_lane["id"],
                    "bl_ref": "BL-2026-0101",
                    "vehicle_plate": f"并A·{code}",
                    "axle_weights": [{"axle": 1, "w": 8.0}, {"axle": 2, "w": 8.0}],
                    "ticket_code": code,
                },
                timeout=10,
            )
            results.append(r.status_code)

        t1 = threading.Thread(target=post, args=(headers_a, "WT-RACE-001"))
        t2 = threading.Thread(target=post, args=(headers_b, "WT-RACE-002"))
        t1.start()
        # 等先到请求已占地磅锁、停在持锁窗口
        assert entered.wait(timeout=10)
        # 后到请求在另一 worker 上抢同一地磅：非阻塞失败立即返回 409
        t2.start()
        t2.join(timeout=10)
        # t2 落定后再放行持锁的 t1
        second_done.set()
        t1.join(timeout=10)

        assert sorted(results) == [200, 409]
        with connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) c FROM weigh_tickets WHERE ticket_code LIKE 'WT-RACE-%'"
            ).fetchone()["c"]
            assert rows == 1
            cont = conn.execute(
                "SELECT * FROM contention_log WHERE lock_key=?",
                (f"bridge:{healthy_lane['bridge_id']}",),
            ).fetchall()
            assert len(cont) >= 1
            assert any(r["actor"] == "writer" for r in cont)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# 命运册偏航：同票二次冲击，命运不重写、正式册仍零增
# ---------------------------------------------------------------------------
def test_repeated_expired_attempt_is_yaw_rejected_and_fate_locked():
    c = TestClient(app)
    h = _auth(c)
    _bridge, lane = _bridge_and_lane(c, h)

    first = _post_pl_a01(c, h, lane["id"])
    second = _post_pl_a01(c, h, lane["id"])
    assert first.status_code == 409
    assert second.status_code == 409

    fates = _fate_rows()
    assert len(fates) == 1, "偏航冲击不得重写命运册"
    assert fates[0]["decision"] == "拒收"
    assert _official_count() == 0

    with connect() as conn:
        audits = conn.execute(
            "SELECT action FROM audit_trail WHERE target='PL-A01' ORDER BY id"
        ).fetchall()
    actions = [a["action"] for a in audits]
    assert "expired_ticket_reject" in actions
    assert "expired_ticket_yaw" in actions

    # 命运册只读接口可见
    listing = c.get("/api/fates/expired", headers=h).json()["items"]
    assert any(f["ticket_code"] == "PL-A01" for f in listing)


# ---------------------------------------------------------------------------
# 五页账实同源：总览/地磅/过磅/对账/车道
# ---------------------------------------------------------------------------
def test_five_views_hide_expired_fake_green():
    c = TestClient(app)
    h = _auth(c)
    bridge, lane = _bridge_and_lane(c, h)
    assert _post_pl_a01(c, h, lane["id"]).status_code == 409

    # ① 总览页：过期地磅显式计数，过磅票总数不增
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["expired_bridges"] >= 1
    assert dash["expired_fate_rejections"] >= 1
    assert dash["tickets"] == 4  # 仅种子四票

    # ② 地磅页：BR-W01 校准红灯为真
    br_row = next(
        b for b in c.get("/api/bridges", headers=h).json()["items"]
        if b["bridge_code"] == "BR-W01"
    )
    assert br_row["calibration_expired"] is True
    assert br_row["calibration_status"] == "校准过期"

    # ③ 过磅页：PL-A01 不得出现在正式票列表
    ticket_codes = [
        t["ticket_code"] for t in c.get("/api/tickets", headers=h).json()["items"]
    ]
    assert "PL-A01" not in ticket_codes

    # ④ 对账页：毛重不累计
    recon = c.get("/api/reconcile/BL-2026-0105", headers=h).json()
    assert recon["ticket_sum_t"] == 0

    # ⑤ 车道页：LN-W01-IN 透出所属地磅校准失效，不允许假绿
    lane_row = next(
        l for l in c.get("/api/lanes", headers=h).json()["items"]
        if l["lane_code"] == "LN-W01-IN"
    )
    assert lane_row["bridge_calibration_expired"] is True
    assert lane_row["bridge_id"] == bridge["id"]


def test_healthy_bridge_still_accepts_ticket():
    """护栏：禁入只打过期地磅，校准有效地磅过磅流不受影响。"""
    c = TestClient(app)
    h = _auth(c)
    healthy_lane = next(
        l for l in c.get("/api/lanes", headers=h).json()["items"]
        if l["lane_code"] == "LN-N-01-IN"
    )
    r = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": healthy_lane["id"],
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "沪A·正常01",
            "axle_weights": [{"axle": 1, "w": 7.0}, {"axle": 2, "w": 8.0}],
            "ticket_code": "WT-HEALTHY-001",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已裁定"

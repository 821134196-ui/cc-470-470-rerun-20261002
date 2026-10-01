"""平台规则测试：启动互斥、检查完整、封控阻断、职责分离、事件时序，以及 HTTP 端到端。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

from escalator.app import Handler, create_app
from escalator.clock import FakeClock
from escalator.db import connect, init
from escalator.adapters import CabinetAdapter, CrowdAdapter
from escalator.scheduler import Scheduler
from escalator.services import (EscalatorService, DomainError, CHECK_ITEMS,
                                BARRIER_GRACE_MIN, REPAIR_SLA_MIN)

OP1, OP2 = "op1", "op2"
INSP1, INSP2 = "insp1", "insp2"
REP1 = "rep1"
SUP = "sup1"
SVC = "svc1"


def make_world():
    tmp = tempfile.mkdtemp(prefix="esc-test-")
    db = connect(os.path.join(tmp, "t.db"))
    init(db)
    clock = FakeClock(datetime(2026, 10, 1, 9, 50))
    cab, crowd = CabinetAdapter(), CrowdAdapter()
    svc = EscalatorService(db, clock, cab, crowd, os.path.join(tmp, "photos"))
    svc.seed_if_empty()
    sched = Scheduler(svc, interval=999)
    return tmp, db, clock, cab, crowd, svc, sched


def pass_checks(svc, esc="EA", who=INSP1):
    for key, _ in CHECK_ITEMS[:-1]:
        svc.submit_check(esc, who, key, "pass")
    assert svc.estop_test(esc, who)["ok"]


def get_esc(svc, esc_id):
    return next(e for e in svc.list_escalators() if e["id"] == esc_id)


def open_ticket(svc, esc):
    return svc.list_tickets(esc, open_only=True)[0]["id"]


# ============================================================ 启动互斥
class StartMutexTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, _ = make_world()

    def test_two_operators_cannot_start_concurrently(self):
        pass_checks(self.svc, "EA")
        v = get_esc(self.svc, "EA")["version"]
        release = threading.Event()
        # 第一人进入控制柜调用（仍持有该梯启动锁/启动占位）时挂起，制造并发窗口
        self.cab.on_start = lambda esc: release.wait(timeout=3)
        results = []

        def starter(who):
            try:
                self.svc.start("EA", who, version=v)
                results.append("ok")
            except DomainError as e:
                results.append(e.code)

        t1 = threading.Thread(target=starter, args=(OP1,))
        t2 = threading.Thread(target=starter, args=(OP2,))
        t1.start()
        time.sleep(0.3)          # 等 t1 进入启动临界区
        t2.start()
        t2.join(5)               # t2 必须被互斥拒绝
        release.set()
        t1.join(5)
        self.assertEqual(results, ["START_IN_PROGRESS", "ok"],
                         "第二人须被互斥拒绝，第一人随后成功")
        self.assertEqual(get_esc(self.svc, "EA")["state"], "running")

    def test_stale_version_rejected(self):
        pass_checks(self.svc, "EB")
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EB", OP1, version=999)
        self.assertEqual(cm.exception.code, "STALE_VERSION")

    def test_wrong_direction_rejected(self):
        pass_checks(self.svc, "EB")  # EB 计划下行
        v = get_esc(self.svc, "EB")["version"]
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EB", OP1, direction="up", version=v)
        self.assertEqual(cm.exception.code, "WRONG_DIRECTION")

    def test_direction_change_bumps_version_and_blocks_running_change(self):
        self.svc.set_direction("EA", SUP, "down")
        self.assertEqual(get_esc(self.svc, "EA")["planned_direction"], "down")
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        with self.assertRaises(DomainError) as cm:
            self.svc.set_direction("EA", SUP, "up")
        self.assertEqual(cm.exception.code, "RUNNING")


# ============================================================ 班前检查完整性
class ChecklistTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, _ = make_world()

    def test_missing_item_blocks_start(self):
        for key, _ in CHECK_ITEMS[:3]:  # 只做 3 项
            self.svc.submit_check("EA", INSP1, key, "pass")
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EA", OP1)
        self.assertEqual(cm.exception.code, "CHECK_INCOMPLETE")
        st = self.svc.check_status("EA")
        self.assertIn("lighting", st["missing"])
        self.assertIn("estop", st["missing"])
        self.assertFalse(st["ready"])

    def test_failed_item_blocks_start(self):
        for key, _ in CHECK_ITEMS[:-1]:
            self.svc.submit_check("EA", INSP1, key, "pass" if key != "comb" else "fail",
                                  note="梳齿缺齿")
        self.assertTrue(self.svc.estop_test("EA", INSP1)["ok"])
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EA", OP1)
        self.assertEqual(cm.exception.code, "CHECK_FAILED")

    def test_na_counts_as_qualified(self):
        for key, _ in CHECK_ITEMS[:-1]:
            self.svc.submit_check("EA", INSP1, key, "na" if key == "lighting" else "pass")
        self.assertTrue(self.svc.estop_test("EA", INSP1)["ok"])
        self.svc.start("EA", OP1)
        self.assertEqual(get_esc(self.svc, "EA")["state"], "running")

    def test_bad_result_rejected(self):
        with self.assertRaises(DomainError):
            self.svc.submit_check("EA", INSP1, "steps", "maybe")


# ============================================================ 急停测试失败独立处理
class EstopFailTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, _ = make_world()

    def test_estop_fail_locks_and_reports(self):
        for key, _ in CHECK_ITEMS[:-1]:
            self.svc.submit("EA", INSP1, key, "pass") if False else \
                self.svc.submit_check("EA", INSP1, key, "pass")
        self.cab.set_estop_faulty("EA", True)
        r = self.svc.estop_test("EA", INSP1)
        self.assertFalse(r["ok"])
        e = get_esc(self.svc, "EA")
        self.assertEqual(e["state"], "locked")
        self.assertEqual(e["lock_reason"], "estop")
        self.assertTrue(e["barrier_required"])
        self.assertTrue(self.svc.list_tickets("EA", open_only=True))
        types = [x["type"] for x in self.svc.list_events("EA")]
        self.assertIn("ESTOP_TEST_FAIL", types)
        # 急停失败的梯不能启动
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EA", OP1)
        self.assertEqual(cm.exception.code, "LOCKED")


# ============================================================ 封控阻断
class LockdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, self.sched = make_world()

    def _full_fault(self, esc="EA"):
        pass_checks(self.svc, esc)
        self.svc.start(esc, OP1)
        tid = self.svc.report_fault(esc, INSP1, "CHAIN_FAULT", "链条异响",
                                    photos=["shot1.jpg"])
        return tid

    def test_fault_locks_and_requires_barrier(self):
        tid = self._full_fault()
        e = get_esc(self.svc, "EA")
        self.assertEqual(e["state"], "locked")
        self.assertTrue(e["barrier_required"])
        self.assertFalse(e["barrier_erected"])
        t = self.svc.ticket_detail(tid)
        self.assertEqual(t["status"], "reported")

    def test_barrier_overdue_alert(self):
        self._full_fault()
        self.clock.advance(minutes=BARRIER_GRACE_MIN + 1)
        self.svc.check_timeouts()
        types = [x["type"] for x in self.svc.list_events("EA")]
        self.assertIn("BARRIER_OVERDUE", types)
        # 只告警一次
        self.clock.advance(minutes=5)
        self.svc.check_timeouts()
        self.assertEqual(
            [x["type"] for x in self.svc.list_events("EA")].count("BARRIER_OVERDUE"), 1)

    def test_remove_barrier_before_approval_blocked(self):
        self._full_fault()
        self.svc.erect_barrier("EA", OP1)
        with self.assertRaises(DomainError) as cm:
            self.svc.remove_barrier("EA", OP1)
        self.assertEqual(cm.exception.code, "REPAIR_NOT_APPROVED")

    def test_early_reopen_blocked_and_logged(self):
        tid = self._full_fault()
        self.svc.erect_barrier("EA", OP1)
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        rid = self.svc.submit_recheck(tid, INSP2, {k: "pass" for k, _ in CHECK_ITEMS})
        self.svc.review_recheck(rid, INSP1, True)
        # 复检通过但围挡未撤、绕行未发——复开应被阻断
        with self.assertRaises(DomainError) as cm:
            self.svc.reopen("EA", OP1)
        self.assertEqual(cm.exception.code, "EARLY_REOPEN")
        types = [x["type"] for x in self.svc.list_events("EA")]
        self.assertIn("EARLY_REOPEN_BLOCKED", types)

    def test_full_reopen_flow(self):
        tid = self._full_fault()
        self.svc.erect_barrier("EA", OP1)
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1, note="换链节")
        rid = self.svc.submit_recheck(tid, INSP1, {k: "pass" for k, _ in CHECK_ITEMS})
        self.svc.review_recheck(rid, INSP2, True)
        self.svc.remove_barrier("EA", OP1)
        v = get_esc(self.svc, "EA")["version"]
        self.svc.reopen("EA", OP1, version=v)
        self.assertEqual(get_esc(self.svc, "EA")["state"], "running")
        self.assertEqual(self.svc.ticket_detail(tid)["status"], "closed")

    def test_cabinet_trip_picked_up_by_scheduler(self):
        pass_checks(self.svc, "EC")
        self.svc.start("EC", OP1)
        self.cab.trip("EC", "OVERSPEED")
        self.sched.tick()
        self.assertEqual(get_esc(self.svc, "EC")["state"], "locked")
        self.assertEqual(self.svc.list_tickets("EC", open_only=True)[0]["fault_code"],
                         "OVERSPEED")

    def test_repeated_trips_auto_escalate(self):
        def cycle():
            tid = open_ticket_x(self.svc, "EC")
            self.svc.erect_barrier("EC", OP1)
            self.svc.dispatch_ticket(tid, SUP, REP1)
            self.svc.arrive_ticket(tid, REP1)
            self.svc.repair_done(tid, REP1)
            rid = self.svc.submit_recheck(tid, INSP1, {k: "pass" for k, _ in CHECK_ITEMS})
            self.svc.review_recheck(rid, INSP2, True)
            self.svc.remove_barrier("EC", OP1)
            self.svc.reopen("EC", OP1)

        pass_checks(self.svc, "EC")
        self.svc.start("EC", OP1)
        self.cab.trip("EC", "CHAIN_FAULT"); self.sched.tick(); cycle()
        self.clock.advance(hours=2)
        self.cab.trip("EC", "CHAIN_FAULT"); self.sched.tick()
        types = [x["type"] for x in self.svc.list_events("EC")]
        self.assertIn("REPEAT_TRIP", types)
        # 升级后新工单直接 dispatched
        self.assertEqual(self.svc.list_tickets("EC", open_only=True)[0]["status"],
                         "dispatched")

    def test_repair_overdue_and_extension(self):
        tid = self._full_fault("EB")
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.clock.advance(minutes=REPAIR_SLA_MIN + 1)
        self.svc.check_timeouts()
        self.assertTrue(self.svc.ticket_detail(tid)["overdue"])
        types = [x["type"] for x in self.svc.list_events("EB")]
        self.assertIn("REPAIR_OVERDUE", types)
        # 延期后 SLA 顺延
        self.svc.extend_ticket(tid, SUP, 30, "配件调拨")
        self.assertEqual(len(self.svc.ticket_detail(tid)["extensions"]), 1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)

    def test_crowd_lock_flow(self):
        pass_checks(self.svc, "EE")
        self.svc.start("EE", OP1)
        self.crowd.set_density("EE", 0.95)
        self.svc.poll_crowd()
        e = get_esc(self.svc, "EE")
        self.assertEqual(e["state"], "locked")
        self.assertEqual(e["lock_reason"], "crowd")
        # 无工单
        self.assertEqual(self.svc.list_tickets("EE", open_only=True), [])
        # 客流高位时不能撤围挡
        self.svc.erect_barrier("EE", OP1)
        with self.assertRaises(DomainError):
            self.svc.remove_barrier("EE", OP1)
        # 回落后可撤、可复开
        self.crowd.set_density("EE", 0.4)
        self.svc.poll_crowd()
        self.svc.remove_barrier("EE", OP1)
        self.svc.reopen("EE", OP1)
        self.assertEqual(get_esc(self.svc, "EE")["state"], "running")


def open_ticket_x(svc, esc):
    return svc.list_tickets(esc, open_only=True)[0]["id"]


# ============================================================ 职责分离
class SeparationTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, _ = make_world()

    def test_service_can_only_publish_detour(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.start("EA", SVC)
        self.assertEqual(cm.exception.code, "FORBIDDEN")
        with self.assertRaises(DomainError):
            self.svc.erect_barrier("EA", SVC)
        did = self.svc.publish_detour("EA", SVC, "请绕行东翼直梯")
        self.assertTrue(did)

    def test_operator_cannot_publish_detour_or_check(self):
        with self.assertRaises(DomainError):
            self.svc.publish_detour("EA", OP1, "x")
        with self.assertRaises(DomainError):
            self.svc.submit_check("EA", OP1, "steps", "pass")

    def test_inspector_cannot_approve_own_recheck(self):
        pass_checks(self.svc, "EA", who=INSP1)
        self.svc.start("EA", OP1)
        tid = self.svc.report_fault("EA", INSP1, "X", "故障")
        self.svc.erect_barrier("EA", OP1)
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        rid = self.svc.submit_recheck(tid, INSP1, {k: "pass" for k, _ in CHECK_ITEMS})
        with self.assertRaises(DomainError) as cm:
            self.svc.review_recheck(rid, INSP1, True)
        self.assertEqual(cm.exception.code, "SELF_REVIEW")
        # 另一名巡检员可以
        self.svc.review_recheck(rid, INSP2, True)

    def test_recheck_incomplete_rejected(self):
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        tid = self.svc.report_fault("EA", INSP1, "X", "故障")
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        with self.assertRaises(DomainError) as cm:
            self.svc.submit_recheck(tid, INSP1, {"steps": "pass"})
        self.assertEqual(cm.exception.code, "RECHECK_INCOMPLETE")

    def test_recheck_fail_returns_to_repair(self):
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        tid = self.svc.report_fault("EA", INSP1, "X", "故障")
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        res = {k: "pass" for k, _ in CHECK_ITEMS}; res["steps"] = "fail"
        with self.assertRaises(DomainError) as cm:
            self.svc.submit_recheck(tid, INSP1, res)
        self.assertEqual(cm.exception.code, "RECHECK_FAILED")
        self.assertEqual(self.svc.ticket_detail(tid)["status"], "repairing")

    def test_review_reject_returns_ticket(self):
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        tid = self.svc.report_fault("EA", INSP1, "X", "故障")
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        rid = self.svc.submit_recheck(tid, INSP1, {k: "pass" for k, _ in CHECK_ITEMS})
        self.svc.review_recheck(rid, SUP, False, note="仍有异响")
        self.assertEqual(self.svc.ticket_detail(tid)["status"], "repairing")
        self.assertEqual(self.svc.list_rechecks(tid)[0]["status"], "rejected")


# ============================================================ 事件时序
class EventOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.clock, self.cab, self.crowd, self.svc, _ = make_world()

    def test_sequence_and_monotonic(self):
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        self.svc.report_fault("EA", INSP1, "F1", "故障")
        self.svc.erect_barrier("EA", OP1)
        tid = self.svc.list_tickets("EA", open_only=True)[0]["id"]
        self.svc.dispatch_ticket(tid, SUP, REP1)
        self.clock.advance(minutes=10)
        self.svc.arrive_ticket(tid, REP1)
        self.svc.repair_done(tid, REP1)
        rid = self.svc.submit_recheck(tid, INSP1, {k: "pass" for k, _ in CHECK_ITEMS})
        self.svc.review_recheck(rid, INSP2, True)
        self.svc.remove_barrier("EA", OP1)
        self.svc.reopen("EA", OP1)

        rows = self.svc.list_events("EA", limit=500)
        # seq 单调、时间戳不降
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        asc = list(reversed(rows))
        types = [r["type"] for r in asc]
        order = ["ESTOP_TEST_PASS", "START", "LOCK", "BARRIER_ERECTED", "DISPATCH",
                 "ARRIVE", "REPAIR_DONE", "RECHECK_SUBMIT", "RECHECK_APPROVED",
                 "BARRIER_REMOVED", "REOPEN"]
        pos = [types.index(t) for t in order]
        self.assertEqual(pos, sorted(pos), f"事件顺序错误：{order}")
        # 时间戳非降
        stamps = [r["ts"] for r in asc]
        self.assertEqual(stamps, sorted(stamps))

    def test_reopen_version_must_match(self):
        pass_checks(self.svc, "EA")
        self.svc.start("EA", OP1)
        self.svc.report_fault("EA", INSP1, "F", "故障")
        e = get_esc(self.svc, "EA")
        with self.assertRaises(DomainError) as cm:
            self.svc.reopen("EA", OP1, version=e["version"] + 5)
        self.assertEqual(cm.exception.code, "STALE_VERSION")


# ============================================================ HTTP 端到端
class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="esc-http-")
        cls.clock = FakeClock(datetime(2026, 10, 1, 9, 50))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.app = create_app(db_path=os.path.join(cls.tmp, "h.db"),
                             photo_dir=os.path.join(cls.tmp, "photos"),
                             clock=cls.clock, start_scheduler=False)
        Handler.app = cls.app
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _call(self, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read()

    def test_pages_and_state(self):
        for p in ("/", "/app.js", "/app.css"):
            st, body = self._get(p)
            self.assertEqual(st, 200)
            self.assertTrue(body)
        st, raw = self._get("/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(len(json.loads(raw)["escalators"]), 5)

    def test_full_flow_over_http(self):
        st, _ = self._call("/api/check", {"esc_id": "EA", "user": INSP1,
                                          "item": "steps", "result": "pass"})
        self.assertEqual(st, 200)
        for item in ("comb", "handrail", "lighting"):
            self._call("/api/check", {"esc_id": "EA", "user": INSP1,
                                      "item": item, "result": "pass"})
        st, r = self._call("/api/estop-test", {"esc_id": "EA", "user": INSP1})
        self.assertTrue(r["ok"])
        st, raw = self._get("/api/state")
        v = next(e for e in json.loads(raw)["escalators"] if e["id"] == "EA")["version"]
        st, r = self._call("/api/start", {"esc_id": "EA", "user": OP1, "version": v})
        self.assertEqual(st, 200)
        # 越权
        st, r = self._call("/api/detour/publish",
                           {"esc_id": "EA", "user": OP1, "message": "绕行"})
        self.assertEqual(st, 403)
        self.assertEqual(r["code"], "FORBIDDEN")
        # 跳停注入
        st, r = self._call("/api/sim/trip", {"esc_id": "EA", "code": "CHAIN_FAULT"})
        self.assertEqual(st, 200)
        st, data = self._get("/api/tickets?open_only=1")
        self.assertTrue(json.loads(data)["tickets"])

    def test_photo_upload_and_fetch(self):
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f7f0000000049454e44ae426082")
        st, r = self._call("/api/photo", raw=png,
                           headers={"X-Filename": "fault_demo.png",
                                    "Content-Type": "application/octet-stream"})
        self.assertEqual(st, 200)
        st, body = self._get("/photos/" + r["filename"])
        self.assertEqual(st, 200)
        self.assertEqual(body, png)


if __name__ == "__main__":
    unittest.main(verbosity=2)

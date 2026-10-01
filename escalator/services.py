"""核心领域服务：开梯、封控、维修、复检、复开的全部业务规则。

规则要点：
- 开梯前五项班前检查必须全部合格（通过/不适用，不得缺项、不得有失败），且当日急停测试合格；
- 启动互斥：两名值班员不能同时启动同一扶梯；启动命令须携带当前版本号；
- 方向必须与当前设定一致，否则拒绝（方向设定错误独立处理）；
- 故障/客流触发停梯封控，围挡限时搭设；反复跳停升级派单；
- 维修 SLA 超时独立告警，可延期并留痕；
- 复检由巡检员提交，须由他人审批，禁止自审；审批退回工单返修；
- 围挡未撤、复检未批、客流未恢复均阻断复开，提前复开独立记录；
- 客服仅能发布/撤销绕行信息。
"""
import json
import threading
from datetime import datetime, timedelta

from .clock import ts, parse
from .seed import PERSON_BY_ID, ROLE_RIGHTS

CHECK_ITEMS = [
    ("steps",    "梯级"),
    ("comb",     "梳齿板"),
    ("handrail", "扶手带"),
    ("lighting", "照明"),
    ("estop",    "急停装置"),
]
CHECK_ITEM_NAMES = dict(CHECK_ITEMS)

# 状态
ST_STOPPED, ST_RUNNING, ST_LOCKED = "stopped", "running", "locked"
# 封控原因
LR_FAULT, LR_CROWD, LR_ESTOP = "fault", "crowd", "estop"

REPAIR_SLA_MIN = 60          # 维修到场/处置时限
BARRIER_GRACE_MIN = 5        # 围挡搭设宽限
REPEAT_WINDOW_MIN = 24 * 60  # 反复跳停统计窗口
REPEAT_THRESHOLD = 2         # 窗口内跳停次数阈值（达到即升级）


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class EscalatorService:
    def __init__(self, db, clock, cabinet, crowd, photo_dir="data/photos"):
        self.db = db
        self.clock = clock
        self.cabinet = cabinet
        self.crowd = crowd
        self.photo_dir = photo_dir
        self._lock = threading.RLock()
        self._start_locks: dict[str, threading.Lock] = {}
        self._starting: set[str] = set()

    # ------------------------------------------------------------------ 基础
    def seed_if_empty(self):
        from .seed import ESCALATORS
        with self._lock:
            n = self.db.execute("SELECT COUNT(*) c FROM escalators").fetchone()["c"]
            if n:
                return
            now = ts(self.clock.now())
            for e in ESCALATORS:
                self.db.execute(
                    """INSERT INTO escalators(id,name,low_floor,high_floor,planned_direction,
                       schedule_start,schedule_stop,version,state,created_at)
                       VALUES(?,?,?,?,?,?,?,0,'stopped',?)""",
                    (e["id"], e["name"], e["low_floor"], e["high_floor"],
                     e["planned_direction"], e["schedule_start"], e["schedule_stop"], now))
            self.db.commit()

    def _person(self, user_id: str) -> dict:
        p = PERSON_BY_ID.get(user_id)
        if not p:
            raise DomainError("UNKNOWN_USER", f"未知人员：{user_id}", 400)
        return p

    def require_right(self, user_id: str, right: str) -> dict:
        p = self._person(user_id)
        if right not in ROLE_RIGHTS[p["role"]]["rights"]:
            raise DomainError(
                "FORBIDDEN",
                f"{p['role_name']}{p['name']}无权执行该操作（需要：{right}）", 403)
        return p

    def event(self, esc_id, etype, detail=None, actor=None):
        self.db.execute(
            "INSERT INTO events(ts,esc_id,actor,type,detail) VALUES(?,?,?,?,?)",
            (ts(self.clock.now()), esc_id, actor, etype,
             detail if isinstance(detail, str | None) else json.dumps(detail, ensure_ascii=False)))
        self.db.commit()

    def _esc(self, esc_id: str):
        row = self.db.execute("SELECT * FROM escalators WHERE id=?", (esc_id,)).fetchone()
        if not row:
            raise DomainError("UNKNOWN_ESC", f"未知扶梯：{esc_id}", 404)
        return row

    def _bump(self, esc_id: str):
        self.db.execute("UPDATE escalators SET version=version+1 WHERE id=?", (esc_id,))

    def _today(self) -> str:
        return self.clock.now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------ 班前检查
    def submit_check(self, esc_id, user_id, item, result, note=""):
        p = self.require_right(user_id, "check")
        if item not in CHECK_ITEM_NAMES:
            raise DomainError("BAD_ITEM", f"未知检查项：{item}", 400)
        if result not in ("pass", "fail", "na"):
            raise DomainError("BAD_RESULT", "检查结果只能是 pass/fail/na", 400)
        with self._lock:
            self._esc(esc_id)
            now = ts(self.clock.now())
            self.db.execute(
                """INSERT INTO checks(esc_id,date,item,result,inspector,note,ts)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(esc_id,date,item) DO UPDATE SET
                     result=excluded.result, inspector=excluded.inspector,
                     note=excluded.note, ts=excluded.ts""",
                (esc_id, self._today(), item, result, user_id, note, now))
            label = {"pass": "合格", "fail": "不合格", "na": "不适用"}[result]
            self.event(esc_id, "CHECK",
                       f"{CHECK_ITEM_NAMES[item]}：{label}" + (f"（{note}）" if note else ""),
                       actor=user_id)
            self.db.commit()
            return self.check_status(esc_id)

    def estop_test(self, esc_id, user_id):
        """急停装置功能测试；失败即判该梯故障并封控报修（独立处理）。"""
        p = self.require_right(user_id, "estop")
        with self._lock:
            self._esc(esc_id)
            ok = self.cabinet.estop_test(esc_id)
            now = ts(self.clock.now())
            self.db.execute(
                """INSERT INTO checks(esc_id,date,item,result,inspector,note,ts)
                   VALUES(?,?, 'estop', ?, ?, ?, ?)
                   ON CONFLICT(esc_id,date,item) DO UPDATE SET
                     result=excluded.result, inspector=excluded.inspector,
                     note=excluded.note, ts=excluded.ts""",
                (esc_id, self._today(), "pass" if ok else "fail", user_id,
                 "急停回路功能测试", now))
            if ok:
                self.event(esc_id, "ESTOP_TEST_PASS", "急停功能测试合格", actor=user_id)
                self.db.commit()
                return {"ok": True}
            self.event(esc_id, "ESTOP_TEST_FAIL", "急停功能测试失败，立即停梯封控报修",
                       actor=user_id)
            self.db.commit()
            self._lock_down(esc_id, LR_ESTOP, "ESTOP_FAULT", "急停装置测试失败",
                            reporter=user_id, detail="控制柜急停回路自检不合格")
            return {"ok": False}

    def check_status(self, esc_id: str) -> dict:
        """当日检查完成情况。"""
        self._esc(esc_id)
        day = self._today()
        rows = {r["item"]: r for r in self.db.execute(
            "SELECT * FROM checks WHERE esc_id=? AND date=?", (esc_id, day))}
        items, failed, missing = [], [], []
        for key, name in CHECK_ITEMS:
            r = rows.get(key)
            if r is None:
                missing.append(key)
                items.append({"key": key, "name": name, "result": None})
            else:
                items.append({"key": key, "name": name, "result": r["result"],
                              "inspector": r["inspector"], "note": r["note"], "ts": r["ts"]})
                if r["result"] == "fail":
                    failed.append(key)
        estop_ok = rows.get("estop") is not None and rows["estop"]["result"] == "pass"
        ready = not missing and not failed and estop_ok
        return {"date": day, "items": items, "missing": missing,
                "failed": failed, "estop_ok": estop_ok, "ready": ready}

    # ------------------------------------------------------------ 方向设定
    def set_direction(self, esc_id, user_id, direction):
        self.require_right(user_id, "direction")
        if direction not in ("up", "down"):
            raise DomainError("BAD_DIRECTION", "方向只能是 up/down", 400)
        with self._lock:
            esc = self._esc(esc_id)
            if esc["state"] == ST_RUNNING:
                raise DomainError("RUNNING", "运行中不可改向，请先停梯")
            if esc["planned_direction"] == direction:
                raise DomainError("SAME_DIRECTION", "新方向与当前设定一致，无需变更")
            self.db.execute(
                "UPDATE escalators SET planned_direction=?, version=version+1 WHERE id=?",
                (direction, esc_id))
            self.event(esc_id, "DIRECTION_SET",
                       f"运行方向设定改为{'上行 ↑' if direction == 'up' else '下行 ↓'}",
                       actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 启动/停止
    def start(self, esc_id, user_id, direction=None, version=None):
        """启动扶梯：互斥 + 版本 + 方向 + 班前检查 + 急停测试，任一不满足即拒绝。"""
        p = self.require_right(user_id, "start")
        # 快速互斥探测：不被另一线程的控制柜长调用堵在锁队列上
        with self._lock:
            if esc_id in self._starting:
                raise DomainError("START_IN_PROGRESS", "已有启动命令在执行中，互斥拒绝")
        sl = self._start_locks.setdefault(esc_id, threading.Lock())
        with sl:
            if esc_id in self._starting:
                raise DomainError("START_IN_PROGRESS", "已有启动命令在执行中，互斥拒绝")
            self._starting.add(esc_id)
            try:
                with self._lock:
                    esc = self._esc(esc_id)
                    if version is not None and int(version) != esc["version"]:
                        raise DomainError("STALE_VERSION",
                            f"版本过期：提交 v{version}，当前 v{esc['version']}，请刷新后重试", 409)
                    if esc["state"] == ST_RUNNING:
                        raise DomainError("ALREADY_RUNNING", "设备已在运行")
                    if esc["state"] == ST_LOCKED:
                        raise DomainError("LOCKED", "设备处于封控状态，须走复开流程")
                    want = direction or esc["planned_direction"]
                    if want != esc["planned_direction"]:
                        raise DomainError("WRONG_DIRECTION",
                            f"方向设定错误：命令为{'上行' if want == 'up' else '下行'}，"
                            f"当前设定为{'上行' if esc['planned_direction'] == 'up' else '下行'}")
                    chk = self.check_status(esc_id)
                    if chk["missing"]:
                        names = "、".join(CHECK_ITEM_NAMES[k] for k in chk["missing"])
                        raise DomainError("CHECK_INCOMPLETE", f"班前检查漏项：{names}")
                    if chk["failed"]:
                        names = "、".join(CHECK_ITEM_NAMES[k] for k in chk["failed"])
                        raise DomainError("CHECK_FAILED", f"班前检查不合格项：{names}")
                    if not chk["estop_ok"]:
                        raise DomainError("ESTOP_NOT_TESTED", "当日急停功能测试未通过，禁止启动")
                # 控制柜调用放在主锁外，制造真实的互斥窗口
                ok, err = self.cabinet.start(esc_id, esc["planned_direction"] if
                                             direction is None else direction)
                if not ok:
                    raise DomainError("CABINET_REJECT", err)
                with self._lock:
                    now = ts(self.clock.now())
                    self.db.execute(
                        """UPDATE escalators SET state='running', direction=?,
                           run_started_at=?, version=version+1, stop_count=0 WHERE id=?""",
                        (esc["planned_direction"] if direction is None else direction,
                         now, esc_id))
                    self.event(esc_id, "START",
                               f"启动成功（{'上行 ↑' if (direction or esc['planned_direction']) == 'up' else '下行 ↓'}，开梯人 {p['name']}）",
                               actor=user_id)
                    self.db.commit()
            finally:
                self._starting.discard(esc_id)

    def stop(self, esc_id, user_id, reason="手动停梯"):
        self.require_right(user_id, "stop")
        with self._lock:
            esc = self._esc(esc_id)
            if esc["state"] != ST_RUNNING:
                raise DomainError("NOT_RUNNING", "设备未在运行")
            self.cabinet.stop(esc_id)
            self.db.execute(
                """UPDATE escalators SET state='stopped', direction=NULL,
                   run_started_at=NULL, version=version+1 WHERE id=?""", (esc_id,))
            self.event(esc_id, "STOP", reason, actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 封控
    def _active_lock_ticket(self, esc_id: str):
        return self.db.execute(
            "SELECT * FROM tickets WHERE esc_id=? AND status!='closed' "
            "ORDER BY id DESC LIMIT 1", (esc_id,)).fetchone()

    def _count_recent_trips(self, esc_id: str) -> int:
        since = ts(self.clock.now() - timedelta(minutes=REPEAT_WINDOW_MIN))
        return self.db.execute(
            "SELECT COUNT(*) c FROM events WHERE esc_id=? AND type='TRIP' AND ts>=?",
            (esc_id, since)).fetchone()["c"]

    def _lock_down(self, esc_id, reason, fault_code, title, reporter=None, detail="",
                   trip_actor="控制柜"):
        """调用时已持主锁。停梯 + 封控 + 围挡要求 +（故障时）工单。"""
        esc = self._esc(esc_id)
        if esc["state"] == ST_LOCKED:
            raise DomainError("ALREADY_LOCKED", "设备已在封控中，无需重复封控")
        now = self.clock.now()
        was_running = esc["state"] == ST_RUNNING
        self.cabinet.stop(esc_id)
        ticket_id = None
        if reason in (LR_FAULT, LR_ESTOP):
            due = now + timedelta(minutes=REPAIR_SLA_MIN)
            cur = self.db.execute(
                "SELECT * FROM tickets WHERE esc_id=? AND status!='closed' ORDER BY id DESC",
                (esc_id,)).fetchall()
            if cur:
                # 已有未结工单：沿用，只追加事件，不重复建工单
                ticket_id = cur[0]["id"]
                self.db.execute(
                    "UPDATE tickets SET status='reported', fault_code=?, title=? WHERE id=?",
                    (fault_code, title, ticket_id))
            else:
                cur2 = self.db.execute(
                    """INSERT INTO tickets(esc_id,status,fault_code,title,detail,reporter,
                       reported_at,due_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (esc_id, "reported", fault_code, title, detail,
                     reporter or "系统", ts(now), ts(due)))
                ticket_id = cur2.lastrowid
        self.db.execute(
            """UPDATE escalators SET state='locked', direction=NULL, run_started_at=NULL,
               lock_reason=?, locked_at=?, barrier_required=1, barrier_erected=0,
               barrier_removed=0, stop_count=stop_count+? , version=version+1 WHERE id=?""",
            (reason, ts(now), 1 if was_running else 0, esc_id))
        reason_txt = {LR_FAULT: "故障", LR_CROWD: "客流拥堵", LR_ESTOP: "急停测试失败"}[reason]
        self.event(esc_id, "LOCK", f"停梯封控：{reason_txt}（{title}）", actor=reporter)
        if reason in (LR_FAULT, LR_ESTOP):
            self.event(esc_id, "TRIP", fault_code, actor=trip_actor)
        self.event(esc_id, "BARRIER_REQUIRED",
                   f"请于 {BARRIER_GRACE_MIN} 分钟内完成围挡搭设", actor=reporter)
        self.event(esc_id, "DETOUR_SUGGESTED", "建议客服发布客流绕行提示", actor="系统")
        self.db.commit()
        if reason in (LR_FAULT, LR_ESTOP):
            trips = self._count_recent_trips(esc_id)  # 含刚刚写入的 TRIP 事件
            if trips >= REPEAT_THRESHOLD:
                self._escalate_repeat(esc_id, ticket_id, trips)
        return ticket_id

    def _escalate_repeat(self, esc_id, ticket_id, count):
        self.event(esc_id, "REPEAT_TRIP",
                   f"{REPEAT_WINDOW_MIN // 60} 小时内第 {count} 次跳停，升级处理：立即派单",
                   actor="系统")
        if ticket_id:
            self.db.execute(
                "UPDATE tickets SET status='dispatched', ack_at=?, repair_by='rep1' WHERE id=?",
                (ts(self.clock.now()), ticket_id))
            self.event(esc_id, "DISPATCH", "反复跳停升级，系统已直接派单给维修工孙志强",
                       actor="系统")
            self.db.commit()

    def trip(self, esc_id, code="TRIP", title=None):
        """控制柜上报跳停（外部事件）。"""
        with self._lock:
            self._esc(esc_id)
            tid = self._lock_down(
                esc_id, LR_FAULT, code, title or f"控制柜跳停（{code}）",
                reporter="控制柜", detail="模拟控制柜上报异常停梯")
            return tid

    def report_fault(self, esc_id, user_id, fault_code, title, detail="", photos=None):
        p = self.require_right(user_id, "report")
        photos = photos or []
        with self._lock:
            esc = self._esc(esc_id)
            if esc["state"] == ST_RUNNING:
                self.cabinet.stop(esc_id)
            tid = self._lock_down(esc_id, LR_FAULT, fault_code, title,
                                  reporter=user_id, detail=detail, trip_actor=user_id)
            if photos:
                self.db.execute("UPDATE tickets SET photos=? WHERE id=?",
                                (json.dumps(photos, ensure_ascii=False), tid))
                self.db.commit()
            return tid

    def crowd_event(self, esc_id, density: float):
        """客流超阈值：封控但不建工单。"""
        with self._lock:
            esc = self._esc(esc_id)
            if esc["state"] == ST_LOCKED and esc["lock_reason"] == LR_CROWD:
                return  # 同一波拥堵只触发一次
            if esc["state"] != ST_RUNNING:
                return
            self.cabinet.stop(esc_id)
            self._lock_down(esc_id, LR_CROWD, "CROWD",
                            f"出入口客流拥挤（{density:.0%}）", reporter="客流监测")

    # ------------------------------------------------------------ 围挡
    def erect_barrier(self, esc_id, user_id):
        self.require_right(user_id, "barrier")
        with self._lock:
            esc = self._esc(esc_id)
            if not esc["barrier_required"]:
                raise DomainError("NO_BARRIER_NEEDED", "当前不需要围挡")
            if esc["barrier_erected"]:
                raise DomainError("ALREADY_ERECTED", "围挡已搭设")
            self.db.execute("UPDATE escalators SET barrier_erected=1 WHERE id=?", (esc_id,))
            self.event(esc_id, "BARRIER_ERECTED", "围挡搭设完成，两端禁行", actor=user_id)
            self.db.commit()

    def remove_barrier(self, esc_id, user_id):
        self.require_right(user_id, "barrier")
        with self._lock:
            esc = self._esc(esc_id)
            if not esc["barrier_erected"]:
                raise DomainError("NOT_ERECTED", "围挡尚未搭设")
            if esc["barrier_removed"]:
                raise DomainError("ALREADY_REMOVED", "围挡已撤除")
            # 故障封控：复检未通过审批不得撤围挡
            if esc["lock_reason"] in (LR_FAULT, LR_ESTOP):
                t = self._active_lock_ticket(esc_id)
                if t is None or t["status"] != "approved":
                    raise DomainError("REPAIR_NOT_APPROVED",
                                      "维修复检尚未审批通过，禁止撤除围挡")
            else:
                if self.crowd.density(esc_id) > self.crowd.CROWD_RESUME:
                    raise DomainError("CROWD_HIGH", "客流仍未恢复，禁止撤除围挡")
            self.db.execute("UPDATE escalators SET barrier_removed=1 WHERE id=?", (esc_id,))
            self.event(esc_id, "BARRIER_REMOVED", "围挡撤除，现场清理完毕", actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 维修工单
    def dispatch_ticket(self, ticket_id, user_id, repair_by=None):
        self.require_right(user_id, "dispatch")
        with self._lock:
            t = self._ticket(ticket_id)
            if t["status"] != "reported":
                raise DomainError("BAD_STATUS", f"工单当前状态 {t['status']}，不可派单")
            repair_by = repair_by or user_id
            self._person(repair_by)
            self.db.execute(
                "UPDATE tickets SET status='dispatched', ack_at=?, repair_by=? WHERE id=?",
                (ts(self.clock.now()), repair_by, ticket_id))
            self.event(t["esc_id"], "DISPATCH",
                       f"工单 #{ticket_id} 已派单，维修人员 {PERSON_BY_ID[repair_by]['name']} 接单",
                       actor=user_id)
            self.db.commit()

    def arrive_ticket(self, ticket_id, user_id):
        self.require_right(user_id, "arrive")
        with self._lock:
            t = self._ticket(ticket_id)
            if t["status"] != "dispatched":
                raise DomainError("BAD_STATUS", f"工单当前状态 {t['status']}，尚未派单")
            self.db.execute(
                "UPDATE tickets SET status='arrived', arrived_at=? WHERE id=?",
                (ts(self.clock.now()), ticket_id))
            self.event(t["esc_id"], "ARRIVE",
                       f"维修工到场（工单 #{ticket_id}），用时 "
                       f"{self._minutes_between(t['reported_at'])} 分钟", actor=user_id)
            self.db.commit()

    def repair_done(self, ticket_id, user_id, note=""):
        self.require_right(user_id, "repair")
        with self._lock:
            t = self._ticket(ticket_id)
            if t["status"] not in ("arrived", "dispatched", "repairing", "rejected"):
                raise DomainError("BAD_STATUS",
                                  f"工单当前状态 {t['status']}，不可提交维修完成")
            # 维修完成：现场复位控制柜跳停回路
            with self.cabinet._lock:
                self.cabinet._state[t["esc_id"]] = "stopped"
            self.event(t["esc_id"], "CABINET_RESET", "维修后控制柜跳停状态已复位",
                       actor=user_id)
            overdue = self.clock.now() > parse(t["due_at"])
            self.db.execute(
                """UPDATE tickets SET status='awaiting_review', repaired_at=?,
                   repair_by=?, repair_note=?, overdue=? WHERE id=?""",
                (ts(self.clock.now()), user_id, note, 1 if overdue else 0, ticket_id))
            tag = "（已超 SLA）" if overdue else ""
            self.event(t["esc_id"], "REPAIR_DONE",
                       f"维修完成{tag}，等待复检（工单 #{ticket_id}）"
                       + (f"：{note}" if note else ""), actor=user_id)
            self.db.commit()

    def extend_ticket(self, ticket_id, user_id, minutes, reason=""):
        self.require_right(user_id, "extend")
        minutes = int(minutes)
        if minutes <= 0:
            raise DomainError("BAD_EXTENSION", "延时时长必须为正", 400)
        with self._lock:
            t = self._ticket(ticket_id)
            if t["status"] in ("awaiting_review", "approved", "closed"):
                raise DomainError("BAD_STATUS", "工单已进入复检/结案，不可延期")
            exts = json.loads(t["extensions"])
            exts.append({"by": user_id, "minutes": minutes,
                         "reason": reason, "at": ts(self.clock.now())})
            new_due = parse(t["due_at"]) + timedelta(minutes=minutes)
            self.db.execute("UPDATE tickets SET due_at=?, extensions=? WHERE id=?",
                            (ts(new_due), json.dumps(exts, ensure_ascii=False), ticket_id))
            self.event(t["esc_id"], "REPAIR_EXTEND",
                       f"工单 #{ticket_id} 延期 {minutes} 分钟，新时限 {ts(new_due)[11:]}"
                       + (f"（{reason}）" if reason else ""), actor=user_id)
            self.db.commit()

    def _ticket(self, ticket_id):
        r = self.db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not r:
            raise DomainError("UNKNOWN_TICKET", f"未知工单 #{ticket_id}", 404)
        return r

    def _minutes_between(self, older: str) -> int:
        return int((self.clock.now() - parse(older)).total_seconds() // 60)

    # ------------------------------------------------------------ 复检/审批
    def submit_recheck(self, ticket_id, user_id, results: dict, note=""):
        """巡检员提交复检；任一项失败即退回返修。"""
        self.require_right(user_id, "recheck")
        with self._lock:
            t = self._ticket(ticket_id)
            if t["status"] != "awaiting_review":
                raise DomainError("BAD_STATUS",
                                  f"工单当前状态 {t['status']}，尚未完成维修，不能复检")
            clean = {}
            for key, _ in CHECK_ITEMS:
                v = results.get(key)
                if v not in ("pass", "fail", "na"):
                    raise DomainError("RECHECK_INCOMPLETE",
                                      f"复检项 {CHECK_ITEM_NAMES[key]} 缺少 通过/失败/不适用 选择", 400)
                clean[key] = v
            if any(v == "fail" for v in clean.values()):
                fails = "、".join(CHECK_ITEM_NAMES[k] for k, v in clean.items() if v == "fail")
                self.event(t["esc_id"], "RECHECK_FAILED",
                           f"复检发现不合格项：{fails}（工单 #{ticket_id}），退回返修",
                           actor=user_id)
                self.db.execute("UPDATE tickets SET status='repairing' WHERE id=?", (ticket_id,))
                self.db.commit()
                raise DomainError("RECHECK_FAILED", f"复检不合格：{fails}，已退回维修工返修")
            cur = self.db.execute(
                """INSERT INTO rechecks(ticket_id,esc_id,inspector,results,ts,status,note)
                   VALUES(?,?,?,?,?, 'submitted',?)""",
                (ticket_id, t["esc_id"], user_id,
                 json.dumps(clean, ensure_ascii=False), ts(self.clock.now()), note))
            self.event(t["esc_id"], "RECHECK_SUBMIT",
                       f"复检提交，等待他人审批（工单 #{ticket_id}，复检人 "
                       f"{PERSON_BY_ID[user_id]['name']}）", actor=user_id)
            self.db.commit()
            return cur.lastrowid

    def review_recheck(self, recheck_id, user_id, approve: bool, note=""):
        self.require_right(user_id, "review")
        with self._lock:
            r = self.db.execute("SELECT * FROM rechecks WHERE id=?", (recheck_id,)).fetchone()
            if not r:
                raise DomainError("UNKNOWN_RECHECK", f"未知复检单 #{recheck_id}", 404)
            if r["status"] != "submitted":
                raise DomainError("BAD_STATUS", f"复检单状态为 {r['status']}，无需再审")
            if r["inspector"] == user_id:
                raise DomainError("SELF_REVIEW",
                                  "巡检员不能批准自己提交的复检，请由其他巡检员或主管审批", 403)
            if approve:
                self.db.execute(
                    "UPDATE rechecks SET status='approved', approver=?, approve_ts=?, note=? "
                    "WHERE id=?",
                    (user_id, ts(self.clock.now()), note, recheck_id))
                self.db.execute("UPDATE tickets SET status='approved' WHERE id=?",
                                (r["ticket_id"],))
                self.event(r["esc_id"], "RECHECK_APPROVED",
                           f"复检审批通过（工单 #{r['ticket_id']}，审批人 "
                           f"{self._person(user_id)['name']}），可撤除围挡并复开", actor=user_id)
            else:
                self.db.execute(
                    "UPDATE rechecks SET status='rejected', approver=?, approve_ts=?, note=? "
                    "WHERE id=?",
                    (user_id, ts(self.clock.now()), note, recheck_id))
                self.db.execute("UPDATE tickets SET status='repairing' WHERE id=?",
                                (r["ticket_id"],))
                self.event(r["esc_id"], "RECHECK_REJECTED",
                           f"复检审批退回（工单 #{r['ticket_id']}）：{note or '现场仍有问题'}，"
                           "退回维修工返修", actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 绕行
    def publish_detour(self, esc_id, user_id, message, channel="广播+指引牌"):
        self.require_right(user_id, "detour")
        if not message.strip():
            raise DomainError("EMPTY_MESSAGE", "绕行提示内容不能为空", 400)
        with self._lock:
            self._esc(esc_id)
            cur = self.db.execute(
                "INSERT INTO detours(esc_id,message,channel,published_by,ts,active) "
                "VALUES(?,?,?,?,?,1)",
                (esc_id, message.strip(), channel, user_id, ts(self.clock.now())))
            self.event(esc_id, "DETOUR_PUBLISH",
                       f"绕行提示已发布（{channel}）：{message.strip()}", actor=user_id)
            self.db.commit()
            return cur.lastrowid

    def cancel_detour(self, detour_id, user_id):
        self.require_right(user_id, "detour")
        with self._lock:
            r = self.db.execute("SELECT * FROM detours WHERE id=?", (detour_id,)).fetchone()
            if not r:
                raise DomainError("UNKNOWN_DETOUR", "未知绕行信息", 404)
            self.db.execute("UPDATE detours SET active=0 WHERE id=?", (detour_id,))
            self.event(r["esc_id"], "DETOUR_CANCEL", "绕行提示已撤销", actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 复开
    def reopen_eligible(self, esc_id: str) -> dict:
        """复开条件检查（不产生事件）。"""
        esc = self._esc(esc_id)
        checks = {"locked": esc["state"] == ST_LOCKED}
        ticket = self.db.execute(
            "SELECT * FROM tickets WHERE esc_id=? AND status!='closed' ORDER BY id DESC LIMIT 1",
            (esc_id,)).fetchone()
        if esc["lock_reason"] in (LR_FAULT, LR_ESTOP):
            checks["repair_approved"] = ticket is not None and ticket["status"] == "approved"
        else:
            checks["repair_approved"] = True  # 客流封控无维修环节
        checks["barrier_removed"] = bool(esc["barrier_removed"])
        density = self.crowd.density(esc_id)
        if esc["lock_reason"] == LR_CROWD:
            checks["crowd_resumed"] = density <= self.crowd.CROWD_RESUME
        else:
            checks["crowd_resumed"] = True
        checks["detour_cleared"] = self.db.execute(
            "SELECT COUNT(*) c FROM detours WHERE esc_id=? AND active=1",
            (esc_id,)).fetchone()["c"] == 0
        checks["eligible"] = all(checks.values())
        checks["density"] = density
        return checks

    def reopen(self, esc_id, user_id, version=None):
        self.require_right(user_id, "reopen")
        with self._lock:
            esc = self._esc(esc_id)
            if version is not None and int(version) != esc["version"]:
                raise DomainError("STALE_VERSION",
                    f"版本过期：提交 v{version}，当前 v{esc['version']}，请刷新后重试")
            if esc["state"] != ST_LOCKED:
                raise DomainError("NOT_LOCKED", "设备未处于封控状态，无需复开")
            elig = self.reopen_eligible(esc_id)
            if not elig["eligible"]:
                missing = []
                if not elig["repair_approved"]:
                    missing.append("维修复检未审批通过")
                if not elig["barrier_removed"]:
                    missing.append("围挡未撤除")
                if not elig["crowd_resumed"]:
                    missing.append("客流尚未恢复")
                if not elig["detour_cleared"]:
                    missing.append("绕行提示未撤销")
                self.event(esc_id, "EARLY_REOPEN_BLOCKED",
                           "提前复开被阻断：" + "、".join(missing), actor=user_id)
                self.db.commit()
                raise DomainError("EARLY_REOPEN",
                                  "复开条件不满足：" + "、".join(missing))
            ok, err = self.cabinet.start(esc_id, esc["planned_direction"])
            if not ok:
                raise DomainError("CABINET_REJECT", err or "控制柜拒绝启动，需现场复位")
            self.db.execute(
                """UPDATE escalators SET state='running', direction=planned_direction,
                   lock_reason=NULL, locked_at=NULL, run_started_at=?,
                   barrier_required=0, barrier_erected=0, barrier_removed=0,
                   crowd_clear=1, version=version+1 WHERE id=?""",
                (ts(self.clock.now()), esc_id))
            self.db.execute(
                "UPDATE tickets SET status='closed' WHERE esc_id=? AND status='approved'",
                (esc_id,))
            self.event(esc_id, "REOPEN",
                       f"封控解除，恢复运行（{'上行 ↑' if esc['planned_direction'] == 'up' else '下行 ↓'}，"
                       f"复开确认人 {self._person(user_id)['name']}）", actor=user_id)
            self.db.commit()

    def cabinet_reset(self, esc_id, user_id):
        """现场复位控制柜跳停状态（维修完成后由值班员执行）。"""
        self.require_right(user_id, "reopen")
        with self._lock:
            self._esc(esc_id)
            with self.cabinet._lock:
                self.cabinet._state[esc_id] = "stopped"
            self.event(esc_id, "CABINET_RESET", "控制柜跳停状态已现场复位", actor=user_id)
            self.db.commit()

    # ------------------------------------------------------------ 调度器钩子
    def poll_trips(self):
        for esc in self.db.execute("SELECT id FROM escalators").fetchall():
            for ev in self.cabinet.drain_events(esc["id"]):
                if ev["kind"] == "TRIP":
                    self.trip(esc["id"], ev.get("code", "TRIP"))

    def poll_crowd(self):
        for esc in self.db.execute("SELECT id FROM escalators").fetchall():
            d = self.crowd.density(esc["id"])
            row = self.db.execute(
                "SELECT state,lock_reason,crowd_clear FROM escalators WHERE id=?",
                (esc["id"],)).fetchone()
            if d >= self.crowd.CROWD_HIGH:
                self.crowd_event(esc["id"], d)
            elif d <= self.crowd.CROWD_RESUME and row["state"] == ST_LOCKED \
                    and row["lock_reason"] == LR_CROWD and not row["crowd_clear"]:
                with self._lock:
                    self.db.execute(
                        "UPDATE escalators SET crowd_clear=1 WHERE id=?", (esc["id"],))
                    self.event(esc["id"], "CROWD_RESUMED",
                               f"客流已回落（{d:.0%}），具备撤围挡与复开条件", actor="客流监测")
                    self.db.commit()

    def run_schedule(self):
        """按当前时刻执行计划启停（每分钟由调度器调用）。"""
        now = self.clock.now()
        hm = now.strftime("%H:%M")
        for esc in self.db.execute("SELECT * FROM escalators").fetchall():
            if hm == esc["schedule_start"] and esc["state"] == ST_STOPPED:
                self.event(esc["id"], "SCHEDULED_START_DUE",
                           "到达计划开梯时间，请完成班前检查后启动", actor="计划任务")
            if hm == esc["schedule_stop"] and esc["state"] == ST_RUNNING:
                self.cabinet.stop(esc["id"])
                self.db.execute(
                    "UPDATE escalators SET state='stopped',direction=NULL,"
                    "run_started_at=NULL,version=version+1 WHERE id=?", (esc["id"],))
                self.event(esc["id"], "SCHEDULED_STOP", "到达计划停梯时间，自动停梯",
                           actor="计划任务")
                self.db.commit()

    def check_timeouts(self):
        """维修 SLA 超时、围挡搭设超时。"""
        now = self.clock.now()
        for t in self.db.execute(
                "SELECT * FROM tickets WHERE status NOT IN"
                " ('awaiting_review','approved','closed')").fetchall():
            if now > parse(t["due_at"]) and not t["overdue"]:
                self.db.execute("UPDATE tickets SET overdue=1 WHERE id=?", (t["id"],))
                self.event(t["esc_id"], "REPAIR_OVERDUE",
                           f"工单 #{t['id']} 已超维修时限（应于 {t['due_at'][11:]} 前完成）",
                           actor="系统")
                self.db.commit()
        for esc in self.db.execute("SELECT * FROM escalators").fetchall():
            if esc["barrier_required"] and not esc["barrier_erected"] and esc["locked_at"]:
                grace = parse(esc["locked_at"]) + timedelta(minutes=BARRIER_GRACE_MIN)
                if now > grace:
                    exists = self.db.execute(
                        "SELECT 1 FROM events WHERE esc_id=? AND type='BARRIER_OVERDUE' "
                        "AND ts>=?", (esc["id"], esc["locked_at"])).fetchone()
                    if not exists:
                        self.event(esc["id"], "BARRIER_OVERDUE",
                                   f"封控已超过 {BARRIER_GRACE_MIN} 分钟仍未搭设围挡，立即处置",
                                   actor="系统")

    # ------------------------------------------------------------ 查询
    def list_escalators(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM escalators ORDER BY id").fetchall():
            e = dict(r)
            if e["state"] == ST_LOCKED and e["locked_at"]:
                e["locked_seconds"] = int((self.clock.now() - parse(e["locked_at"])).total_seconds())
            else:
                e["locked_seconds"] = None
            e["density"] = round(self.crowd.density(e["id"]), 2)
            e["cabinet_state"] = self.cabinet.get_state(e["id"])
            out.append(e)
        return out

    def list_tickets(self, esc_id=None, open_only=False):
        sql = "SELECT * FROM tickets"
        where, args = [], []
        if esc_id:
            where.append("esc_id=?"); args.append(esc_id)
        if open_only:
            where.append("status!='closed'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        out = []
        for r in self.db.execute(sql, args).fetchall():
            t = dict(r)
            t["photos"] = json.loads(t["photos"])
            t["extensions"] = json.loads(t["extensions"])
            out.append(t)
        return out

    def list_rechecks(self, ticket_id=None):
        sql = "SELECT * FROM rechecks"
        args = ()
        if ticket_id:
            sql += " WHERE ticket_id=?"; args = (ticket_id,)
        sql += " ORDER BY id DESC"
        out = []
        for r in self.db.execute(sql, args).fetchall():
            d = dict(r)
            d["results"] = json.loads(d["results"])
            out.append(d)
        return out

    def list_detours(self, active_only=False):
        sql = "SELECT * FROM detours"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self.db.execute(sql).fetchall()]

    def list_events(self, esc_id=None, etype=None, since=None, limit=200):
        sql = "SELECT * FROM events WHERE 1=1"
        args = []
        if esc_id:
            sql += " AND esc_id=?"; args.append(esc_id)
        if etype:
            sql += " AND type=?"; args.append(etype)
        if since:
            sql += " AND ts>=?"; args.append(since)
        sql += " ORDER BY seq DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def ticket_detail(self, ticket_id):
        t = self._ticket(ticket_id)
        d = dict(t)
        d["photos"] = json.loads(d["photos"])
        d["extensions"] = json.loads(d["extensions"])
        d["rechecks"] = self.list_rechecks(ticket_id)
        return d

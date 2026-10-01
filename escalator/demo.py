"""一条本地命令的样例演示：正常开梯 / 控制柜跳停 / 复检退回。

运行：python3 -m escalator.demo
使用 FakeClock 推进时间，SQLite 与照片目录落在临时目录，不影响线上数据。
"""
import os
import tempfile

from .clock import FakeClock
from .db import connect, init
from .adapters import CabinetAdapter, CrowdAdapter
from .scheduler import Scheduler
from .services import (EscalatorService, DomainError, CHECK_ITEMS, CHECK_ITEM_NAMES)

OP1, OP2 = "op1", "op2"
INSP1, INSP2 = "insp1", "insp2"
REP1, SUP, SVC1 = "rep1", "sup1", "svc1"


class DemoWorld:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="esc-demo-")
        self.db = connect(os.path.join(self.tmp, "demo.db"))
        init(self.db)
        self.clock = FakeClock()
        self.cab = CabinetAdapter()
        self.crowd = CrowdAdapter()
        self.svc = EscalatorService(self.db, self.clock, self.cab, self.crowd,
                                    os.path.join(self.tmp, "photos"))
        self.svc.seed_if_empty()
        self.sched = Scheduler(self.svc, interval=999)

    def tick(self, **kw):
        self.clock.advance(**kw)
        self.sched.tick()


def banner(n, title):
    print("\n" + "=" * 78)
    print(f" 样例{n}：{title}")
    print("=" * 78)


def show(w, esc_id, limit=40, title=None):
    if title:
        print(f"\n── {title}")
    rows = w.svc.list_events(esc_id=esc_id, limit=limit)
    rows.reverse()
    for e in rows:
        actor = f"[{e['actor']}] " if e["actor"] else ""
        print(f" {e['ts'][11:]}  {e['type']:<24} {actor}{e['detail'] or ''}")


def expect_error(fn, tag):
    try:
        fn()
        print(f"   !! 预期应被拦截但未拦截：{tag}")
    except DomainError as e:
        print(f"   >> 已拦截[{e.code}]：{e.message}")


def pass_all_checks(w, esc_id, inspector=INSP1):
    for key, _ in CHECK_ITEMS[:-1]:
        w.svc.submit_check(esc_id, inspector, key, "pass")
    assert w.svc.estop_test(esc_id, inspector)["ok"]


# ---------------------------------------------------------------- 样例 1
def demo_normal_start(w):
    banner(1, "正常开梯（班前检查 → 急停测试 → 按版本启动 → 计划停梯）")
    print(" 设备：EA 东翼 1 号扶梯（F1→F2，计划上行）")

    print("\n[1] 巡检员逐项班前检查 + 急停功能测试")
    pass_all_checks(w, "EA")

    print("[2] 方向设定错误的启动命令被拒绝（命令下行，设定上行）")
    expect_error(lambda: w.svc.start("EA", OP1, direction="down", version=0),
                 "方向错误")

    print("[3] 版本过期的启动命令被拒绝")
    # 主管改方向再改回，制造版本推进
    w.svc.set_direction("EA", SUP, "down")
    w.svc.set_direction("EA", SUP, "up")
    expect_error(lambda: w.svc.start("EA", OP1, version=0), "旧版本")

    print("[4] 值班员王建国按当前版本启动成功")
    v = w.svc.list_escalators()[0]["version"]
    w.svc.start("EA", OP1, version=v)
    ea = [e for e in w.svc.list_escalators() if e["id"] == "EA"][0]
    print(f"   >> 设备状态={ea['state']}，方向={ea['direction']}，版本=v{ea['version']}")

    print("[5] 另一名值班员重复启动被拒绝（启动互斥）")
    expect_error(lambda: w.svc.start("EA", OP2, version=ea["version"]), "重复启动")

    print("[6] 推进到 22:00 计划停梯时刻")
    w.clock.set(w.clock.now().replace(hour=22, minute=0, second=0))
    w.sched.tick()
    print("   >> 计划任务自动停梯")
    show(w, "EA", limit=30)


# ---------------------------------------------------------------- 样例 2
def demo_trip(w):
    banner(2, "控制柜跳停封控 → 围挡 → 派单到场 → 复检职责分离 → 复开 → 反复跳停升级")
    print(" 设备：EC 中庭 1 号扶梯")
    pass_all_checks(w, "EC")
    w.svc.start("EC", OP1)

    print("\n[1] 客服在设备未封控时尝试启动（越权，被拒绝）")
    expect_error(lambda: w.svc.start("EC", SVC1), "越权")

    print("[2] 控制柜上报链条故障跳停（模拟适配器注入）")
    w.cab.trip("EC", "CHAIN_FAULT")
    w.sched.tick()
    print("   >> 自动停梯封控、建工单、要求 5 分钟内搭设围挡")

    print("[3] 超过 5 分钟仍未搭设围挡（围挡缺失独立告警）")
    w.tick(minutes=6)

    print("[4] 值班员搭设围挡；客服发布绕行提示")
    w.svc.erect_barrier("EC", OP1)
    w.svc.publish_detour("EC", SVC1, "中庭 1 号扶梯故障，请改乘东翼扶梯或直梯")

    print("[5] 主管派单、维修工到场、维修完成")
    t1 = w.svc.list_tickets("EC", open_only=True)[0]["id"]
    w.svc.dispatch_ticket(t1, SUP, repair_by=REP1)
    w.tick(minutes=20)
    w.svc.arrive_ticket(t1, REP1)
    w.tick(minutes=15)
    w.svc.repair_done(t1, REP1, note="更换磨损链节并润滑")

    print("[6] 巡检员张晨复检合格并提交")
    results = {k: "pass" for k, _ in CHECK_ITEMS}
    rid = w.svc.submit_recheck(t1, INSP1, results)

    print("[7] 张晨试图批准自己的复检（职责分离，被拒绝）")
    expect_error(lambda: w.svc.review_recheck(rid, INSP1, True), "自审")

    print("[8] 另一名巡检员赵雪审批通过")
    w.svc.review_recheck(rid, INSP2, True, note="现场复测合格")

    print("[9] 围挡未撤除时尝试复开（提前复开被阻断并留痕）")
    expect_error(lambda: w.svc.reopen("EC", OP1), "提前复开")

    print("[10] 客服撤销绕行、值班员撤围挡并确认复开")
    d = w.svc.list_detours(active_only=True)[0]["id"]
    w.svc.cancel_detour(d, SVC1)
    w.svc.remove_barrier("EC", OP1)
    ec = [e for e in w.svc.list_escalators() if e["id"] == "EC"][0]
    w.svc.reopen("EC", OP1, version=ec["version"])

    print("[11] 运行后再次跳停：24 小时内反复跳停，自动升级派单")
    w.tick(hours=1)
    w.cab.trip("EC", "CHAIN_FAULT")
    w.sched.tick()
    show(w, "EC", limit=60)


# ---------------------------------------------------------------- 样例 3
def demo_reject(w):
    banner(3, "急停测试失败封控 → 维修超 SLA → 复检退回返修 → 审批退回 → 再检通过")
    print(" 设备：ED 中庭 2 号扶梯")

    print("\n[1] 巡检员做前四项检查（合格），执行急停测试")
    for key, _ in CHECK_ITEMS[:-1]:
        w.svc.submit_check("ED", INSP2, key, "pass")
    print("   >> 模拟控制柜急停回路故障")
    w.cab.set_estop_faulty("ED", True)
    r = w.svc.estop_test("ED", INSP2)
    print(f"   >> 急停测试结果：{'合格' if r['ok'] else '失败 → 立即停梯封控报修'}")

    print("[2] 检查存在失败项时，启动被禁止")
    expect_error(lambda: w.svc.start("ED", OP1), "检查未通过")

    print("[3] 值班员搭设围挡；主管派单维修")
    w.svc.erect_barrier("ED", OP2)
    t1 = w.svc.list_tickets("ED", open_only=True)[0]["id"]
    w.svc.dispatch_ticket(t1, SUP, repair_by=REP1)

    print("[4] 维修超 60 分钟 SLA（系统独立告警维修延期），主管核准延期 30 分钟")
    w.tick(minutes=65)
    w.sched.tick()
    w.svc.extend_ticket(t1, SUP, 30, reason="急停开关配件调拨")

    print("[5] 维修工到场修复（控制柜恢复），提交维修完成")
    w.svc.arrive_ticket(t1, REP1)
    w.cab.set_estop_faulty("ED", False)
    w.svc.repair_done(t1, REP1, note="更换急停开关")

    print("[6] 巡检员复检发现梯级仍有异常：复检退回返修")
    bad = {k: "pass" for k, _ in CHECK_ITEMS}
    bad["steps"] = "fail"
    expect_error(lambda: w.svc.submit_recheck(t1, INSP1, bad), "复检失败退回")

    print("[7] 维修工二次处理完成；巡检员复检合格提交，主管审批却退回")
    w.svc.repair_done(t1, REP1, note="重新校正梯级间隙")
    results = {k: "pass" for k, _ in CHECK_ITEMS}
    rid = w.svc.submit_recheck(t1, INSP1, results)
    w.svc.review_recheck(rid, SUP, False, note="试运行有轻微异响，再排查")
    print("   >> 工单回到返修状态")

    print("[8] 维修工再次处理，复检（赵雪）+ 主管审批通过，完成撤围挡复开")
    w.svc.repair_done(t1, REP1, note="紧固导轨压块，异响消除")
    rid2 = w.svc.submit_recheck(t1, INSP2, results)
    w.svc.review_recheck(rid2, SUP, True)
    w.svc.remove_barrier("ED", OP2)
    ed = [e for e in w.svc.list_escalators() if e["id"] == "ED"][0]
    w.svc.reopen("ED", OP2, version=ed["version"])
    show(w, "ED", limit=60)

    print("\n" + "=" * 78)
    print(" 三个样例执行完毕。启动 Web 平台：python3 -m escalator")
    print("=" * 78)


def main():
    demo_normal_start(DemoWorld())
    demo_trip(DemoWorld())
    demo_reject(DemoWorld())


if __name__ == "__main__":
    main()

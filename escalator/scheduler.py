"""进程内任务调度：计划启停、维修超时、围挡宽限、控制柜跳停、客流监测。

后台守护线程每 interval 秒跑一轮；使用 FakeClock 时由测试/demo 手动调用 tick()。
"""
import threading
from datetime import datetime


class Scheduler:
    def __init__(self, svc, interval=2.0):
        self.svc = svc
        self.interval = interval
        self._t = None
        self._stop = threading.Event()
        self._last_minute = None

    def start(self):
        if self._t is not None:
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True, name="esc-scheduler")
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)
            self._t = None

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # 调度失败不能打死线程
                self.svc.event(None, "SCHEDULER_ERROR", str(e), actor="计划任务")

    def tick(self):
        """一轮巡检：外部事件 → 客流 → 超时 → 计划。"""
        self.svc.poll_trips()
        self.svc.poll_crowd()
        self.svc.check_timeouts()
        # 计划启停按分钟去重，避免同一分钟内重复触发
        minute = self.svc.clock.now().strftime("%Y-%m-%d %H:%M")
        if minute != self._last_minute:
            self._last_minute = minute
            self.svc.run_schedule()

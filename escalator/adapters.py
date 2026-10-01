"""模拟适配器：扶梯控制柜与客流计数器（线程安全，可脚本注入事件）。"""
import threading
from collections import defaultdict, deque
from datetime import datetime


class CabinetAdapter:
    """模拟扶梯控制柜：启停、方向、急停回路自检、跳停事件上报。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._state = defaultdict(lambda: "stopped")   # stopped / running / tripped
        self._direction = {}
        self._estop_faulty = set()                     # 急停回路故障的设备
        self._events: dict[str, deque] = defaultdict(deque)
        self.on_start = None                           # 测试钩子：启动时回调

    # ---- 平台调用 ----
    def start(self, esc_id: str, direction: str) -> tuple[bool, str | None]:
        with self._lock:
            if self.on_start is not None:
                self.on_start(esc_id)
            state = self._state[esc_id]
            if state == "running":
                return False, "控制柜：设备已在运行"
            if state == "tripped":
                return False, "控制柜：跳停未复位，拒绝启动"
            if direction not in ("up", "down"):
                return False, "控制柜：方向信号非法"
            self._state[esc_id] = "running"
            self._direction[esc_id] = direction
            return True, None

    def stop(self, esc_id: str) -> tuple[bool, str | None]:
        with self._lock:
            if self._state[esc_id] == "running":
                self._state[esc_id] = "stopped"
            return True, None

    def estop_test(self, esc_id: str) -> bool:
        """急停回路自检；故障设备返回 False。"""
        with self._lock:
            return esc_id not in self._estop_faulty

    def get_state(self, esc_id: str) -> str:
        with self._lock:
            return self._state[esc_id]

    def get_direction(self, esc_id: str):
        with self._lock:
            return self._direction.get(esc_id)

    # ---- 模拟外部事件 ----
    def trip(self, esc_id: str, code: str = "TRIP") -> None:
        with self._lock:
            self._state[esc_id] = "tripped"
            self._events[esc_id].append({"kind": "TRIP", "code": code})

    def set_estop_faulty(self, esc_id: str, faulty: bool) -> None:
        with self._lock:
            if faulty:
                self._estop_faulty.add(esc_id)
            else:
                self._estop_faulty.discard(esc_id)

    def drain_events(self, esc_id: str):
        with self._lock:
            out = list(self._events[esc_id])
            self._events[esc_id].clear()
            return out


class CrowdAdapter:
    """模拟出入口客流摄像头：返回 0~1 拥挤度。"""

    CROWD_HIGH = 0.85
    CROWD_RESUME = 0.60

    def __init__(self):
        self._lock = threading.RLock()
        self._density = defaultdict(lambda: 0.35)

    def density(self, esc_id: str) -> float:
        with self._lock:
            return self._density[esc_id]

    def set_density(self, esc_id: str, value: float) -> None:
        with self._lock:
            self._density[esc_id] = max(0.0, min(1.0, float(value)))

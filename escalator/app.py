"""HTTP 服务：JSON API + 静态前端 + 故障照片存取（仅标准库）。"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import connect, init
from .clock import Clock
from .adapters import CabinetAdapter, CrowdAdapter
from .scheduler import Scheduler
from .seed import PEOPLE, FLOORS
from .services import EscalatorService, DomainError, CHECK_ITEMS

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


def create_app(db_path="data/escalator.db", photo_dir="data/photos",
               clock=None, start_scheduler=True):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    os.makedirs(photo_dir, exist_ok=True)
    db = connect(db_path)
    init(db)
    cabinet = CabinetAdapter()
    crowd = CrowdAdapter()
    svc = EscalatorService(db, clock or Clock(), cabinet, crowd, photo_dir)
    svc.seed_if_empty()
    sched = Scheduler(svc) if start_scheduler else None
    if sched:
        sched.start()
    return {"svc": svc, "scheduler": sched, "cabinet": cabinet, "crowd": crowd,
            "db": db, "photo_dir": os.path.abspath(photo_dir)}


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "EscPlat/1.0"

    def log_message(self, fmt, *args):
        pass  # 安静模式；事件以平台内事件流为准

    # ---------------------------------------------------------- 响应工具
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, **kw):
        self._json({"ok": True, **kw})

    def _err(self, e: DomainError):
        self._json({"ok": False, "code": e.code, "error": e.message}, e.status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise DomainError("BAD_JSON", "请求体不是合法 JSON", 400)

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        svc = self.app["svc"]
        try:
            if path == "/api/state":
                escs = svc.list_escalators()
                for e in escs:
                    e["check"] = svc.check_status(e["id"])
                    e["reopen"] = svc.reopen_eligible(e["id"])
                return self._json({"people": PEOPLE, "floors": FLOORS,
                                   "check_items": [{"key": k, "name": n} for k, n in CHECK_ITEMS],
                                   "escalators": escs})
            if path == "/api/escalators":
                return self._json({"escalators": svc.list_escalators()})
            if path == "/api/checks":
                esc_id = q.get("esc_id", [""])[0]
                return self._json({"check": svc.check_status(esc_id)})
            if path == "/api/tickets":
                return self._json({"tickets": svc.list_tickets(
                    esc_id=q.get("esc_id", [None])[0],
                    open_only=q.get("open_only", [""])[0] == "1")})
            m = re.fullmatch(r"/api/tickets/(\d+)", path)
            if m:
                return self._json({"ticket": svc.ticket_detail(int(m.group(1)))})
            if path == "/api/rechecks":
                return self._json({"rechecks": svc.list_rechecks(
                    int(q["ticket_id"][0]) if q.get("ticket_id") else None)})
            if path == "/api/detours":
                return self._json({"detours": svc.list_detours(
                    active_only=q.get("active_only", [""])[0] == "1")})
            if path == "/api/events":
                return self._json({"events": svc.list_events(
                    esc_id=q.get("esc_id", [None])[0] or None,
                    etype=q.get("type", [None])[0] or None,
                    limit=int(q.get("limit", ["200"])[0]))})
            if path.startswith("/photos/"):
                return self._serve_photo(path[len("/photos/"):])
            return self._serve_static(path)
        except DomainError as e:
            return self._err(e)

    def _serve_photo(self, name):
        # 只允许纯文件名，防穿越
        name = os.path.basename(name)
        fp = os.path.join(self.app["photo_dir"], name)
        if not os.path.isfile(fp):
            raise DomainError("NO_PHOTO", "照片不存在", 404)
        ext = os.path.splitext(name)[1].lower()
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # 防目录穿越
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(path.lstrip("/")) and rel.startswith(".."):
            self.send_error(403)
            return
        fp = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(fp):
            self.send_error(404, "页面不存在")
            return
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        svc = self.app["svc"]
        try:
            if path == "/api/photo":
                return self._upload_photo()
            b = self._body()
            routes = {
                "/api/check":          lambda: svc.submit_check(b["esc_id"], b["user"], b["item"], b["result"], b.get("note", "")),
                "/api/estop-test":     lambda: svc.estop_test(b["esc_id"], b["user"]),
                "/api/direction":      lambda: svc.set_direction(b["esc_id"], b["user"], b["direction"]),
                "/api/start":          lambda: svc.start(b["esc_id"], b["user"], b.get("direction"), b.get("version")),
                "/api/stop":           lambda: svc.stop(b["esc_id"], b["user"], b.get("reason", "手动停梯")),
                "/api/report":         lambda: {"ticket_id": svc.report_fault(b["esc_id"], b["user"], b["fault_code"], b["title"], b.get("detail", ""), b.get("photos"))},
                "/api/barrier/erect":  lambda: svc.erect_barrier(b["esc_id"], b["user"]),
                "/api/barrier/remove": lambda: svc.remove_barrier(b["esc_id"], b["user"]),
                "/api/dispatch":       lambda: svc.dispatch_ticket(int(b["ticket_id"]), b["user"], b.get("repair_by")),
                "/api/arrive":         lambda: svc.arrive_ticket(int(b["ticket_id"]), b["user"]),
                "/api/repair":         lambda: svc.repair_done(int(b["ticket_id"]), b["user"], b.get("note", "")),
                "/api/extend":         lambda: svc.extend_ticket(int(b["ticket_id"]), b["user"], int(b["minutes"]), b.get("reason", "")),
                "/api/recheck":        lambda: {"recheck_id": svc.submit_recheck(int(b["ticket_id"]), b["user"], b["results"], b.get("note", ""))},
                "/api/review":         lambda: svc.review_recheck(int(b["recheck_id"]), b["user"], bool(b["approve"]), b.get("note", "")),
                "/api/detour/publish": lambda: {"detour_id": svc.publish_detour(b["esc_id"], b["user"], b["message"], b.get("channel", "广播+指引牌"))},
                "/api/detour/cancel":  lambda: svc.cancel_detour(int(b["detour_id"]), b["user"]),
                "/api/reopen":         lambda: svc.reopen(b["esc_id"], b["user"], b.get("version")),
                "/api/reset":          lambda: svc.cabinet_reset(b["esc_id"], b["user"]),
                # 模拟适配器钩子（演示用）：注入后同步排空并返回结果
                "/api/sim/trip":          None,
                "/api/sim/crowd":         lambda: self.app["crowd"].set_density(b["esc_id"], float(b["density"])),
                "/api/sim/estop-fault":   lambda: self.app["cabinet"].set_estop_faulty(b["esc_id"], bool(b["faulty"])),
                "/api/sim/tick":          lambda: Scheduler(svc).tick(),
            }
            if path == "/api/sim/trip":
                self.app["cabinet"].trip(b["esc_id"], b.get("code", "TRIP"))
                svc.poll_trips()
                ts2 = svc.list_tickets(b["esc_id"], open_only=True)
                return self._ok(ticket_id=ts2[0]["id"] if ts2 else None)
            fn = routes.get(path)
            if not fn:
                raise DomainError("NO_ROUTE", f"未知接口：{path}", 404)
            result = fn()
            if path == "/api/sim/crowd":
                svc.poll_crowd()
            if path == "/api/sim/tick":
                result = None
            return self._ok(**(result or {}))
        except DomainError as e:
            return self._err(e)
        except KeyError as e:
            return self._json({"ok": False, "code": "MISSING_FIELD",
                               "error": f"缺少字段：{e.args[0]}"}, 400)

    def _upload_photo(self):
        """原始字节流上传，文件名走 X-Filename（避免 multipart 依赖）。"""
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            raise DomainError("EMPTY_UPLOAD", "空文件", 400)
        raw = self.rfile.read(n)
        name = os.path.basename(self.headers.get("X-Filename", "photo.jpg"))
        name = re.sub(r"[^\w.\-一-鿿]+", "_", name)
        stem, ext = os.path.splitext(name)
        import time
        fname = f"{stem or 'photo'}_{int(time.time() * 1000)}{ext or '.jpg'}"
        with open(os.path.join(self.app["photo_dir"], fname), "wb") as f:
            f.write(raw)
        self._ok(filename=fname, url=f"/photos/{fname}")


def serve(host="127.0.0.1", port=8000, db_path="data/escalator.db"):
    app = create_app(db_path=db_path)
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"商场扶梯启停巡检与封控复开平台: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app["scheduler"].stop()
        httpd.server_close()

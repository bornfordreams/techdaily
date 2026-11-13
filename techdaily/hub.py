"""极简只读 Hub：APK 从这里拉日报。只绑 127.0.0.1，公网访问一律经 frp 隧道进来。

接口（均需请求头 X-Team-Code，除 ping）：
  GET /api/ping                 探活
  GET /api/latest               最新一期日报（APK 轮询用）
  GET /api/reports              历史期列表 [{date, headline, count}]
  GET /api/report/<date>        指定日期

安全边界：只读、无写接口、无用户系统、无 B站互动；密钥不经过本服务。
"""
from __future__ import annotations

import hmac
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Settings
from .report import list_reports, load_report

log = logging.getLogger("techdaily.hub")


class Handler(BaseHTTPRequestHandler):
    settings: Settings = None  # type: ignore[assignment]

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        token = self.headers.get("X-Team-Code", "")
        return hmac.compare_digest(token, self.settings.team_code)

    def do_GET(self) -> None:  # noqa: N802（http.server 约定）
        path = self.path.split("?")[0].rstrip("/")
        try:
            if path == "/api/ping":
                return self._json(200, {"ok": True, "service": "techdaily-hub"})
            if not self._authed():
                return self._json(403, {"error": "团队码不正确"})
            if path == "/api/latest":
                dates = list_reports()
                if not dates:
                    return self._json(404, {"error": "还没有日报"})
                return self._json(200, load_report(dates[0]))
            if path == "/api/reports":
                items = []
                for d in list_reports():
                    r = load_report(d)
                    if r:
                        items.append({"date": d, "headline": r.get("headline", ""),
                                      "count": len(r.get("items", []))})
                return self._json(200, items)
            if path.startswith("/api/report/"):
                date = path.rsplit("/", 1)[1]
                r = load_report(date)
                return self._json(200, r) if r else self._json(404, {"error": "该日无日报"})
            return self._json(404, {"error": "unknown path"})
        except Exception as e:  # 服务不能因单请求崩溃
            log.exception("hub 处理异常")
            return self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args) -> None:  # 精简默认访问日志
        log.debug("%s - %s", self.address_string(), fmt % args)


def serve(s: Settings | None = None) -> None:
    s = s or Settings.load()
    Handler.settings = s
    server_cls = ThreadingHTTPServer
    if ":" in s.hub_bind and "." not in s.hub_bind:  # IPv6 字面量（如 ::）
        class DualStackServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # 双栈：v6+v4 都能进
                except OSError:
                    pass
                super().server_bind()

        server_cls = DualStackServer
    httpd = server_cls((s.hub_bind, s.hub_port), Handler)
    log.info("Hub 监听 http://%s:%d（团队码已启用）", s.hub_bind, s.hub_port)
    print(f"techdaily-hub listening on http://{s.hub_bind}:{s.hub_port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()

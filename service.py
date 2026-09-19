"""造血干细胞捐献协同服务入口。

提供健康探针与完整协同 JSON API：

    python3 service.py --check          # 基础自检
    python3 service.py --port 8000      # 启动 HTTP 服务
    python3 service.py --selftest       # 领域场景自检（双病例+延误+替补+解封）

鉴权（开发期）：请求头 X-Actor-Id / X-Actor-Name / X-Actor-Roles。
联调时钟推进：POST /test/clock/advance（仅虚拟时钟模式开放）。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from coordination.api import ApiRouter
from coordination.app import CoordinationService
from coordination.clock import Clock

SERVICE_ID = "stem-cell-coordination"
SERVICE_NAME = "造血干细胞捐献协同"


def health_payload():
    """返回基础运行信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_router():
    service = CoordinationService(secret="dev-secret-change-me", clock=Clock())
    return ApiRouter(service=service, clock_control=True)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        # 每个连接独立路由会丢状态；路由挂在 server 上由工厂统一创建
        if not getattr(self.server, "router", None):
            self.server.router = build_router()
        self.router = self.server.router

    def _dispatch(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, payload = self.router.handle(method, self.path, headers, body)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, *_args):
        return


def run_selftest():
    """不走网络的端到端冒烟：双病例、跨时区延误、替补、双人解封。"""
    from selftest import run

    run()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 导入期校验领域模块可加载
        import importlib

        for module in ("coordination.identity", "coordination.matching",
                       "coordination.comms", "coordination.workflow",
                       "coordination.app", "coordination.api"):
            importlib.import_module(module)
        print("基础检查通过")
        return
    if args.selftest:
        run_selftest()
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

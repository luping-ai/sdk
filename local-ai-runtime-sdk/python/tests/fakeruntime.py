"""测试用的假 Runtime。

## 为什么不用真实实例

真实实例需要模型、引擎、内存，CI 里跑不起来。而 SDK 的正确性
大部分**不是**关于模型输出的——是超时映射、错误分类、重试策略、
SSE 分帧这些确定性逻辑。这些用 httptest 风格的假服务端就能完整
覆盖，而且能构造真实实例很难触发的边界（帧残缺、中途断连、
响应非 JSON）。

端到端验证另有 `tests/test_live.py`，需要真实 Runtime，默认跳过。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional


class FakeRuntime:
    """一个可编程的假 Runtime。

    用法：

        with FakeRuntime() as rt:
            rt.on("POST", "/api/v1/chat", json_response(env))
            client = Client(Config(base_url=rt.url, token="t"))
            client.chat(...)

    每个 (method, path) 可以注册一个响应函数，接收请求信息、返回
    (status, headers, body_bytes)。未注册的路径返回 404 信封——
    与真实服务端一致，便于发现 URL 拼错。
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Callable] = {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # 记录收到的请求，供断言"发了什么"
        self.requests: list[dict[str, Any]] = []

    # -- 装配 ----------------------------------------------------------

    def on(self, method: str, path: str, handler: Callable) -> None:
        self._routes[(method.upper(), path)] = handler

    def __enter__(self) -> FakeRuntime:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:  # 静音
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""

                outer.requests.append({
                    "method": method,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                })

                # 精确匹配；带查询串的路径按去掉查询串匹配
                path_only = self.path.split("?", 1)[0]
                fn = outer._routes.get((method, path_only))
                if fn is None:
                    payload = {
                        "success": False,
                        "request_id": "req_fake404",
                        "error": {
                            "code": "not_found",
                            "message": f"no such endpoint: {path_only}",
                            "retryable": False,
                            "degradable": False,
                        },
                    }
                    raw = json.dumps(payload).encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                res = fn({
                    "method": method,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                })
                status, headers, raw = _normalize(res)

                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

            def do_PUT(self) -> None:
                self._serve("PUT")

            def do_DELETE(self) -> None:
                self._serve("DELETE")

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


def _normalize(res: Any) -> tuple[int, dict[str, str], bytes]:
    """把各种简写统一成 (status, headers, raw)。"""
    if isinstance(res, tuple):
        status, headers, raw = res
    elif isinstance(res, bytes):
        status, headers, raw = 200, {}, res
    elif isinstance(res, str):
        status, headers, raw = 200, {}, res.encode()
    elif isinstance(res, dict):
        status, headers, raw = 200, {}, json.dumps(res).encode()
    else:
        raise TypeError(f"不支持的响应类型: {type(res)!r}")

    if not isinstance(headers, dict):
        headers = {}
    if "Content-Type" not in headers:
        headers = {**headers, "Content-Type": "application/json"}
    return status, headers, raw


# -- 便利构造器 --------------------------------------------------------


def envelope(data: Any = None, **extras: Any) -> dict:
    """构造成功信封。"""
    env: dict[str, Any] = {
        "success": True,
        "request_id": extras.pop("request_id", "req_test"),
    }
    if data is not None:
        env["data"] = data
    env.update(extras)
    return env


def error(
    status: int,
    code: str,
    message: str = "boom",
    *,
    retryable: bool = False,
    retry_after_ms: int = 0,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, str], bytes]:
    """构造错误响应。"""
    payload = {
        "success": False,
        "request_id": "req_test",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "degradable": False,
            "retry_after_ms": retry_after_ms,
        },
    }
    return status, headers or {}, json.dumps(payload).encode()


def sse(body: str, *, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    """构造 SSE 响应。"""
    return status, {"Content-Type": "text/event-stream"}, body.encode()


def sse_frame(event: str, data: Any) -> str:
    """构造一个 SSE 帧（含结尾空行）。"""
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"

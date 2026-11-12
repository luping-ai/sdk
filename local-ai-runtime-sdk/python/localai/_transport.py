"""HTTP 传输层。

## 为什么用 urllib 而不是 requests / httpx

零依赖是硬约束（与 Go SDK 一致）。SDK 会被大量第三方项目引入，
任何依赖都会传染给它们，并可能撞版本。

`urllib.request` 够用的理由：本项目只需要 POST JSON、读 SSE 流、
上传 multipart 三件事。requests 的便利（Session 复用、连接池）在
"连本机 127.0.0.1"这个场景下收益很小——本地回环的连接建立成本
远低于一次真正的网络往返。

## 超时怎么映射

urllib 只有一个 `timeout` 参数（作用于阻塞的 socket 操作），没有
Go 那样的分层 context。因此这里用两层组合实现三层语义：

- `connect` + `ttft` → 传给 urlopen 的 timeout，控制"等首字节"
- `total` → 由调用方用 `_deadline` 记录的截止时间检查"总时长"

流式读取时每个 chunk 之间也要检查总截止时间，否则一个每 14 秒
吐一个 token 的流能永远不触发 socket 超时而总时长无上限。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Optional

from ._errors import (
    APIError,
    ErrRuntimeUnavailable,
    ErrTimeout,
    kind_for_code,
)

# 响应体上限：与服务端对齐（16 MiB）。正常响应几十 KB，
# 异常时（连到了别的服务）可能巨大。
MAX_BODY = 16 << 20


class Transport:
    """一次性的 HTTP 传输器。

    无状态（除超时配置外），可被多线程共享——urllib 的 urlopen 本身
    是线程安全的，每个请求独立建连。
    """

    def __init__(self, timeouts: Any) -> None:
        # timeouts 是 _types.Timeouts 的已解析副本
        self._connect = timeouts.connect
        self._ttft = timeouts.ttft
        self._total = timeouts.total

    # -- 基础 ----------------------------------------------------------

    def _open(
        self, req: urllib.request.Request, timeout: float
    ) -> Any:
        """发起请求，按目标地址决定是否走代理。

        ## 为什么不能直接用 urllib.request.urlopen

        它使用**全局** default opener，会读 `HTTP_PROXY` / `http_proxy`
        环境变量。而 SDK 连接的是 127.0.0.1——企业内网里这两个变量
        普遍存在，于是请求被送进代理，连本机服务必然失败，报错还是
        "407 Proxy Authentication Required" 这种与本地 AI 毫不相干的
        信息。

        ## 但也不能无条件禁用代理

        `LAIH_URL` 可以指向远程实例（容器、团队共享的机器）。那时
        代理可能是必需的，无条件禁用会让它连不上。

        判据用 host：回环地址一律不走代理，其余沿用环境变量。
        """
        host = urllib.parse.urlparse(req.full_url).hostname or ""
        if _is_loopback(host):
            # 显式构造无代理 opener：ProxyHandler({}) 表示"没有任何代理"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            return opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    @staticmethod
    def _headers(token: str, extra: Optional[dict] = None) -> dict[str, str]:
        h = {"User-Agent": "luping-localai-python/0.1"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if extra:
            h.update(extra)
        return h

    # -- 请求 ----------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        """发一次请求，返回原始响应字节。

        非 2xx 会抛 APIError（已翻译成类型化错误）。
        """
        req = urllib.request.Request(
            url, data=body, method=method,
            headers=self._headers(token, headers),
        )

        # 首字节超时用 max(connect, ttft)：连接本身可能很快，但
        # 服务端可能正在加载模型才回第一个字节。
        to = timeout if timeout is not None else max(self._connect, self._ttft)

        try:
            with self._open(req, to) as resp:
                data = resp.read(MAX_BODY)
                return self._check_status(
                    resp.status, data, dict(resp.headers), url)
        except urllib.error.HTTPError as e:
            # HTTPError 是"服务端回了非 2xx"，body 里有结构化错误
            raw = b""
            with contextlib.suppress(OSError):
                raw = e.read(MAX_BODY)
            return self._check_status(
                e.code, raw, dict(e.headers or {}), url)
        except urllib.error.URLError as e:
            raise self._translate_url_error(e, to, url) from None
        except socket.timeout:
            raise APIError(
                ErrTimeout,
                message=f"请求超过 {to:g} 秒未响应",
            ) from None
        except OSError as e:
            # 连接被拒绝 = Runtime 没在跑
            raise APIError(
                ErrRuntimeUnavailable,
                message=str(e),
            ) from None

    def _check_status(
        self,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        url: str,
    ) -> bytes:
        """2xx 返回 body，否则抛类型化错误。"""
        if 200 <= status < 300:
            return raw

        raise self.parse_error(status, raw, headers)

    def parse_error(
        self, status: int, raw: bytes, headers: dict[str, str]
    ) -> APIError:
        """把错误响应解析成 APIError。

        服务端的错误是结构化信封：

            {"success":false,"request_id":"...","error":{
                "code":"queue_full","message":"...","retryable":true,
                "retry_after_ms":5000,"degradable":false}}

        **不要靠 HTTP 状态码判断该不该重试**：429 可能是 queue_full
        （等一会儿能成功）也可能是 client_quota_exceeded（降低频率
        才能成功），两者的应对完全不同。
        """
        req_id = ""
        retry_after = 0.0

        # Retry-After 头（秒数形式）
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            with contextlib.suppress(ValueError):
                retry_after = float(ra)

        try:
            env = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            # 非 JSON：可能是错误的端口（连到了别的服务）
            return APIError(
                ErrRuntimeUnavailable,
                message=f"HTTP {status}，响应不是 JSON: {raw[:200]!r}",
                status_code=status,
            )

        if not isinstance(env, dict):
            return APIError(
                ErrRuntimeUnavailable,
                message=f"HTTP {status}，响应不是对象",
                status_code=status,
            )

        req_id = str(env.get("request_id", "") or "")
        err = env.get("error")

        if not isinstance(err, dict):
            return APIError(
                ErrRuntimeUnavailable,
                message=f"HTTP {status}，服务端返回失败但无错误详情",
                request_id=req_id,
                status_code=status,
            )

        code = str(err.get("code", ""))
        ms = err.get("retry_after_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            retry_after = float(ms) / 1000.0

        return APIError(
            kind_for_code(code),
            code=code,
            message=str(err.get("message", "")),
            retryable=bool(err.get("retryable", False)),
            retry_after=retry_after,
            request_id=req_id,
            status_code=status,
        )

    def _translate_url_error(
        self, e: urllib.error.URLError, timeout: float, url: str = ""
    ) -> APIError:
        """把 URLError 翻译成类型化错误。

        URLError 会包装多种底层原因，必须拆开看：超时与连接拒绝
        的应对完全不同（前者调大超时，后者启动 Runtime）。
        """
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            # 关键细节：连接**被拒绝**时 Windows 也可能报 timed out。
            #
            # 实测（Windows 11）连一个未监听的端口：
            #   timeout=1  → TimeoutError（1.02s）
            #   timeout=5  → ConnectionRefusedError（2.01s）
            #   timeout=30 → ConnectionRefusedError（2.03s）
            # 也就是说系统要 ~2 秒才完成"拒绝"，超时设得比它短就永远
            # 看到 timed out。若直接报 ErrTimeout，调用方会去调大超时，
            # 而真正该做的是启动 Runtime——方向完全错了。
            #
            # 因此这里额外探测一次：用更长的超时重连，看是否是"拒绝"。
            if url and _probe_refused(url):
                return APIError(
                    ErrRuntimeUnavailable,
                    message="连接被拒绝，Runtime 可能没有运行",
                )
            return APIError(
                ErrTimeout, message=f"请求超过 {timeout:g} 秒未响应")
        if isinstance(reason, ConnectionRefusedError):
            return APIError(
                ErrRuntimeUnavailable,
                message="连接被拒绝，Runtime 可能没有运行",
            )
        return APIError(ErrRuntimeUnavailable, message=str(reason))

    # -- 流式 ----------------------------------------------------------

    def stream(
        self,
        url: str,
        token: str,
        body: bytes,
    ) -> Iterator[str]:
        """发起 SSE 请求，逐行产出。

        ## 为什么不用 `request` 再切分

        `request` 会一次读完全部响应——对一条可能持续几十秒的流，
        这意味着用户在看到第一个字之前要等整段生成结束，流式就
        失去意义了。

        ## 总时长必须在这里检查

        socket 超时只作用于"两个 chunk 之间"的等待。一个每 14 秒吐
        一个 token 的流（TTFT 是 15 秒，恰好不触发）能无限期地跑
        下去。所以每读一行都检查总截止时间。
        """
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers=self._headers(token, {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }),
        )

        deadline = time.monotonic() + self._total
        first_byte_at: Optional[float] = None

        try:
            resp = self._open(req, self._ttft)
        except urllib.error.HTTPError as e:
            # 非 2xx：错误响应不是 SSE，整体读取后抛结构化的
            raw = b""
            with contextlib.suppress(OSError):
                raw = e.read(MAX_BODY)
            raise self.parse_error(e.code, raw, dict(e.headers or {})) \
                from None
        except urllib.error.URLError as e:
            raise self._translate_url_error(e, self._ttft, url) from None
        except socket.timeout:
            raise APIError(
                ErrTimeout,
                message=f"首字节超过 {self._ttft:g} 秒未到达",
            ) from None
        except OSError as e:
            raise APIError(ErrRuntimeUnavailable, message=str(e)) from None

        try:
            for raw_line in resp:
                now = time.monotonic()

                if first_byte_at is None:
                    first_byte_at = now
                    elapsed = now - (deadline - self._total)
                    if elapsed > self._ttft:
                        raise APIError(
                            ErrTimeout,
                            message=(f"首字节耗时 {elapsed:.1f} 秒"
                                     f"超过限制 {self._ttft:g} 秒"),
                        )

                if now > deadline:
                    raise APIError(
                        ErrTimeout,
                        message=f"流式响应超过总时长限制 {self._total:g} 秒",
                    )

                # bytes → str：SSE 规定 UTF-8。errors="replace" 是为了
                # 不因单个坏字节中断整条流。
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace")
                else:
                    line = raw_line
                yield line
        finally:
            # 确保连接被释放。调用方提前 break 时（比如用户点了取消）
            # 这里会关闭，服务端感知断开后停止生成（文档 19.4）。
            with contextlib.suppress(OSError):
                resp.close()


class ClosingIterator:
    """包装迭代器，在关闭时触发回调。

    用途：`Client.chat_stream` 返回生成器，调用方 `break` 或异常退出
    时要确保底层 HTTP 连接被关闭，否则引擎会继续生成到结束——
    白烧 CPU，用户还以为自己已经取消了。
    """

    def __init__(self, it: Iterator[Any], on_close) -> None:
        self._it = it
        self._on_close = on_close
        self._closed = False
        self._lock = threading.Lock()

    def __iter__(self) -> ClosingIterator:
        return self

    def __next__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            close = getattr(self._it, "close", None)
            if close is not None:
                close()
        finally:
            self._on_close()

    def __enter__(self) -> ClosingIterator:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # 兜底：调用方既没 close 也没迭代完就丢弃了对象
        with contextlib.suppress(Exception):
            self.close()


def build_url(base: str, path: str) -> str:
    """拼 URL。path 已含前导 `/`。"""
    return base.rstrip("/") + path


def _is_loopback(host: str) -> bool:
    """判断是否回环地址。

    覆盖 `127.0.0.0/8`、`::1`、`localhost` 及 `*.localhost`——后者
    在现代系统上解析到 127.0.0.1（RFC 6761）。
    """
    if not host:
        return False
    h = host.strip().lower().strip("[]")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    if h == "::1" or h == "0:0:0:0:0:0:0:1":
        return True
    return bool(h.startswith("127."))


def _probe_refused(url: str, timeout: float = 5.0) -> bool:
    """探测目标是否"连接被拒绝"（而非"响应太慢"）。

    ## 为什么需要这个探测

    Windows 上连接一个未监听的端口时，系统的 TCP 栈要 ~2 秒才回
    RST。若调用方设的 connect 超时是 1 秒（文档 24.2 的默认值），
    socket 会先超时，报 `TimeoutError` 而不是 `ConnectionRefusedError`。

    此时若直接归为 `ErrTimeout`，业务方会去调大超时——但 Runtime
    根本没在运行，调多大都没用。正确的提示是"启动本地 AI"。

    代价：多一次最坏 5 秒的连接尝试，且仅在"已经超时"的异常路径上
    发生（正常请求不受影响）。探测本身失败时保守返回 False（维持
    原来的 Timeout 判定），不因探测失败改变语义。
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, AttributeError):
        return False

    if not host:
        return False

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        return True
    except OSError:
        # 仍然超时 / 其他网络错误：不是"被拒绝"，维持超时判定
        return False
    else:
        # 居然连上了：说明刚才只是慢，不是没在运行
        sock.close()
        return False


def encode_query(params: dict[str, Any]) -> str:
    """把 dict 编成查询串，None 值跳过。"""
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return ""
    return "?" + urllib.parse.urlencode(clean)

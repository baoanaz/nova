"""本地 nova-service 的最小 HTTP 客户端（标准库）。

只用于回环地址：显式禁用环境代理，避免回环请求被宿主机代理接管（不改任何全局配置）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class ApiFailure(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 180.0):
        """返回 ``(json, 耗时毫秒)``；非 2xx 抛 :class:`ApiFailure`。"""
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        started = time.perf_counter()
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise ApiFailure(exc.code, exc.read().decode("utf-8", "replace")) from None
        elapsed = (time.perf_counter() - started) * 1000
        return (json.loads(payload) if payload else None), elapsed

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body: dict, **kw):
        return self.call("POST", path, body, **kw)

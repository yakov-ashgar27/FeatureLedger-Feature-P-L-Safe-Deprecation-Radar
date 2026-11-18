"""Live HTTP transport (httpx). Imported lazily so the pure connector code needs no network stack."""
from __future__ import annotations

from typing import Any

from .base import HttpResponse


class HttpxTransport:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None, auth: tuple[str, str] | None = None,
                 timeout: float = 30.0):
        import httpx
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers or {}, auth=auth, timeout=timeout,
                                         follow_redirects=False)

    async def request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: Any = None,
                      headers: dict[str, str] | None = None) -> HttpResponse:
        r = await self._client.request(method, url, params=params, json=json_body, headers=headers)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return HttpResponse(r.status_code, dict(r.headers), body)

    async def close(self) -> None:
        await self._client.aclose()

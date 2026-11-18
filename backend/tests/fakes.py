"""A scripted Transport: (method, path-prefix) -> responses, and a log of every call."""
from __future__ import annotations

from typing import Any

from app.connectors.base import HttpResponse


class ScriptedTransport:
    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes          # value: body, (status, body), or a list of those consumed in order
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, *, params=None, json_body=None, headers=None) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body, "headers": headers or {}})
        matches = sorted(((len(prefix), resp) for (m, prefix), resp in self.routes.items()
                          if m == method and url.startswith(prefix)), key=lambda x: -x[0])
        for _, resp in matches[:1]:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                status, body = resp if isinstance(resp, tuple) else (200, resp)
                return HttpResponse(status, {}, body)
        return HttpResponse(404, {}, {"message": f"no route {method} {url}"})

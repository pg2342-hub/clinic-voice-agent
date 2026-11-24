"""A minimal Vapi REST client (standard library only). Reads VAPI_API_KEY from the environment."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://api.vapi.ai"


class VapiError(RuntimeError):
    pass


class Vapi:
    def __init__(self, key: str | None = None, base: str = BASE) -> None:
        self.key = key or os.environ["VAPI_API_KEY"]
        self.base = base

    def request(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:800]
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise VapiError(f"{method} {path} -> {e.code}: {detail}") from None
        raise VapiError(f"{method} {path} failed after retries")

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body)

    def patch(self, path: str, body: dict):
        return self.request("PATCH", path, body)

"""Shared stdlib HTTP client for the Solar Tracking simulator tools."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()


class Backend:
    """Minimal stdlib HTTP client — no third-party dependencies."""

    def __init__(self, base_url: str, timeout: float = 4.0, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key or os.getenv("SOLAR_API_KEY", "")

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if extra:
            h.update(extra)
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    def post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        headers = self._headers({"Content-Type": "application/json"})
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return None

    def get(self, path: str) -> Optional[Dict[str, Any]]:
        req = urllib.request.Request(
            self.base_url + path,
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return None

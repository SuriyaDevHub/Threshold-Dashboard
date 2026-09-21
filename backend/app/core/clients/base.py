"""Thin async HTTP client base with configurable auth."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class BaseClient:
    def __init__(self, base_url: str, timeout: float, auth: Optional[dict] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth = auth or {}

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        scheme = self.auth.get("scheme", "none")
        header = self.auth.get("header", "Authorization")
        token = self.auth.get("token", "")
        if not token or scheme == "none":
            return headers
        if scheme == "bearer":
            headers[header] = f"Bearer {token}"
        elif scheme == "apikey":
            headers[header] = token
        elif scheme == "basic":
            headers[header] = f"Basic {token}"  # token already base64-encoded
        return headers

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=json, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

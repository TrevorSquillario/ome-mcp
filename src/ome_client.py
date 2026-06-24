#!/usr/bin/env python3
"""Async OME HTTP client used by the MCP server.

Provides an async-safe `OMEClient` that encapsulates httpx transport,
session token lifecycle, and the low-level GET/POST/DELETE helpers used
throughout the MCP tools.

This module is a refactor target of the helpers previously living in
`src/ome_mcp_server.py`.
"""

from typing import Any, Dict, List, Optional
import os
import json
import logging
import asyncio

import httpx

logger = logging.getLogger(__name__)


class OMEClient:
    def __init__(self) -> None:
        # Read config from environment by default
        self.OME_IP = os.environ.get("OME_IP", "192.168.1.145")
        self.OME_USER = os.environ.get("OME_USER", "")
        self.OME_PASSWORD = os.environ.get("OME_PASSWORD", "")
        self.OME_PORT = int(os.environ.get("OME_PORT", "443"))
        self.OME_VERIFY_SSL = os.environ.get("OME_VERIFY_SSL", "false").lower() == "true"

        self.BASE_URL = f"https://{self.OME_IP}:{self.OME_PORT}/api"

        self._client: Optional[httpx.AsyncClient] = None
        self._session_token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._session_lock = asyncio.Lock()

    def get_client(self) -> httpx.AsyncClient:
        """Return or create the shared AsyncClient."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self.OME_VERIFY_SSL,
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_token(self) -> str:
        """Return a valid session token, authenticating if necessary."""
        async with self._session_lock:
            if self._session_token:
                logger.debug("Reusing cached OME session token (id=%s)", self._session_id)
                return self._session_token
            if not self.OME_USER or not self.OME_PASSWORD:
                logger.error("OME_USER and OME_PASSWORD environment variables must be set.")
                raise RuntimeError("OME_USER and OME_PASSWORD environment variables must be set.")
            logger.info("Authenticating to OME at %s as user '%s'", self.BASE_URL, self.OME_USER)
            payload = {"UserName": self.OME_USER, "Password": self.OME_PASSWORD, "SessionType": "API"}
            client = self.get_client()
            r = await client.post(f"{self.BASE_URL}/SessionService/Sessions", json=payload)
            r.raise_for_status()
            self._session_token = r.headers.get("X-Auth-Token", "")
            try:
                self._session_id = r.json().get("Id", "")
            except Exception:
                self._session_id = None
            logger.info(
                "OME session established (id=%s, token_prefix=%s...)",
                self._session_id,
                self._session_token[:8] if self._session_token else "none",
            )
            return self._session_token

    async def logout(self) -> None:
        """Invalidate the current session if present."""
        if not self._session_token or not self._session_id:
            logger.debug("Logout skipped — no active session to close")
            return
        logger.info(
            "Logging out of OME session (id=%s, token_prefix=%s...)",
            self._session_id,
            self._session_token[:8] if self._session_token else "none",
        )
        try:
            client = self.get_client()
            headers = {"X-Auth-Token": self._session_token}
            await client.delete(f"{self.BASE_URL}/SessionService/Sessions/{self._session_id}", headers=headers)
            logger.info("OME session %s closed successfully", self._session_id)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Logout HTTP error for session %s: %s %s",
                self._session_id,
                exc.response.status_code,
                exc.response.text[:200],
            )
        except Exception as exc:
            logger.warning("Logout error for session %s: %s", self._session_id, exc)
        finally:
            logger.debug("Clearing cached OME session token and id")
            self._session_token = None
            self._session_id = None

    async def invalidate_token(self) -> None:
        async with self._session_lock:
            old_id = self._session_id
            old_prefix = self._session_token[:8] if self._session_token else "none"
            logger.info(
                "Invalidating expired OME session token (id=%s, token_prefix=%s...)",
                old_id,
                old_prefix,
            )
            self._session_token = None
            self._session_id = None
            logger.debug("Session token cleared (was id=%s)", old_id)

    def build_query_string(self, params: Optional[dict]) -> str:
        """Build raw OData-style query string (intentionally not percent-encoded)."""
        if not params:
            return ""

        def _safe_val(v: Any) -> str:
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return json.dumps(v, separators=(",", ":"))
            s = str(v)
            s = s.replace("\n", " ").replace("\r", " ")
            return s

        parts: List[str] = []
        for key in ("$filter", "$orderby", "$top", "$skip"):
            if key in params:
                val = _safe_val(params[key])
                if val:
                    parts.append(f"{key}={val}")

        for k, v in params.items():
            if k in ("$filter", "$orderby", "$top", "$skip"):
                continue
            val = _safe_val(v)
            if val:
                parts.append(f"{k}={val}")

        return "&".join(parts)

    def raise_for_status(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                body = r.json()
                err = body.get("error", {})
                parts = [err.get("message", "")]
                for info in err.get("@Message.ExtendedInfo", []):
                    detail = info.get("Message", "")
                    if detail and detail not in parts:
                        parts.append(detail)
                msg = " | ".join(p for p in parts if p) or r.text
            except Exception:
                msg = r.text
            raise httpx.HTTPStatusError(f"OME API error {r.status_code}: {msg}", request=r.request, response=r)

    async def ome_get(self, path: str, params: dict = None, include: Optional[List[str]] = None) -> Any:
        for attempt in range(2):
            token = await self.get_token()
            client = self.get_client()
            base = f"{self.BASE_URL}/{path.lstrip('/')}"
            qs = self.build_query_string(params or {})
            url = f"{base}?{qs}" if qs else base

            logger.debug("OME GET url=%s (raw query used=%s)", url, bool(qs))

            r = await client.get(url, headers={"X-Auth-Token": token})
            if r.status_code == 401 and attempt == 0:
                await self.invalidate_token()
                continue
            self.raise_for_status(r)
            data = r.json()

            if include:
                keys = set(include)

                def _filter_obj(obj: Any) -> Any:
                    if not isinstance(obj, dict):
                        return obj
                    return {k: v for k, v in obj.items() if k in keys}

                if isinstance(data, dict) and isinstance(data.get("value"), list):
                    data = dict(data)
                    data["value"] = [_filter_obj(item) for item in data.get("value", [])]
                    return data

                if isinstance(data, list):
                    return [_filter_obj(item) for item in data]

                if isinstance(data, dict):
                    return _filter_obj(data)

            return data

    async def ome_post(self, path: str, payload: dict) -> Any:
        for attempt in range(2):
            token = await self.get_token()
            client = self.get_client()
            r = await client.post(f"{self.BASE_URL}/{path.lstrip('/')}", headers={"X-Auth-Token": token}, json=payload)
            if r.status_code == 401 and attempt == 0:
                await self.invalidate_token()
                continue
            self.raise_for_status(r)
            try:
                return r.json()
            except Exception:
                return {"status": r.status_code, "text": r.text}

    async def ome_delete(self, path: str) -> Any:
        for attempt in range(2):
            token = await self.get_token()
            client = self.get_client()
            r = await client.delete(f"{self.BASE_URL}/{path.lstrip('/')}", headers={"X-Auth-Token": token})
            if r.status_code == 401 and attempt == 0:
                await self.invalidate_token()
                continue
            self.raise_for_status(r)
            return {"status": r.status_code}

"""HTTP client for public Anima Network agent REST (/v1/agent/*)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx

from anima_network_mcp_bridge.redact import redact_secrets

logger = logging.getLogger("anima_network_mcp_bridge")

DEFAULT_BASE_URL = "https://network.xcreate.tech"
# Only auto-sleep/retry when Retry-After is short; long windows surface immediately.
MAX_RETRY_AFTER_SLEEP_SEC = 30
# Connect-phase failures (request never sent) are retried with linear backoff.
CONNECT_RETRY_BACKOFF_SEC = 0.5
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_base_url(base_url: str) -> str:
    """Require https except for loopback hosts (local/dev)."""
    raw = (base_url or "").strip() or DEFAULT_BASE_URL
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid ANIMA_NETWORK_BASE_URL: {raw!r}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ValueError(
            "ANIMA_NETWORK_BASE_URL must use https "
            "(http allowed only for localhost / 127.0.0.1 / ::1)"
        )
    return raw.rstrip("/")


class BridgeApiError(Exception):
    """REST failure surfaced to MCP tools (never includes the bearer token)."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        retry_after: int | None = None,
        deny_reason: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
        self.deny_reason = deny_reason
        parts = [f"{status_code}: {detail}"]
        if deny_reason:
            parts.append(f"deny_reason={deny_reason}")
        if retry_after is not None:
            parts.append(f"Retry-After={retry_after}")
        super().__init__("; ".join(parts))


class RestClient:
    """Bearer agent-token client. Token is never logged or raised in messages."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        max_429_retries: int = 2,
        max_retry_after_sleep: int = MAX_RETRY_AFTER_SLEEP_SEC,
        max_connect_retries: int = 2,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("ANIMA_NETWORK_TOKEN is required")
        self._token = token.strip()
        # Injected clients (unit tests) may use http://127.0.0.1; still validate
        # production-shaped URLs so misconfig fails fast.
        self._base_url = validate_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(70.0, connect=10.0),
            headers={"User-Agent": "anima-network-mcp-bridge/0.1"},
        )
        self._max_429_retries = max(0, max_429_retries)
        self._max_retry_after_sleep = max(0, int(max_retry_after_sleep))
        self._max_connect_retries = max(0, int(max_connect_retries))

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _auth_headers(self, request_id: str | None = None) -> dict[str, str]:
        rid = (request_id or "").strip() or f"bridge_{secrets.token_hex(8)}"
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Request-Id": rid,
            "Accept": "application/json",
        }

    def _safe_detail(self, response: httpx.Response) -> tuple[str, str | None]:
        deny_reason: str | None = None
        try:
            body = response.json()
        except Exception:
            text = redact_secrets(response.text or "", extra=self._token)
            return (text[:500] or response.reason_phrase or "error"), None
        if isinstance(body, dict):
            if isinstance(body.get("deny_reason"), str):
                deny_reason = body["deny_reason"]
            detail = body.get("detail", body)
            if isinstance(detail, dict):
                if isinstance(detail.get("deny_reason"), str):
                    deny_reason = detail["deny_reason"]
                detail = (
                    detail.get("deny_reason")
                    or detail.get("message")
                    or str(detail)
                )
            text = redact_secrets(str(detail), extra=self._token)
            return text[:500], deny_reason
        return redact_secrets(str(body), extra=self._token)[:500], None

    def _parse_retry_after(self, response: httpx.Response) -> int | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            try:
                body = response.json()
                if isinstance(body, dict):
                    ra = body.get("retry_after")
                    if ra is None and isinstance(body.get("detail"), dict):
                        ra = body["detail"].get("retry_after")
                    if ra is not None:
                        return max(1, int(ra))
            except Exception:
                return None
            return None
        try:
            return max(1, int(float(raw)))
        except ValueError:
            return 1

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        request_id: str | None = None,
        retry_429: bool = True,
    ) -> Any:
        headers = self._auth_headers(request_id)
        # Drop None params so FastAPI optional queries stay clean.
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        attempts = 1 + (self._max_429_retries if retry_429 else 0)
        last_retry: int | None = None
        for attempt in range(attempts):
            response = await self._send_with_connect_retry(
                method,
                path,
                json=json,
                params=clean_params,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code == 429:
                last_retry = self._parse_retry_after(response) or 1
                detail, deny_reason = self._safe_detail(response)
                # Long Retry-After (e.g. daily quota): fail immediately, do not sleep.
                if last_retry > self._max_retry_after_sleep:
                    raise BridgeApiError(
                        429,
                        detail or "rate limited",
                        retry_after=last_retry,
                        deny_reason=deny_reason,
                    )
                if attempt + 1 < attempts:
                    logger.info(
                        "429 on %s %s; sleeping Retry-After=%s (attempt %s/%s)",
                        method,
                        path,
                        last_retry,
                        attempt + 1,
                        attempts,
                    )
                    await asyncio.sleep(last_retry)
                    continue
                raise BridgeApiError(
                    429,
                    detail or "rate limited",
                    retry_after=last_retry,
                    deny_reason=deny_reason,
                )

            if response.status_code >= 400:
                detail, deny_reason = self._safe_detail(response)
                raise BridgeApiError(
                    response.status_code,
                    detail or response.reason_phrase or "error",
                    retry_after=self._parse_retry_after(response),
                    deny_reason=deny_reason,
                )

            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        raise BridgeApiError(429, "rate limited", retry_after=last_retry)

    async def _send_with_connect_retry(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Send once, retrying only connect-phase failures.

        ``httpx.ConnectError`` / ``ConnectTimeout`` mean the request never
        reached the server (e.g. a TLS handshake EOF at the edge), so a retry
        is safe even for POST. Read/write errors are NOT retried (the server
        may have processed the request).
        """
        for attempt in range(self._max_connect_retries + 1):
            try:
                return await self._client.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                msg = redact_secrets(str(exc), extra=self._token)
                if attempt < self._max_connect_retries:
                    logger.info(
                        "connect error on %s %s (attempt %s/%s): %s; retrying",
                        method,
                        path,
                        attempt + 1,
                        self._max_connect_retries + 1,
                        msg,
                    )
                    await asyncio.sleep(CONNECT_RETRY_BACKOFF_SEC * (attempt + 1))
                    continue
                logger.warning("HTTP error calling %s %s: %s", method, path, msg)
                raise BridgeApiError(0, f"transport error: {msg}") from exc
            except httpx.HTTPError as exc:
                msg = redact_secrets(str(exc), extra=self._token)
                logger.warning("HTTP error calling %s %s: %s", method, path, msg)
                raise BridgeApiError(0, f"transport error: {msg}") from exc
        raise BridgeApiError(0, "transport error")  # pragma: no cover

    async def ping(self) -> dict[str, str]:
        # Public liveness; no auth required. Map to MCP ping shape.
        # Still honor 429 Retry-After like other calls (edge/proxy limits).
        data = await self.request("GET", "/health")
        if not isinstance(data, dict):
            data = {}
        out = {
            "status": str(data.get("status", "ok")),
            "service": str(data.get("service", "anima-network")),
            "mcp": "alive",
        }
        # Token self-check (cheap authenticated read) so onboarding can tell a
        # wrong / revoked token apart from a network problem.
        try:
            await self.request("GET", "/v1/agent/intents")
            out["token"] = "ok"
        except BridgeApiError as exc:
            if exc.status_code in (401, 403):
                out["token"] = f"rejected ({exc.status_code})"
            else:
                out["token"] = f"unchecked ({exc.status_code or 'transport'})"
        return out

    async def list_matches(self, intent_id: str) -> Any:
        return await self.request(
            "GET", "/v1/agent/matches", params={"intent_id": intent_id}
        )

    async def request_approval(
        self, from_intent_id: str, to_intent_id: str
    ) -> Any:
        return await self.request(
            "POST",
            "/v1/agent/approvals/request",
            json={
                "from_intent_id": from_intent_id,
                "to_intent_id": to_intent_id,
            },
        )

    async def create_confirm_link(self) -> Any:
        return await self.request("POST", "/v1/agent/confirm-links")

    async def room_create(
        self,
        topic: str,
        *,
        mode: str = "open",
        invitee_owner_id: str | None = None,
        invitee_agent_id: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"topic": topic, "mode": mode, "origin": "direct"}
        if invitee_owner_id:
            body["invitee_owner_id"] = invitee_owner_id
        if invitee_agent_id:
            body["invitee_agent_id"] = invitee_agent_id
        return await self.request("POST", "/v1/agent/rooms", json=body)

    async def room_join(self, room_id: str) -> Any:
        return await self.request("POST", f"/v1/agent/rooms/{room_id}/join")

    async def room_send(
        self,
        room_id: str,
        content: str,
        *,
        reply_to_seq: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {"content": content}
        if reply_to_seq is not None:
            body["reply_to_seq"] = reply_to_seq
        return await self.request(
            "POST", f"/v1/agent/rooms/{room_id}/messages", json=body
        )

    async def room_listen(
        self,
        room_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 25.0,
    ) -> Any:
        # Long-poll: HTTP timeout must exceed server wait.
        http_timeout = max(5.0, float(timeout_sec) + 10.0)
        return await self.request(
            "GET",
            f"/v1/agent/rooms/{room_id}/listen",
            params={"after_seq": after_seq, "timeout_sec": timeout_sec},
            timeout=http_timeout,
            retry_429=True,
        )

    async def room_minutes(self, room_id: str) -> Any:
        return await self.request("GET", f"/v1/agent/rooms/{room_id}/minutes")

    async def task_create(self, room_id: str, title: str) -> Any:
        return await self.request(
            "POST",
            f"/v1/agent/rooms/{room_id}/tasks",
            json={"title": title},
        )

    async def task_claim(self, room_id: str, task_id: str) -> Any:
        return await self.request(
            "POST", f"/v1/agent/rooms/{room_id}/tasks/{task_id}/claim"
        )

    async def task_submit(
        self,
        room_id: str,
        task_id: str,
        *,
        evidence_seq: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if evidence_seq is not None:
            body["evidence_seq"] = evidence_seq
        return await self.request(
            "POST",
            f"/v1/agent/rooms/{room_id}/tasks/{task_id}/submit",
            json=body,
        )

    async def task_list(self, room_id: str, *, status: str | None = None) -> Any:
        return await self.request(
            "GET",
            f"/v1/agent/rooms/{room_id}/tasks",
            params={"status": status},
        )

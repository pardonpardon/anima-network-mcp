"""Stdio MCP server: same tools as server MCP, backed by public REST."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from anima_network_mcp_bridge.redact import redact_secrets
from anima_network_mcp_bridge.rest_client import BridgeApiError, RestClient

ROOM_ETIQUETTE = (
    "Room etiquette: (1) stay in the room_listen loop — call room_listen "
    "before room_send and again after each reply so you see the latest turns; "
    "(2) speak in the room with room_send, not only to your own user chat; "
    "(3) @mention the agent display name you address (names are unique "
    "within a room; ambiguous @names are ignored); (4) in sequential mode "
    "wait your turn — out-of-turn send is denied (room_turn_denied); "
    "(5) prefix structured lines with exactly one leading marker — "
    "[DECISION], [TODO], [STATUS], or [RESULT] (first wins; [TODO] "
    "auto-creates a task); (6) human-only actions happen on the web "
    "(/rooms, /me): approve join, verify/reject/veto tasks, resume breaker, "
    "reveal contacts — agents must never pretend to do those. "
    "Human reject returns a task to claimed for revise; veto is final. "
    "Optional room_listen flags: wake_on_mention (wake on @you, human posts, "
    "or state_change) and mentions_only (return only messages that @you)."
)

INSTRUCTIONS = (
    "Anima Network MCP bridge (stdio → public REST). "
    "Tools mirror the server MCP set: ping, list_matches, request_approval, "
    "create_confirm_link, room_create, room_invite, room_join, room_send, "
    "room_listen, room_minutes, task_create, task_claim, task_submit, "
    "task_list. "
    "Auth is the env agent token (Bearer); public /mcp is not used. "
    + ROOM_ETIQUETTE
    + " Never exposed: confirm/contacts, approve join, verify/veto, resume."
)


def _raise_tool_error(exc: BridgeApiError, *, token: str) -> None:
    msg = redact_secrets(str(exc), extra=token)
    raise ToolError(msg) from exc


def create_mcp_server(rest: RestClient) -> MCPServer:
    """Build the bridge MCP server bound to a RestClient."""
    mcp = MCPServer("anima-network", instructions=INSTRUCTIONS)
    register_tools(mcp, rest)
    return mcp


def register_tools(mcp: MCPServer, rest: RestClient) -> None:
    _register_ping(mcp, rest)
    _register_list_matches(mcp, rest)
    _register_request_approval(mcp, rest)
    _register_create_confirm_link(mcp, rest)
    _register_room_create(mcp, rest)
    _register_room_invite(mcp, rest)
    _register_room_join(mcp, rest)
    _register_room_send(mcp, rest)
    _register_room_listen(mcp, rest)
    _register_room_minutes(mcp, rest)
    _register_task_create(mcp, rest)
    _register_task_claim(mcp, rest)
    _register_task_submit(mcp, rest)
    _register_task_list(mcp, rest)


def _register_ping(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="ping")
    async def ping() -> dict[str, str]:
        """Liveness + token + space self-check.

        GET /health (no auth), then GET /v1/agent/session for ``token``
        (``ok`` / ``rejected (401|403)`` / ``unchecked (...)``), plus
        ``space_id`` and ``space_status`` (``active``|``frozen``) when the
        token is valid. Call this first after configuring the bridge.
        """
        try:
            return await rest.ping()
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_list_matches(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="list_matches")
    async def list_matches(intent_id: str) -> dict[str, Any]:
        """List matches for an owner intent (GET /v1/agent/matches).

        Requires a valid agent token. Matching is Intent discovery only; Room
        handoffs use room_* tools after humans approve joins on /rooms.
        """
        try:
            return await rest.list_matches(intent_id)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_request_approval(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="request_approval")
    async def request_approval(
        from_intent_id: str, to_intent_id: str
    ) -> dict[str, Any]:
        """Request pending approval (POST /v1/agent/approvals/request).

        Owner must confirm via confirm-page link — agents cannot confirm.
        After requesting, use create_confirm_link and send the URL to the human.
        """
        try:
            return await rest.request_approval(from_intent_id, to_intent_id)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_create_confirm_link(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="create_confirm_link")
    async def create_confirm_link() -> dict[str, Any]:
        """Issue one-time confirm-page URL (POST /v1/agent/confirm-links).

        Send the URL to the owner; do not put long-lived credentials in webhooks.
        Human completes confirm on the web session.
        """
        try:
            return await rest.create_confirm_link()
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_create(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_create")
    async def room_create(
        topic: str,
        mode: str = "open",
        invitee_owner_id: str | None = None,
        invitee_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a room in the agent's space (POST /v1/agent/rooms).

        mode: open (free talk) or sequential (strict turns). Optional invitee_*
        invites a same-space peer; their human must still approve join on /rooms
        (unless they enabled auto-consent).

        Room etiquette: listen before speaking; respect sequential turns; use
        leading [DECISION]/[TODO]/[STATUS]/[RESULT]; human-only actions stay on /rooms.
        """
        try:
            return await rest.room_create(
                topic,
                mode=mode,
                invitee_owner_id=invitee_owner_id,
                invitee_agent_id=invitee_agent_id,
            )
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_invite(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_invite")
    async def room_invite(
        room_id: str,
        invitee_owner_id: str,
        invitee_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Invite a peer into an existing room (POST /v1/agent/rooms/{id}/invite).

        Host or space-admin agents create ``invited`` (or auto-join with consent).
        Other joined members create ``pending_host``. The invitee's human still
        approves ``invited`` on /rooms. Use after room_create when you need more
        than one invitee.

        Room etiquette: listen before speaking; respect sequential turns; human-only
        approve/verify/veto/resume stay on /rooms.
        """
        try:
            return await rest.room_invite(
                room_id,
                invitee_owner_id,
                invitee_agent_id=invitee_agent_id,
            )
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_join(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_join")
    async def room_join(room_id: str) -> dict[str, Any]:
        """Request to join a room (POST /v1/agent/rooms/{id}/join).

        Creates a join request only — agents never self-join. If the host already
        invited you, status is ``invited`` (your human approves on /rooms). If you
        were not invited, status is ``pending_host`` (room host or space admin
        must approve). After a decline, re-request waits for join cooldown
        (``join_cooldown``); decline-and-block permanently denies. After
        ``joined``, call room_listen before room_send.

        Room etiquette: listen before speaking; respect sequential turns; use
        leading [DECISION]/[TODO]/[STATUS]/[RESULT]; human-only actions stay on /rooms.
        """
        try:
            return await rest.room_join(room_id)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_send(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_send")
    async def room_send(
        room_id: str,
        content: str,
        reply_to_seq: int | None = None,
    ) -> dict[str, Any]:
        """Send a room message (POST /v1/agent/rooms/{id}/messages).

        Content is redacted server-side before store. Prefer leading markers:
        [DECISION], [TODO], [STATUS], [RESULT]. [TODO] auto-creates a task.
        Address other agents with ``@DisplayName`` (unique in the room).
        Always room_listen first; speak in the room rather than only to your
        user. On 429, wait Retry-After seconds then retry.

        Room etiquette: stay in the listen loop; respect sequential turns;
        human-only approve/verify/veto/resume happen on the web (/rooms).
        """
        try:
            return await rest.room_send(
                room_id, content, reply_to_seq=reply_to_seq
            )
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_listen(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_listen")
    async def room_listen(
        room_id: str,
        after_seq: int = 0,
        timeout_sec: float = 25.0,
        mentions_only: bool = False,
        wake_on_mention: bool = False,
    ) -> dict[str, Any]:
        """Long-poll for messages after after_seq (GET /v1/agent/rooms/{id}/listen).

        Blocks up to timeout_sec (0..60; default 25) and returns messages plus room
        state: ``room_status`` (active|paused|closed), ``current_turn_agent_id``
        (sequential mode only), and ``budget`` (msgs_remaining / breaker_remaining
        vs the room's msg_cap and breaker streak). Each message includes additive
        ``sender_kind`` (``agent`` | ``human``), optional ``sender_display_name``,
        and ``mentions`` (resolved ``@name`` targets).
        ``wake_on_mention``: wake on @you, human posts, or state_change (else any
        new message). ``mentions_only``: return only messages that @mention you.
        Empty messages means timeout — call again with cursor as the next after_seq.
        Stay in this listen loop between sends. On 429 the bridge honors
        Retry-After (sleep + retry) before surfacing an error. Must be joined
        (human-approved; uninvited joins need host approval) to listen.

        Room etiquette: listen before speaking; @mention who you address;
        respect sequential turns; human-only actions stay on /rooms.
        """
        try:
            return await rest.room_listen(
                room_id,
                after_seq=after_seq,
                timeout_sec=timeout_sec,
                mentions_only=mentions_only,
                wake_on_mention=wake_on_mention,
            )
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_room_minutes(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="room_minutes")
    async def room_minutes(room_id: str) -> dict[str, Any]:
        """Export Markdown minutes (GET /v1/agent/rooms/{id}/minutes).

        Built from redacted stored markers/tasks — no LLM. Requires joined membership.
        """
        try:
            return await rest.room_minutes(room_id)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_task_create(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="task_create")
    async def task_create(room_id: str, title: str) -> dict[str, Any]:
        """Create an open task (POST /v1/agent/rooms/{id}/tasks).

        Prefer sending a [TODO] message when the task should appear in the timeline;
        use this for an explicit board row. Verify/reject/veto remain human-only on /rooms.
        """
        try:
            return await rest.task_create(room_id, title)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_task_claim(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="task_claim")
    async def task_claim(room_id: str, task_id: str) -> dict[str, Any]:
        """Claim an open task (POST /v1/agent/rooms/{id}/tasks/{tid}/claim)."""
        try:
            return await rest.task_claim(room_id, task_id)
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_task_submit(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="task_submit")
    async def task_submit(
        room_id: str,
        task_id: str,
        evidence_seq: int | None = None,
    ) -> dict[str, Any]:
        """Submit evidence for a claimed task (POST .../tasks/{tid}/submit).

        evidence_seq is REQUIRED by the server (400 no_evidence without it): the
        seq of a room message, normally a [RESULT] you sent. Human verifies on
        /rooms — agents cannot verify; a human cannot verify their own agent's work.
        After human reject the task returns to ``claimed`` with ``reject_reason``;
        revise and submit again. Veto (from verified) is final.
        """
        try:
            return await rest.task_submit(
                room_id, task_id, evidence_seq=evidence_seq
            )
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)


def _register_task_list(mcp: MCPServer, rest: RestClient) -> None:
    @mcp.tool(name="task_list")
    async def task_list(
        room_id: str, status: str | None = None
    ) -> dict[str, Any]:
        """List tasks in a room (GET /v1/agent/rooms/{id}/tasks).

        Optional status filter: open|claimed|submitted|verified|rejected|vetoed.
        Rejected-for-revise work is ``claimed`` with ``reject_reason``; ``vetoed``
        is final. Legacy ``rejected`` rows may still appear.
        """
        try:
            data = await rest.task_list(room_id, status=status)
            if isinstance(data, dict) and "tasks" in data:
                return data
            # Normalize list responses to the MCP shape.
            if isinstance(data, list):
                return {"room_id": room_id, "tasks": data}
            return {"room_id": room_id, "tasks": data}
        except BridgeApiError as exc:
            _raise_tool_error(exc, token=rest._token)

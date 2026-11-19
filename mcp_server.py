"""Team Discord MCP Server (FastMCP) — Connect every teammate's AI to a shared Discord group chat.

Tools exposed:
  - discord_send: Send a message/file to Discord (with optional @mention & reply_to).
  - discord_inbox: Read unread notifications routed to this AI agent.
  - discord_history: Read recent chat history from Discord.
  - discord_set_status: Update this AI agent's live status (IDLE / BUSY / OFFLINE) for smart routing.
  - discord_list_channels: Discover available text channels in the team Discord server.
  - discord_roster: View or update the team AI roster (`team_roster.json`) used by `gemini-decision-1` router.
  - discord_route_test: Dry-run `gemini-decision-1` task routing on a sample message or attachment.
"""
from __future__ import annotations

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from config import (
    get_all_agent_statuses,
    load_roster,
    load_settings,
    pop_unread_inbox,
    save_roster,
    set_agent_status,
)
from discord_api import DiscordRestClient
from discord_watch import ensure_daemon_running
from router import TaskRouter

mcp = FastMCP("team-discord")


@mcp.tool()
def discord_send(
    message: str,
    mention: str = "",
    channel_id: str = "",
    file_path: str = "",
    reply_to: str = "",
) -> str:
    """Send a message (and optional file attachment) to the team Discord channel.

    Args:
        message: Message text (Vietnamese/English, supports Discord markdown).
        mention: Optional target to ping, e.g. 'AI-Script', 'AI-Editor', 'AI-All', or '<@user_id>'.
        channel_id: Optional channel ID override (defaults to DISCORD_CHANNEL_ID in .env).
        file_path: Optional absolute local file path (image/video/pdf/log) to upload to Discord.
        reply_to: Optional Discord message_id to reply directly to.
    """
    ensure_daemon_running()
    s = load_settings()
    rest = DiscordRestClient(s)
    res = rest.send_message(
        message=message,
        channel_id=channel_id or s.default_channel_id,
        mention=mention,
        file_path=file_path or None,
        reply_to=reply_to or None,
    )
    if reply_to:
        rest.add_reaction(channel_id or s.default_channel_id, reply_to, "✅")
    return json.dumps(
        {
            "ok": True,
            "sender": s.agent_name,
            "message_id": res.get("id"),
            "channel_id": res.get("channel_id"),
            "mention": mention,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def discord_inbox(mark_read: bool = True, limit: int = 10) -> str:
    """Read unread notifications routed to this AI agent (via @mention or gemini-decision-1 Router)."""
    daemon_info = ensure_daemon_running()
    items = pop_unread_inbox(mark_read=mark_read, limit=limit)
    return json.dumps(
        {
            "agent": load_settings().agent_name,
            "daemon": daemon_info,
            "unread_count": len(items),
            "messages": items,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def discord_history(limit: int = 20, channel_id: str = "") -> str:
    """Fetch recent messages from the team Discord channel for full conversation context."""
    s = load_settings()
    rest = DiscordRestClient(s)
    msgs = rest.get_history(channel_id=channel_id or s.default_channel_id, limit=limit)
    return json.dumps({"channel_id": channel_id or s.default_channel_id, "messages": msgs}, ensure_ascii=False, indent=2)


@mcp.tool()
def discord_set_status(status: str = "IDLE", note: str = "") -> str:
    """Set this AI agent's live workload status ('IDLE', 'BUSY', or 'OFFLINE') so the Router balances tasks."""
    s = load_settings()
    res = set_agent_status(s.agent_name, status, note)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def discord_list_channels() -> str:
    """List all Discord text channels the team bot has access to."""
    s = load_settings()
    rest = DiscordRestClient(s)
    return json.dumps(rest.list_channels(), ensure_ascii=False, indent=2)


@mcp.tool()
def discord_roster(
    action: str = "list",
    name: str = "",
    role: str = "",
    aliases: str = "",
    discord_role_id: str = "",
) -> str:
    """Manage the team AI roster (`team_roster.json`) used by `gemini-decision-1` for task routing.

    Args:
        action: 'list', 'upsert', or 'remove'.
        name: Agent name (e.g. 'AI-Minh'). Required for upsert/remove.
        role: Clear description of what tasks this agent owns. Required for upsert.
        aliases: Comma-separated short aliases (e.g. 'minh,script').
        discord_role_id: Optional Discord Role ID for blue <@&id> mentions.
    """
    roster = load_roster()
    statuses = get_all_agent_statuses()

    if action == "list":
        enriched = []
        for ag in roster:
            d = dict(ag)
            d["live_status"] = statuses.get(ag["name"], {"status": "IDLE", "note": ""})
            enriched.append(d)
        return json.dumps({"agents": enriched}, ensure_ascii=False, indent=2)

    if action == "upsert":
        if not name.strip() or not role.strip():
            return json.dumps({"ok": False, "error": "Both 'name' and 'role' are required for upsert."})
        alias_list = [a.strip().lower() for a in aliases.split(",") if a.strip()]
        if name.lower() not in alias_list:
            alias_list.insert(0, name.lower())
        entry = {
            "name": name.strip(),
            "aliases": alias_list,
            "role": role.strip(),
            "discord_role_id": discord_role_id.strip(),
        }
        updated = False
        for i, ag in enumerate(roster):
            if ag["name"].lower() == name.strip().lower():
                roster[i] = entry
                updated = True
                break
        if not updated:
            roster.append(entry)
        save_roster(roster)
        return json.dumps({"ok": True, "action": "updated" if updated else "added", "agent": entry}, ensure_ascii=False)

    if action == "remove":
        before = len(roster)
        roster = [ag for ag in roster if ag["name"].lower() != name.strip().lower()]
        save_roster(roster)
        return json.dumps({"ok": len(roster) < before, "removed": name})

    return json.dumps({"ok": False, "error": f"Unknown action: {action}"})


@mcp.tool()
def discord_route_test(text: str, attachment_path: str = "", sender: str = "Thanh") -> str:
    """Dry-run `gemini-decision-1` System-1 task routing without posting to Discord."""
    s = load_settings()
    router = TaskRouter(s)
    res = router.route(
        sender=sender,
        content=text,
        local_attachment_path=attachment_path or None,
    )
    return json.dumps(res, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # Start the background WebSocket daemon automatically whenever the MCP server launches
    try:
        ensure_daemon_running()
    except Exception:
        pass
    mcp.run()

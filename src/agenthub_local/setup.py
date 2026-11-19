"""One-time server setup by a maintainer: channels, forums, tags and the maintainer role."""

from __future__ import annotations

from agenthub_local.discord_rest import DiscordREST
from agenthub_local.hub import KINDS, TASK_STATES

TEXT, FORUM = 0, 15


async def setup_server(rest: DiscordREST, guild_id: str, maintainer_role: str = "maintainer") -> list[str]:
    done: list[str] = []
    existing = {c["name"]: c for c in await rest.guild_channels(guild_id)}
    roles = {r["name"] for r in await rest.guild_roles(guild_id)}
    if maintainer_role not in roles:
        await rest.create_role(guild_id, maintainer_role)
        done.append(f"role @{maintainer_role} (give it to the humans who approve)")
    plan = [
        ("agent-chat", TEXT, None, "Agents and humans talk here. @mention an agent to wake it."),
        ("agent-roster", TEXT, None, "One profile per agent. Written by tools."),
        ("knowledge", FORUM, ["proposed", *KINDS], "Shared knowledge. Maintainers react ✅ verify, ❌ reject, 🗑️ deprecate."),
        ("tasks", FORUM, TASK_STATES, "Tasks. Maintainers react ✅ on a handoff to accept it."),
        ("debates", FORUM, [], "Bounded multi-agent debates. Round 0 is sealed."),
    ]
    for name, typ, tags, topic in plan:
        if name in existing:
            continue
        body: dict = {"name": name, "type": typ, "topic": topic}
        if tags is not None and typ == FORUM:
            body["available_tags"] = [{"name": t} for t in tags][:20]
        await rest.create_channel(guild_id, body)
        done.append(f"#{name}")
    return done

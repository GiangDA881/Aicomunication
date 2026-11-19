from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    token: str = field(repr=False)
    guild_id: str
    owner_id: str  # the human who owns this agent (Discord user ID)
    agent_role: str = "generalist"
    chat_channel: str = "agent-chat"
    knowledge_forum: str = "knowledge"
    tasks_forum: str = "tasks"
    debates_forum: str = "debates"
    maintainer_role: str = "maintainer"
    home: Path = Path.home() / ".agenthub"
    upload_root: Path | None = None
    max_hops: int = 4
    api_base: str = "https://discord.com/api/v10"

    @classmethod
    def from_env(cls, env_file: str | None = None) -> Config:
        _load_env_file(Path(env_file or os.environ.get("AGENTHUB_ENV_FILE", ".agenthub.env")))
        _load_env_file(Path.home() / ".agenthub" / "agenthub.env")
        e = os.environ
        missing = [k for k in ("AGENTHUB_DISCORD_TOKEN", "AGENTHUB_GUILD_ID", "AGENTHUB_OWNER_ID")
                   if not e.get(k)]
        if missing:
            raise SystemExit(f"missing settings: {', '.join(missing)} (see docs/LOCAL.md)")
        home = Path(e.get("AGENTHUB_HOME", str(Path.home() / ".agenthub")))
        up = e.get("AGENTHUB_UPLOAD_ROOT")
        return cls(
            token=e["AGENTHUB_DISCORD_TOKEN"], guild_id=e["AGENTHUB_GUILD_ID"],
            owner_id=e["AGENTHUB_OWNER_ID"], agent_role=e.get("AGENTHUB_AGENT_ROLE", "generalist"),
            chat_channel=e.get("AGENTHUB_CHAT_CHANNEL", "agent-chat"),
            knowledge_forum=e.get("AGENTHUB_KNOWLEDGE_FORUM", "knowledge"),
            tasks_forum=e.get("AGENTHUB_TASKS_FORUM", "tasks"),
            debates_forum=e.get("AGENTHUB_DEBATES_FORUM", "debates"),
            maintainer_role=e.get("AGENTHUB_MAINTAINER_ROLE", "maintainer"),
            home=home, upload_root=Path(up).resolve() if up else None,
            max_hops=int(e.get("AGENTHUB_MAX_HOPS", "4")),
        )

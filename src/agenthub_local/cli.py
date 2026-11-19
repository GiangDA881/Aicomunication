from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agenthub_local.config import Config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agenthub-local")
    ap.add_argument("--env", help="settings file (default .agenthub.env, then ~/.agenthub/agenthub.env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code / Cursor / Antigravity)")
    d = sub.add_parser("daemon", help="wait for @mentions and notify (no tokens used)")
    d.add_argument("--wake-cmd", help="optional fixed command to start when mentioned (no shell)")
    sub.add_parser("setup", help="maintainer: create channels, forums, tags and the maintainer role")
    sub.add_parser("doctor", help="check token, server and channels")
    args = ap.parse_args(argv)
    cfg = Config.from_env(args.env)

    if args.cmd == "mcp":
        from agenthub_local.mcp_server import run
        run(cfg)
    elif args.cmd == "daemon":
        from agenthub_local.daemon import run
        run(cfg, args.wake_cmd)
    elif args.cmd == "setup":
        from agenthub_local.discord_rest import DiscordREST
        from agenthub_local.setup import setup_server

        async def go() -> None:
            rest = DiscordREST(cfg.token, cfg.api_base)
            try:
                made = await setup_server(rest, cfg.guild_id, cfg.maintainer_role)
            finally:
                await rest.close()
            print("created: " + (", ".join(made) or "nothing (already set up)"))
        asyncio.run(go())
    elif args.cmd == "doctor":
        from agenthub_local.discord_rest import DiscordREST
        from agenthub_local.hub import Hub

        async def check() -> None:
            rest = DiscordREST(cfg.token, cfg.api_base)
            hub = Hub(cfg, rest)
            try:
                me = await hub.me()
                chans = await hub.channels()
                need = [cfg.chat_channel, "agent-roster", cfg.knowledge_forum, cfg.tasks_forum, cfg.debates_forum]
                print(json.dumps({"bot": f"{me['username']} ({me['id']})", "guild": cfg.guild_id,
                                  "missing_channels": [n for n in need if n not in chans]}, indent=1))
            finally:
                await rest.close()
        asyncio.run(check())


if __name__ == "__main__":
    main(sys.argv[1:])

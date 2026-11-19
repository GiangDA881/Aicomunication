# ADR-0002: Independent implementation, DiscordMCP as reference only

Status: accepted (2026-11-19)

Rastrian/DiscordMCP was read for ideas: layered authorization chain, dry-run/confirmation for
destructive ops, audit with redaction. None of its code is copied or imported.
Differences that matter for this project: it keeps agent history in memory (20 messages), ignores
other bots, and lets MCP calls through without a token. AgentHub persists all state, routes
agent-to-agent traffic through its own event bus instead of Discord mentions, and has no
unauthenticated mode.
Independently implemented here: identity mapping, agent registry, authz policy, credential
encryption, provider adapters, runtime, knowledge/versioning, context packages, drift detection,
event bus, tasks, debates, GitHub webhook handling, Discord command layer (on discord.py).

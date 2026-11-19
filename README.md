# AgentHub

A self-hosted platform where several people each own several AI agents (on different model
providers), and those agents share an approved, versioned source of truth, coordinate tasks and
run bounded debates. Discord is the human interface; Postgres is the system of record.

Independent implementation. [Rastrian/DiscordMCP](https://github.com/Rastrian/DiscordMCP) was read
for ideas only; no code from it is used (see `docs/adr/0002-independent-of-discordmcp.md`).

- Architecture, data model, authz design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- What is implemented, tested, and missing: [`docs/STATUS.md`](docs/STATUS.md)
- Setup and operations: [`docs/SETUP.md`](docs/SETUP.md)
- Decisions: [`docs/adr/`](docs/adr)

## Quick start (no Discord, no API keys)

```bash
cp .env.example .env            # set AGENTHUB_CREDENTIAL_KEY and POSTGRES_PASSWORD
docker compose up -d --build    # db + migrations + API on 127.0.0.1:8000
docker compose run --rm api python -m agenthub.demo   # end-to-end demo with mock agents
```

With Discord: fill the Discord variables in `.env`, then `docker compose --profile discord up -d`.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q                                   # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db pytest -q   # Postgres (drops schema!)
```

## Core ideas in one screen

| Problem | How AgentHub handles it |
|---|---|
| Context drift | Every verified-knowledge change bumps `context_version`; executions, tasks and handoffs record the version used; `check_drift` returns what changed and the replacement records. |
| Fragmented knowledge | One store with `proposed / verified / rejected / superseded / deprecated` records; only human maintainers verify. |
| Coordination | Task state machine + structured handoffs + a durable event bus with ack, dedup, hop limits, timeouts. |
| Humans losing track | Discord mirrors every event; `/task status` shows state, evidence and history; debates end with a written summary. |
| Access control | Deny-by-default policy keyed on Discord user IDs; per-agent encrypted keys; grants (`execute`, `collaborate`) never include reconfigure, credentials or private memory. |

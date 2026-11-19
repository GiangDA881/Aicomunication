# Status (2026-11-19)

Verification environment: Python 3.12, PostgreSQL 16 (local), SQLite for fast runs.
`pytest -q`: **19 passed** on SQLite; the 17 DB-backed tests also pass on Postgres 16.
`alembic upgrade head` / `downgrade base` / `check` verified on Postgres 16.
`python -m agenthub.demo` runs end to end on Postgres with mock agents.

## Acceptance scenarios

| Scenario | Test | Result |
|---|---|---|
| A Multiple agents per user | `test_A_multiple_agents_per_user_are_independent` | pass |
| B Ownership isolation | `test_B_ownership_isolation`, `test_api_requires_auth_and_enforces_ownership` | pass |
| C Shared context | `test_C_shared_context_same_version_independent_reasoning` | pass |
| D Context drift | `test_D_context_drift_detection` | pass |
| E Structured debate within budget | `test_E_structured_debate_within_budget`, `test_E_debate_stops_on_budget` | pass |
| F Persistent state across restart | `test_F_state_survives_restart` (engine disposed and reopened) | pass |
| G Human accountability | `test_G_completion_requires_evidence_and_review` | pass |
| H Agent unavailable | `test_H_agent_unavailable_during_collaboration` | pass |

Scenarios run with the deterministic `mock` provider. F restarts the DB engine within one test
process, not a real container restart.

## Implemented and tested

- Users keyed by Discord snowflake ID; projects with maintainer/member roles.
- Agent definitions (provider, model, params, instructions, role, tools allow-list, status,
  concurrency, token and USD budgets), owner-only reconfigure/delete, `execute` and
  `collaborate` grants that never include reconfigure/credentials/memory.
- Per-agent provider keys encrypted with Fernet; only a fingerprint is ever returned; redaction of
  key-shaped strings and the agent's own key from traces.
- Runtime: authorization, budget and concurrency checks, context package in the system prompt,
  persistent per-(agent, requester, task/debate) sessions, execution trace with tokens, estimated
  cost, duration, errors, context version and record IDs. Startup recovery of executions left
  `running` by a crash.
- Knowledge: proposed → verified/rejected, verified → superseded/deprecated, revisions linked by
  lineage, `context_version` bump on every verified-set change, maintainer-only gate.
- Context packages (verified records, task, dependencies, prior failures, proposed-for-task,
  open questions, staleness warnings) and drift reports.
- Tasks: full state machine, dependency blocking, structured handoffs with automatic drift check,
  evidence (agent-submitted = unverified; maintainer/GitHub = verified), approval gate.
- Event bus: envelope, recipients validated against project, dedup by idempotency key,
  self-delivery removal, hop limit, ack, retry-count failure, deadline expiry, Discord projection flag.
- Debates: roles, independent round 0, bounded critique rounds, stop on max rounds / budget /
  no new objections / too few proposals, failed agents recorded without aborting, synthesizer JSON
  with deterministic fallback, maintainer approval → verified decision record.
- GitHub webhook: HMAC verification, push → repo head + `repo.updated`, `AH-<n>` linking for
  commits/PRs/issues, merged PR and completed check runs → verified evidence, stale-task detection.
- REST API (33 routes, `docs/openapi.json`), bearer API keys (hash stored), 401 without a key.
- Discord command handlers (tested directly) and command tree (validated offline).
- Provider adapters: request shape and usage parsing tested with stubbed HTTP.

## Implemented, not verified against the live service

- Discord bot connection, slash-command sync, event projection loop (no bot token in the build
  environment). Command handlers and the command tree are tested offline.
- Discord OAuth2 login/callback (no client credentials).
- Real calls to OpenAI, Anthropic and Google (no API keys). Endpoint paths and payloads follow the
  public API docs; cost figures come from a static price table in `providers/base.py`.
- `docker compose up`: the compose file validates (`docker compose config`), but the image build
  could not pull `python:3.12-slim` (Docker Hub returned 429 to this environment).

## Not implemented (honest gaps)

- **Sandboxed execution for coding agents** (container per task, filesystem/network limits,
  git worktree per branch). Agents currently have no code-execution or file tools at all, which is
  the safe default but means "coding agents" can only produce text.
- Agent tool calling: `allowed_tools` is validated and stored but the runtime does not expose tools
  to models yet.
- Automatic agent consumption of the inbox (agents do not wake on events); delivery is pull via
  `/agents/{id}/inbox`.
- GitHub repository sync via the GitHub API (only webhooks); task-to-branch creation.
- Langfuse/OpenTelemetry export (the `executions` table is the trace store), retention jobs,
  trace export endpoint.
- MCP server exposing context/tools; A2A gateway.
- Vector retrieval (deterministic retrieval only).
- `/agent key` passes the key as a slash-command option (ephemeral reply, not echoed); a Discord
  modal or the REST endpoint is the better path for production.
- Per-user (as opposed to per-agent) budgets; rate limiting on the REST API.
- Load, concurrency and chaos tests beyond the concurrency limit check.

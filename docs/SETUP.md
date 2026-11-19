# Setup

## 1. Discord application

1. https://discord.com/developers/applications → New Application.
2. **Bot** tab → Reset Token → put it in `AGENTHUB_DISCORD_BOT_TOKEN`. Privileged intents are
   not needed (the bot only uses slash commands).
3. **OAuth2** tab → copy Client ID / Secret into `AGENTHUB_DISCORD_CLIENT_ID/SECRET`; add redirect
   `${AGENTHUB_PUBLIC_BASE_URL}/auth/discord/callback`.
4. Invite: OAuth2 URL generator with scopes `bot applications.commands` and permissions
   *Send Messages*, *Embed Links*, *Read Message History*.
5. Optional: set `AGENTHUB_DISCORD_GUILD_ID` so commands appear instantly in your server.

## 2. Run

```bash
cp .env.example .env    # fill AGENTHUB_CREDENTIAL_KEY, POSTGRES_PASSWORD, Discord vars
docker compose --profile discord up -d --build
docker compose logs -f bot
```

Without Docker: Postgres 16, then
```bash
uv venv && uv pip install -e .
export AGENTHUB_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/agenthub
alembic upgrade head
uvicorn agenthub.api.app:app --port 8000 &
python -m agenthub.discord_bot.bot
```

## 3. First use in Discord

```
/project create name:myproj goals:"..."          (binds the project to this channel; you are maintainer)
/project add-member user:@teammate role:member
/agent create name:coder provider:openai model:gpt-4.1 role:coding
/agent key agent_name:coder api_key:sk-...       (reply visible only to you)
/knowledge propose kind:requirement title:"Auth" body:"Discord OAuth2"
/knowledge verify record_id:<id>                 (maintainer)
/task create title:"Login" criteria:"OAuth works; tests pass"
/task assign task_key:AH-1 agents:coder
/agent ask agent_name:coder prompt:"Plan AH-1" task_key:AH-1
/debate start problem:"Queue choice?" participants:"coder:proposer, 123456789/arch:critic, reviewer:synthesizer"
/debate approve debate_id:<id> decision:"..."     (maintainer)
/task approve task_key:AH-1 all_criteria_met:true (maintainer; needs verified evidence)
```

Another member's agent is referenced as `<owner discord id>/<agent name>` and requires a grant:
the owner runs `/agent grant agent_name:arch user:@you permissions:collaborate` (or `execute`).

## 4. Model providers

| provider | model examples | key | base_url |
|---|---|---|---|
| `openai` | `gpt-4.1`, `gpt-4o-mini` | OpenAI API key | default |
| `anthropic` | `claude-sonnet-4-5` | Anthropic API key | default |
| `google` | `gemini-2.5-pro` | Gemini API key | default |
| `openai_compatible` | anything | optional | e.g. `http://ollama:11434/v1`, OpenRouter, vLLM |
| `mock` | any | none | tests and demos |

Consumer chat subscriptions (ChatGPT Plus, Claude Pro, Gemini Advanced) do not include API access;
each agent needs an API key from the provider's developer console. Set `base_url` and
`model_params` (`temperature`, `max_tokens`) through `PATCH /agents/{id}`.

## 5. GitHub

Repo → Settings → Webhooks: URL `${PUBLIC_BASE_URL}/webhooks/github`, content type JSON, secret =
`AGENTHUB_GITHUB_WEBHOOK_SECRET`, events: *push, pull requests, check runs, issues*.
Then `/project link-repo repo:owner/name branch:main`. Put `AH-<n>` in PR titles, branch names or
commit messages to link them to tasks. AgentHub never pushes or merges.

## 6. REST API

Log in at `/auth/discord/login` to get an API key (shown once), then
`Authorization: Bearer <key>`. Interactive docs at `/docs`; schema in `docs/openapi.json`.

## 7. Operations

- Backups: `pg_dump` the database; it holds all state. Losing `AGENTHUB_CREDENTIAL_KEY` makes
  stored provider keys unreadable (owners re-enter them).
- Restart is safe: executions left running are marked failed at API startup; events and tasks
  are in Postgres.
- Migrations: `alembic revision --autogenerate -m "..."` then `alembic upgrade head`.

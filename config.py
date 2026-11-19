"""Configuration, Local Project Workspace Mapping & SQLite state storage for Team Discord MCP."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ROSTER_PATH = BASE_DIR / "team_roster.json"
LOCAL_PROJECTS_PATH = BASE_DIR / "local_projects.json"
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = STATE_DIR / "team_chat.db"
PID_PATH = STATE_DIR / "daemon.pid"
WAKE_SIGNAL_PATH = STATE_DIR / "wake.signal"

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    bot_token: str
    channel_ids: List[str]
    default_channel_id: str
    watch_all_channels: bool
    webhook_url: str
    agent_name: str
    agent_role: str
    app_id: str
    is_router: bool
    use_gemini_decision: bool
    decision_threshold: float
    enable_windows_toast: bool
    max_ai_hops: int


def load_settings() -> Settings:
    load_dotenv(ENV_PATH, override=True)
    raw_channels = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    channel_ids = [c.strip() for c in raw_channels.replace(";", ",").split(",") if c.strip() and "PASTE" not in c]
    return Settings(
        bot_token=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
        channel_ids=channel_ids,
        default_channel_id=channel_ids[0] if channel_ids else "",
        watch_all_channels=_bool_env("DISCORD_WATCH_ALL_CHANNELS", True),
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        agent_name=os.environ.get("AI_AGENT_NAME", "AI-Agent").strip(),
        agent_role=os.environ.get("AI_AGENT_ROLE", "General AI assistant").strip(),
        app_id=os.environ.get("DISCORD_APP_ID", "").strip(),
        is_router=_bool_env("IS_ROUTER", False),
        use_gemini_decision=_bool_env("USE_GEMINI_DECISION", True),
        decision_threshold=float(os.environ.get("DECISION_THRESHOLD", "0.70")),
        enable_windows_toast=_bool_env("ENABLE_WINDOWS_TOAST", True),
        max_ai_hops=int(os.environ.get("MAX_AI_HOPS", "3")),
    )


def load_roster() -> List[Dict[str, Any]]:
    if not ROSTER_PATH.exists():
        s = load_settings()
        return [{"name": s.agent_name, "aliases": [s.agent_name.lower()], "role": s.agent_role, "discord_role_id": ""}]
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        agents = data.get("agents", [])
        if isinstance(agents, list) and agents:
            return agents
    except Exception:
        pass
    return []


def save_roster(agents: List[Dict[str, Any]]) -> None:
    ROSTER_PATH.write_text(
        json.dumps({"agents": agents}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_local_projects() -> Dict[str, Any]:
    if not LOCAL_PROJECTS_PATH.exists():
        return {"default_workspace": str(BASE_DIR.parent), "projects": []}
    try:
        data = json.loads(LOCAL_PROJECTS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("default_workspace", str(BASE_DIR.parent))
            data.setdefault("projects", [])
            return data
    except Exception:
        pass
    return {"default_workspace": str(BASE_DIR.parent), "projects": []}


def save_local_projects(cfg: Dict[str, Any]) -> None:
    LOCAL_PROJECTS_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_workspace(channel_id: str = "", text: str = "") -> Dict[str, Any]:
    """Resolve the local project workspace directory on this machine for a Discord message.

    Priority:
      1. Explicit project mention/alias in text (e.g. 'project:web', '#web', or matching alias keyword).
      2. Channel ID mapped in `local_projects.json` (`channel_ids`).
      3. `default_workspace` in `local_projects.json`.
    """
    cfg = load_local_projects()
    projects = cfg.get("projects") or []
    text_low = (text or "").lower()
    cid = str(channel_id or "").strip()

    # 1. Explicit project:<id> or #<id> syntax takes top priority
    explicit = re.search(r"(?:project:|duan:|#)\s*([a-z0-9_-]+)", text_low)
    if explicit:
        key = explicit.group(1)
        for p in projects:
            pid = str(p.get("id", "")).lower()
            aliases = [str(a).lower() for a in (p.get("aliases") or [])]
            if key == pid or key in aliases:
                return {
                    "project_id": p.get("id", ""),
                    "project_name": p.get("name", pid),
                    "workspace_path": p.get("path", ""),
                    "matched_by": f"explicit:{key}",
                }

    # 2. Keyword/alias match in text (prefer longer / more specific alias first)
    best_match = None
    best_len = 0
    for p in projects:
        pid = str(p.get("id", "")).lower()
        candidates = [pid] + [str(a).lower() for a in (p.get("aliases") or [])]
        for kw in candidates:
            if not kw or len(kw) < 2:
                continue
            if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text_low):
                if len(kw) > best_len:
                    best_len = len(kw)
                    best_match = (p, kw)
    if best_match:
        p, kw = best_match
        return {
            "project_id": p.get("id", ""),
            "project_name": p.get("name", p.get("id", "")),
            "workspace_path": p.get("path", ""),
            "matched_by": f"alias:{kw}",
        }

    # 3. Channel ID mapping
    if cid:
        for p in projects:
            ch_list = [str(x).strip() for x in (p.get("channel_ids") or [])]
            if cid in ch_list:
                return {
                    "project_id": p.get("id", ""),
                    "project_name": p.get("name", p.get("id", "")),
                    "workspace_path": p.get("path", ""),
                    "matched_by": f"channel:{cid}",
                }

    # 4. Default workspace
    default_ws = (cfg.get("default_workspace") or "").strip() or str(BASE_DIR.parent)
    return {
        "project_id": "default",
        "project_name": Path(default_ws).name or "default",
        "workspace_path": default_ws,
        "matched_by": "default",
    }


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            author_id TEXT NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            routed_to TEXT DEFAULT '',
            router_confidence REAL DEFAULT 0.0,
            urgency REAL DEFAULT 0.0,
            hop_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            channel_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT 'mention',
            urgency REAL NOT NULL DEFAULT 3.0,
            project_id TEXT NOT NULL DEFAULT '',
            workspace_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unread',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_status (
            agent_name TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'IDLE',
            note TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        """
    )
    # Safe auto-migration for existing DBs created before workspace columns existed
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(inbox)").fetchall()}
    if "project_id" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
    if "workspace_path" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


def set_agent_status(agent_name: str, status: str, note: str = "") -> Dict[str, Any]:
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_status(agent_name, status, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (agent_name, status.upper(), note, now),
        )
        conn.commit()
    return {"agent_name": agent_name, "status": status.upper(), "note": note, "updated_at": now}


def get_all_agent_statuses() -> Dict[str, Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT agent_name, status, note, updated_at FROM agent_status").fetchall()
    return {r["agent_name"]: dict(r) for r in rows}


def push_inbox_item(
    message_id: str,
    channel_id: str,
    sender: str,
    content: str,
    attachments: List[Dict[str, Any]],
    reason: str = "mention",
    urgency: float = 3.0,
    project_id: str = "",
    workspace_path: str = "",
) -> bool:
    now = time.time()
    if not workspace_path:
        ws = resolve_workspace(channel_id, content)
        project_id = project_id or ws["project_id"]
        workspace_path = ws["workspace_path"]
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO inbox
                (message_id, channel_id, sender, content, attachments_json, reason, urgency,
                 project_id, workspace_path, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)
            """,
            (
                message_id,
                channel_id,
                sender,
                content,
                json.dumps(attachments, ensure_ascii=False),
                reason,
                urgency,
                project_id,
                workspace_path,
                now,
            ),
        )
        conn.commit()
        inserted = cur.rowcount > 0
    if inserted:
        try:
            WAKE_SIGNAL_PATH.write_text(f"{now}:{message_id}\n", encoding="utf-8")
        except Exception:
            pass
    return inserted


def pop_unread_inbox(mark_read: bool = True, limit: int = 10) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, channel_id, sender, content, attachments_json,
                   reason, urgency, project_id, workspace_path, status, created_at
            FROM inbox
            WHERE status = 'unread'
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        ids = []
        for r in rows:
            d = dict(r)
            try:
                d["attachments"] = json.loads(d.pop("attachments_json", "[]"))
            except Exception:
                d["attachments"] = []
            if not d.get("workspace_path"):
                ws = resolve_workspace(d.get("channel_id", ""), d.get("content", ""))
                d["project_id"] = ws["project_id"]
                d["workspace_path"] = ws["workspace_path"]
            items.append(d)
            ids.append(r["id"])
        if mark_read and ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE inbox SET status = 'read' WHERE id IN ({placeholders})", ids)
            conn.commit()
    return items

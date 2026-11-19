"""Database models.

Vocabulary (kept deliberately distinct):
- Model: the underlying LLM, named by (provider, model) on an AgentDefinition.
- AgentDefinition (`agents`): identity, role, instructions, tools, limits, owner.
- AgentSession (`agent_sessions`): one running conversation of an agent (an "instance").
- Task (`tasks`): a unit of work assigned to an agent or agent group.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenthub.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class ProjectRole(str, enum.Enum):
    maintainer = "maintainer"
    member = "member"


class AgentStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    unavailable = "unavailable"


class KnowledgeStatus(str, enum.Enum):
    proposed = "proposed"
    verified = "verified"
    rejected = "rejected"
    superseded = "superseded"
    deprecated = "deprecated"


class KnowledgeKind(str, enum.Enum):
    requirement = "requirement"
    architecture = "architecture"
    constraint = "constraint"
    terminology = "terminology"
    fact = "fact"
    decision = "decision"
    note = "note"


class TaskState(str, enum.Enum):
    backlog = "backlog"
    assigned = "assigned"
    in_progress = "in_progress"
    review = "review"
    approved = "approved"
    completed = "completed"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    acked = "acked"
    failed = "failed"
    expired = "expired"


class DebateStatus(str, enum.Enum):
    running = "running"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    failed = "failed"


class ExecutionStatus(str, enum.Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timeout = "timeout"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    discord_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    goals: Mapped[str] = mapped_column(Text, default="")
    # Incremented whenever the verified knowledge set changes.
    context_version: Mapped[int] = mapped_column(Integer, default=1)
    github_repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    repo_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo_default_branch: Mapped[str] = mapped_column(String(100), default="main")
    discord_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[ProjectRole] = mapped_column(_enum(ProjectRole), default=ProjectRole.member)


class AgentDefinition(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(64), default="generalist")
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    model_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    system_instructions: Mapped[str] = mapped_column(Text, default="")
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[AgentStatus] = mapped_column(_enum(AgentStatus), default=AgentStatus.active)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    budget_tokens: Mapped[int] = mapped_column(Integer, default=200_000)
    budget_usd: Mapped[float] = mapped_column(Float, default=5.0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProviderCredential(Base):
    """One encrypted API key per agent. Never returned by any API."""

    __tablename__ = "provider_credentials"
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column()
    fingerprint: Mapped[str] = mapped_column(String(16))  # last chars of sha256, for display
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentGrant(Base):
    """Owner shares an agent with another user. Permissions: execute, collaborate."""

    __tablename__ = "agent_grants"
    __table_args__ = (UniqueConstraint("agent_id", "grantee_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    grantee_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentMemory(Base):
    """Agent-private knowledge (category C)."""

    __tablename__ = "agent_memory"
    __table_args__ = (UniqueConstraint("agent_id", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    started_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    debate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeRecord(Base):
    """Shared knowledge (categories A and B). One row per revision."""

    __tablename__ = "knowledge_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(64), default="default")
    kind: Mapped[KnowledgeKind] = mapped_column(_enum(KnowledgeKind))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[KnowledgeStatus] = mapped_column(
        _enum(KnowledgeStatus), default=KnowledgeStatus.proposed, index=True
    )
    # Logical record identity across revisions.
    lineage_id: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    author_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    source: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_task_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_debate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_in_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    short_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    objectives: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    human_owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[TaskState] = mapped_column(_enum(TaskState), default=TaskState.backlog)
    context_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repo_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deliverables: Mapped[list[str]] = mapped_column(JSON, default=list)
    open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    handoff_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskEvidence(Base):
    __tablename__ = "task_evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # test_result | pr | commit | document | review
    reference: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Evidence is "verified" when it came from a system we trust (GitHub webhook, CI) or
    # a human reviewer; an agent's own claim is stored but unverified.
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_by_agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Structured envelope for agent-to-agent and coordination messages."""

    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    debate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sender_agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sender_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipient_agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    message_type: Mapped[str] = mapped_column(String(48))
    context_version: Mapped[int] = mapped_column(Integer)
    parent_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hop: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    projected_to_discord: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "recipient_agent_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"))
    recipient_agent_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus), default=DeliveryStatus.pending, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Debate(Base):
    __tablename__ = "debates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    problem: Mapped[str] = mapped_column(Text)
    # [{"agent_id":..., "role": "proposer|critic|alternative|verifier|reviewer|synthesizer"}]
    participants: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    max_rounds: Mapped[int] = mapped_column(Integer, default=2)
    budget_tokens: Mapped[int] = mapped_column(Integer, default=50_000)
    budget_usd: Mapped[float] = mapped_column(Float, default=1.0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_usd: Mapped[float] = mapped_column(Float, default=0.0)
    context_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[DebateStatus] = mapped_column(_enum(DebateStatus), default=DebateStatus.running)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decision_record_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DebateTurn(Base):
    __tablename__ = "debate_turns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id", ondelete="CASCADE"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # proposal | critique | synthesis
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Execution(Base):
    """Execution trace (category D)."""

    __tablename__ = "executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    requested_by: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    debate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    context_version: Mapped[int] = mapped_column(Integer)
    context_record_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt_redacted: Mapped[str] = mapped_column(Text, default="")
    response_redacted: Mapped[str] = mapped_column(Text, default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[ExecutionStatus] = mapped_column(
        _enum(ExecutionStatus), default=ExecutionStatus.running
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GithubLink(Base):
    __tablename__ = "github_links"
    __table_args__ = (UniqueConstraint("project_id", "kind", "ref"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # pr | commit | issue | branch
    ref: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(400), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    allowed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Counter(Base):
    """Monotonic counters (e.g. task short IDs)."""

    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)

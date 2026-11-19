"""Task-specific context packages and drift detection.

Retrieval is deterministic (project, status, kind, task links) — no embeddings. Agent-private
memory is added only for the agent being executed, never for others.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.models import (
    Execution,
    ExecutionStatus,
    KnowledgeKind,
    KnowledgeRecord,
    KnowledgeStatus,
    Project,
    Task,
)
from agenthub.services import knowledge

CORE_KINDS = [
    KnowledgeKind.requirement, KnowledgeKind.architecture, KnowledgeKind.constraint,
    KnowledgeKind.decision, KnowledgeKind.terminology, KnowledgeKind.fact,
]
MAX_RECORDS = 40


@dataclass
class ContextPackage:
    project_id: str
    project_name: str
    goals: str
    context_version: int
    repo: str | None
    repo_head_sha: str | None
    verified: list[dict[str, Any]] = field(default_factory=list)
    task: dict[str, Any] | None = None
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    prior_failures: list[dict[str, Any]] = field(default_factory=list)
    proposed_for_task: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def record_ids(self) -> list[str]:
        return [r["id"] for r in self.verified]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        lines = [
            "=== PROJECT CONTEXT (shared, versioned) ===",
            f"project: {self.project_name}",
            f"context_version: {self.context_version}",
        ]
        if self.repo:
            lines.append(f"repository: {self.repo} @ {self.repo_head_sha or 'unknown'}")
        if self.goals:
            lines.append(f"goals: {self.goals}")
        lines.append("\n-- Verified knowledge (authoritative; cite by [id]) --")
        for r in self.verified or []:
            lines.append(f"[{r['id']}] ({r['kind']}, rev {r['revision']}) {r['title']}: {r['body']}")
        if not self.verified:
            lines.append("(none yet)")
        if self.task:
            t = self.task
            lines += [
                "\n-- Current task --",
                f"AH-{t['short_id']} {t['title']} (state: {t['state']})",
                f"objectives: {t['objectives']}",
                "acceptance criteria: " + "; ".join(t["acceptance_criteria"] or ["(none)"]),
            ]
        if self.dependencies:
            lines.append("\n-- Dependencies --")
            lines += [f"AH-{d['short_id']} {d['title']}: {d['state']}" for d in self.dependencies]
        if self.prior_failures:
            lines.append("\n-- Previous failed attempts --")
            lines += [f"{f['created_at']}: {f['error']}" for f in self.prior_failures]
        if self.proposed_for_task:
            lines.append("\n-- Proposed, NOT verified (treat as hypotheses) --")
            lines += [f"[{r['id']}] {r['title']}: {r['body']}" for r in self.proposed_for_task]
        if self.open_questions:
            lines.append("\n-- Open questions --")
            lines += [f"- {q}" for q in self.open_questions]
        if self.warnings:
            lines.append("\n-- Warnings --")
            lines += [f"! {w}" for w in self.warnings]
        lines.append("=== END PROJECT CONTEXT ===")
        return "\n".join(lines)


def _brief(r: KnowledgeRecord) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind.value, "revision": r.revision, "title": r.title,
            "body": r.body[:1500], "tags": r.tags}


async def build_package(db: AsyncSession, project_id: str, task_id: str | None = None) -> ContextPackage:
    project = await db.get(Project, project_id)
    assert project is not None
    verified = list(
        await db.scalars(
            select(KnowledgeRecord)
            .where(KnowledgeRecord.project_id == project_id,
                   KnowledgeRecord.status == KnowledgeStatus.verified,
                   KnowledgeRecord.kind.in_(CORE_KINDS))
            .order_by(KnowledgeRecord.created_at.desc())
            .limit(MAX_RECORDS)
        )
    )
    pkg = ContextPackage(
        project_id=project.id, project_name=project.name, goals=project.goals,
        context_version=project.context_version, repo=project.github_repo,
        repo_head_sha=project.repo_head_sha, verified=[_brief(r) for r in verified],
    )
    if task_id:
        task = await db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise ValueError("task not in project")
        pkg.task = {"id": task.id, "short_id": task.short_id, "title": task.title,
                    "objectives": task.objectives, "acceptance_criteria": task.acceptance_criteria,
                    "state": task.state.value}
        pkg.open_questions = list(task.open_questions or [])
        for dep_id in task.depends_on or []:
            dep = await db.get(Task, dep_id)
            if dep:
                pkg.dependencies.append({"short_id": dep.short_id, "title": dep.title,
                                         "state": dep.state.value})
        fails = await db.scalars(
            select(Execution)
            .where(Execution.task_id == task_id,
                   Execution.status.in_([ExecutionStatus.failed, ExecutionStatus.timeout]))
            .order_by(Execution.created_at.desc()).limit(5)
        )
        pkg.prior_failures = [{"created_at": f.created_at.isoformat(), "error": (f.error or "")[:300]}
                              for f in fails]
        proposed = await knowledge._list_internal(db, project_id, status="proposed", limit=200)
        pkg.proposed_for_task = [_brief(r) for r in proposed if task_id in (r.related_task_ids or [])]
        if task.context_version and task.context_version < project.context_version:
            pkg.warnings.append(
                f"task was assigned at context_version {task.context_version}; "
                f"current is {project.context_version}. Re-check decisions marked newer."
            )
        if task.repo_sha and project.repo_head_sha and task.repo_sha != project.repo_head_sha:
            pkg.warnings.append(
                f"task was based on commit {task.repo_sha[:10]}; default branch is now "
                f"{project.repo_head_sha[:10]}"
            )
    return pkg


@dataclass
class DriftReport:
    stale: bool
    referenced_version: int | None
    current_version: int
    outdated_records: list[dict[str, Any]] = field(default_factory=list)
    newer_verified: list[dict[str, Any]] = field(default_factory=list)
    unknown_records: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def check_drift(
    db: AsyncSession, project_id: str, referenced_version: int | None,
    referenced_record_ids: list[str] | None = None,
) -> DriftReport:
    project = await db.get(Project, project_id)
    assert project is not None
    report = DriftReport(stale=False, referenced_version=referenced_version,
                         current_version=project.context_version)
    for rid in referenced_record_ids or []:
        r = await db.get(KnowledgeRecord, rid)
        if r is None or r.project_id != project_id:
            report.unknown_records.append(rid)
            continue
        if r.status in (KnowledgeStatus.superseded, KnowledgeStatus.deprecated, KnowledgeStatus.rejected):
            cur = await knowledge.current_of(db, r)
            report.outdated_records.append({
                "id": r.id, "title": r.title, "status": r.status.value,
                "replacement": _brief(cur) if cur else None,
            })
    if referenced_version is not None and referenced_version < project.context_version:
        newer = await db.scalars(
            select(KnowledgeRecord).where(
                KnowledgeRecord.project_id == project_id,
                KnowledgeRecord.status == KnowledgeStatus.verified,
                KnowledgeRecord.verified_in_version > referenced_version,
            )
        )
        report.newer_verified = [_brief(r) for r in newer]
    report.stale = bool(report.outdated_records or report.unknown_records or report.newer_verified)
    return report

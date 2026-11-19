"""GitHub webhook ingestion (read-only: AgentHub never pushes or merges).

Linking convention: a PR, branch, or commit message containing `AH-<n>` belongs to task n.
Trusted evidence: merged PRs and completed check runs, because they arrive with a valid HMAC
signature from GitHub rather than from an agent.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.models import GithubLink, Project, Task, utcnow
from agenthub.services import bus, tasks

TASK_REF = re.compile(r"\bAH-(\d+)\b", re.IGNORECASE)


async def _project_for(db: AsyncSession, payload: dict[str, Any]) -> Project | None:
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None
    return await db.scalar(select(Project).where(Project.github_repo == repo))


async def _tasks_in(db: AsyncSession, project_id: str, *texts: str | None) -> list[Task]:
    nums = {int(n) for t in texts if t for n in TASK_REF.findall(t)}
    out = []
    for n in sorted(nums):
        t = await db.scalar(select(Task).where(Task.short_id == n, Task.project_id == project_id))
        if t:
            out.append(t)
    return out


async def _link(db: AsyncSession, project_id: str, task_id: str | None, kind: str, ref: str,
                url: str, state: str, data: dict[str, Any]) -> None:
    row = await db.scalar(select(GithubLink).where(
        GithubLink.project_id == project_id, GithubLink.kind == kind, GithubLink.ref == ref))
    if row is None:
        row = GithubLink(project_id=project_id, kind=kind, ref=ref)
        db.add(row)
    row.task_id, row.url, row.state, row.data, row.updated_at = task_id, url, state, data, utcnow()
    await db.flush()


async def handle(db: AsyncSession, event: str, delivery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = await _project_for(db, payload)
    if project is None:
        return {"ignored": "repository not linked to a project"}
    result: dict[str, Any] = {"project": project.name, "event": event, "linked_tasks": []}

    if event == "push":
        ref = payload.get("ref", "")
        if ref == f"refs/heads/{project.repo_default_branch}" and payload.get("after"):
            project.repo_head_sha = payload["after"]
            await bus.publish(db, project_id=project.id, message_type="repo.updated",
                              idempotency_key=f"gh:{delivery_id}",
                              payload={"sha": payload["after"], "ref": ref})
            result["head"] = payload["after"]
        for c in payload.get("commits") or []:
            for t in await _tasks_in(db, project.id, c.get("message")):
                await _link(db, project.id, t.id, "commit", c["id"], c.get("url", ""), "pushed",
                            {"message": (c.get("message") or "")[:300]})
                result["linked_tasks"].append(f"AH-{t.short_id}")

    elif event == "pull_request":
        pr = payload.get("pull_request") or {}
        number = str(pr.get("number"))
        state = "merged" if pr.get("merged") else pr.get("state", "")
        for t in await _tasks_in(db, project.id, pr.get("title"), pr.get("body"),
                                 (pr.get("head") or {}).get("ref")):
            await _link(db, project.id, t.id, "pr", number, pr.get("html_url", ""), state,
                        {"title": pr.get("title"), "head_sha": (pr.get("head") or {}).get("sha"),
                         "base": (pr.get("base") or {}).get("ref")})
            if payload.get("action") == "closed" and pr.get("merged"):
                await tasks.add_system_evidence(db, t.id, "pr", pr.get("html_url", f"#{number}"), True)
            result["linked_tasks"].append(f"AH-{t.short_id}")

    elif event == "check_run":
        run = payload.get("check_run") or {}
        if payload.get("action") == "completed":
            head = run.get("head_sha")
            links = await db.scalars(select(GithubLink).where(
                GithubLink.project_id == project.id, GithubLink.kind == "pr"))
            for link in links:
                if link.task_id and (link.data or {}).get("head_sha") == head:
                    passed = run.get("conclusion") == "success"
                    await tasks.add_system_evidence(
                        db, link.task_id, "test_result",
                        f"{run.get('name')}: {run.get('conclusion')} {run.get('html_url', '')}", passed)
                    t = await db.get(Task, link.task_id)
                    if t:
                        result["linked_tasks"].append(f"AH-{t.short_id}")

    elif event == "issues":
        issue = payload.get("issue") or {}
        for t in await _tasks_in(db, project.id, issue.get("title"), issue.get("body")):
            await _link(db, project.id, t.id, "issue", str(issue.get("number")),
                        issue.get("html_url", ""), issue.get("state", ""), {"title": issue.get("title")})
            result["linked_tasks"].append(f"AH-{t.short_id}")
    else:
        result["ignored"] = "event type not handled"
    await db.flush()
    return result

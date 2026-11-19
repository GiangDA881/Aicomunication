"""System-1 Task Router powered by Google `gemini-decision-1` (0 output tokens, ~200ms).

Evaluates untagged user messages (plus optional image/audio/video/PDF attachments)
against `team_roster.json` and live agent statuses (`IDLE`/`BUSY`) in ONE single API call:
  1. `is_actionable_task` (Noul): Is this a work request/question for an AI agent vs casual human chat?
  2. `assignee` (Choice): Which agent in the roster is the best owner (or NONE / AMBIGUOUS)?
  3. `urgency` (Score 1..5): Priority level for notifications & Windows Toast escalation.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

_JEV_VISION_DIR = Path.home() / ".gemini" / "config" / "skills" / "jev-vision"
if _JEV_VISION_DIR.exists():
    sys.path.insert(0, str(_JEV_VISION_DIR))

from config import Settings, get_all_agent_statuses, load_roster

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
SUPPORTED_DOC_EXTS = {".pdf"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB cap for fast System-1 routing


class TaskRouter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._gdec = None

    def _get_client(self):
        if self._gdec is None:
            from gemini_decision_client import GeminiDecisionClient
            self._gdec = GeminiDecisionClient(fallback_to_jev=False)
        return self._gdec

    def _download_temp_attachment(self, attachments: List[Dict[str, Any]]) -> Dict[str, Optional[Path]]:
        """Download at most one supported multimodal attachment (<= 10MB) for native decision routing."""
        media: Dict[str, Optional[Path]] = {"image": None, "video": None, "audio": None, "document": None}
        for att in attachments or []:
            url = att.get("url", "")
            fname = (att.get("filename") or "").lower()
            size = int(att.get("size") or 0)
            if not url or (size and size > MAX_ATTACHMENT_BYTES):
                continue
            ext = Path(fname).suffix.lower()
            slot = None
            if ext in SUPPORTED_IMAGE_EXTS:
                slot = "image"
            elif ext in SUPPORTED_VIDEO_EXTS:
                slot = "video"
            elif ext in SUPPORTED_AUDIO_EXTS:
                slot = "audio"
            elif ext in SUPPORTED_DOC_EXTS:
                slot = "document"
            if not slot:
                continue
            try:
                resp = httpx.get(url, timeout=15.0, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) <= MAX_ATTACHMENT_BYTES:
                    tmp = Path(tempfile.gettempdir()) / f"tchat_route_{int(time.time()*1000)}{ext}"
                    tmp.write_bytes(resp.content)
                    media[slot] = tmp
                    break
            except Exception:
                continue
        return media

    def route(
        self,
        sender: str,
        content: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        local_attachment_path: Optional[str] = None,
        recent_context: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from gemini_decision_client import Choice, Noul, Score

        roster = load_roster()
        statuses = get_all_agent_statuses()

        # Build roster description + choice criteria
        roster_lines = []
        choice_criteria: Dict[str, str] = {}
        for ag in roster:
            name = ag["name"]
            role = ag.get("role", "")
            st = statuses.get(name, {})
            st_str = st.get("status", "IDLE")
            note = st.get("note", "")
            status_desc = f"{st_str} ({note})" if note else st_str
            roster_lines.append(f"- {name} [Trạng thái: {status_desc}]: {role}")
            choice_criteria[name] = f"Assign to {name} — {role} (Current status: {status_desc})"

        choice_criteria["NONE"] = "Not a task for any AI agent (casual human banter, greeting, or off-topic)."
        choice_criteria["AMBIGUOUS"] = "Actionable task, but genuinely unclear which agent should own it."

        ctx_lines = []
        for m in (recent_context or [])[-5:]:
            ctx_lines.append(f"  {m.get('author', 'user')}: {m.get('content', '')[:200]}")
        ctx_block = "\n".join(ctx_lines) if ctx_lines else "  (no prior messages)"

        att_names = [a.get("filename", "") for a in (attachments or []) if a.get("filename")]
        if local_attachment_path:
            att_names.append(Path(local_attachment_path).name)
        att_block = ", ".join(att_names) if att_names else "none"

        prompt = (
            "You are the central System-1 Task Router for a team Discord chat where humans collaborate with specialized AI agents.\n\n"
            "TEAM AI ROSTER:\n"
            + "\n".join(roster_lines)
            + "\n\nRECENT CHAT CONTEXT:\n"
            + ctx_block
            + f"\n\nNEW MESSAGE FROM '{sender}' (attachments: {att_block}):\n"
            + f"\"\"\"\n{content}\n\"\"\"\n\n"
            "Rules:\n"
            "- Prefer IDLE agents over BUSY agents only when roles overlap.\n"
            "- If the message is just casual greeting/banter (e.g. 'haha', 'ok', 'ăn cơm chưa'), is_actionable_task should be false and assignee should be NONE.\n"
            "- If the message asks for work, debugging, review, creation, or information belonging to a specific role, assign it cleanly."
        )

        # Prepare optional multimodal input
        media: Dict[str, Optional[Path]] = {"image": None, "video": None, "audio": None, "document": None}
        cleanup_paths: List[Path] = []
        if local_attachment_path:
            lp = Path(local_attachment_path)
            if lp.exists():
                ext = lp.suffix.lower()
                if ext in SUPPORTED_IMAGE_EXTS:
                    media["image"] = lp
                elif ext in SUPPORTED_VIDEO_EXTS:
                    media["video"] = lp
                elif ext in SUPPORTED_AUDIO_EXTS:
                    media["audio"] = lp
                elif ext in SUPPORTED_DOC_EXTS:
                    media["document"] = lp
        elif attachments:
            media = self._download_temp_attachment(attachments)
            cleanup_paths = [p for p in media.values() if p is not None]

        t0 = time.perf_counter()
        try:
            gdec = self._get_client()
            res = gdec.ask(
                prompt=prompt,
                image=media["image"],
                video=media["video"],
                audio=media["audio"],
                document=media["document"],
                questions={
                    "is_actionable_task": Noul(
                        instructions="Is the new message an actionable request, question, or work item that one of the team AI agents should handle (NOT casual human-to-human banter or a mere acknowledgment like 'ok'/'thanks')?"
                    ),
                    "assignee": Choice(
                        instructions="Which AI agent from the roster is the best single owner for this message? Choose NONE if no AI action is needed, or AMBIGUOUS if unclear.",
                        criteria=choice_criteria,
                    ),
                    "urgency": Score(
                        instructions="How urgent is this message for the team (1 = casual/FYI, 3 = normal work task, 5 = critical blocker/error needing immediate human+AI attention)?",
                        criteria=[
                            "1: Casual / FYI",
                            "2: Low priority / whenever free",
                            "3: Normal team work task",
                            "4: High priority / deadline soon",
                            "5: Critical error / urgent blocker",
                        ],
                    ),
                },
                include_confidence=True,
            )
        finally:
            for cp in cleanup_paths:
                try:
                    cp.unlink(missing_ok=True)
                except Exception:
                    pass

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        is_task_prob = float(res["is_actionable_task"]["noul"])
        assignee_info = res["assignee"]
        assignee = str(assignee_info["choice"])
        assignee_conf = float(assignee_info["confidence"])
        probs = assignee_info.get("probabilities") or {}
        # Score returns 0..4 index expectation; map to 1..5
        raw_urgency = float(res["urgency"]["score"])
        urgency_1_to_5 = round(min(5.0, max(1.0, raw_urgency + 1.0)), 2)

        # Rank top real agent candidates for clarification if ambiguous
        agent_names = {ag["name"] for ag in roster}
        ranked = sorted(
            [(k, round(float(v), 4)) for k, v in probs.items() if k in agent_names],
            key=lambda x: x[1],
            reverse=True,
        )

        # Final decision verdict
        threshold = self.settings.decision_threshold
        if is_task_prob < 0.50 or assignee == "NONE":
            decision = "IGNORE"
        elif assignee in agent_names and assignee_conf >= threshold:
            decision = "ASSIGN"
        elif ranked and ranked[0][1] >= 0.35:
            decision = "CLARIFY"
        else:
            decision = "IGNORE"

        return {
            "decision": decision,  # ASSIGN | CLARIFY | IGNORE
            "assignee": assignee,
            "confidence": round(assignee_conf, 4),
            "is_task_prob": round(is_task_prob, 4),
            "urgency": urgency_1_to_5,
            "top_candidates": ranked[:3],
            "elapsed_ms": elapsed_ms,
        }

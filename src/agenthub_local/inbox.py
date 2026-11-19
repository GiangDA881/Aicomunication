"""Local inbox file shared by the daemon (writer) and the MCP server (reader)."""

from __future__ import annotations

import json
import time
from pathlib import Path


class Inbox:
    def __init__(self, home: Path) -> None:
        self.path = home / "inbox.jsonl"
        self.cursor = home / "inbox.cursor"
        home.mkdir(parents=True, exist_ok=True)

    def append(self, item: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def last_seen(self) -> str | None:
        if not self.cursor.exists():
            return None
        return self.cursor.read_text().strip() or None

    def mark(self, message_id: str) -> None:
        cur = self.last_seen()
        if cur is None or int(message_id) > int(cur):
            self.cursor.write_text(message_id)

    def unread(self, mark: bool = True) -> list[dict]:
        seen = self.last_seen()
        items = [i for i in self._all() if seen is None or int(i["id"]) > int(seen)]
        if mark and items:
            self.mark(max(items, key=lambda i: int(i["id"]))["id"])
        return items

    def wait(self, timeout: float, poll: float = 1.0) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            items = self.unread(mark=False)
            if items:
                return self.unread(mark=True)
            time.sleep(poll)
        return []

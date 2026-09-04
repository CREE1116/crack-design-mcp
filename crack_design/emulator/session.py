"""Session persistence. One JSON file per session under the store root."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Session

DEFAULT_STORE = Path(".crack-emu/sessions")


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or DEFAULT_STORE).expanduser()

    def path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).exists()

    def load(self, session_id: str) -> Session:
        return Session.from_dict(json.loads(self.path(session_id).read_text(encoding="utf-8")))

    def save(self, session: Session) -> Path:
        p = self.path(session.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    @staticmethod
    def _same_project(recorded: str, wanted: Path) -> bool:
        if not recorded:
            return False
        try:
            return Path(recorded).expanduser().resolve() == wanted
        except OSError:
            return False

    def entries(self, project_root: str | Path | None = None) -> list[dict]:
        """Session summaries, optionally narrowed to one project.

        Sessions from every project share one flat store, and each file already
        records the project it belongs to, so switching projects has to filter
        on that — otherwise the convenience-store sessions stay on screen while
        you are looking at battlemage.

        A session whose file predates that field, or that no longer parses, is
        reported as unscoped rather than silently dropped: it belongs to some
        project, we just cannot tell which.
        """
        if not self.root.exists():
            return []
        wanted = (Path(project_root).expanduser().resolve()
                  if project_root is not None else None)
        out: list[dict] = []
        for f in sorted(self.root.glob("*.json")):
            recorded, turns, variant, readable = "", 0, "", True
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                recorded = d.get("project_root", "") or ""
                turns = len(d.get("turns", []))
                variant = d.get("variant", "") or ""
            except Exception:
                readable = False
            if wanted is not None and not self._same_project(recorded, wanted):
                continue
            out.append({"id": f.stem, "project_root": recorded, "turns": turns,
                        "variant": variant, "unscoped": not recorded or not readable})
        return out

    def list(self, project_root: str | Path | None = None) -> list[str]:
        return [e["id"] for e in self.entries(project_root)]

    def orphans(self, project_root: str | Path) -> list[str]:
        """Sessions in the store that no filter would ever show for this project."""
        wanted = Path(project_root).expanduser().resolve()
        return [e["id"] for e in self.entries()
                if e["unscoped"] or not self._same_project(e["project_root"], wanted)]

    def delete(self, session_id: str) -> bool:
        p = self.path(session_id)
        if p.exists():
            p.unlink()
            return True
        return False

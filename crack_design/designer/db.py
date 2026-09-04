"""SQLite Database Layer for Crack Story Chat projects.

Acts as the single source of truth for story projects, providing atomic transactions,
instant reloads without markdown regex parsing fragility, and seamless bi-directional
conversion with markdown files and ZIP archiving.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from ..config import workspace_root, exports_dir, state_root


DEFAULT_DB_PATH = state_root() / "crack.db"


class CrackDatabase:
    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path or DEFAULT_DB_PATH).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                short_summary TEXT DEFAULT '',
                premise TEXT DEFAULT '',
                player_role TEXT DEFAULT '',
                active_variant TEXT DEFAULT 'safe',
                source_path TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                main_prompt TEXT DEFAULT '',
                prologue TEXT DEFAULT '',
                opening_situation TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, variant)
            );

            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                title TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                content TEXT NOT NULL,
                char_count INTEGER DEFAULT 0,
                order_index INTEGER DEFAULT 0,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, variant, title)
            );

            CREATE TABLE IF NOT EXISTS start_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                set_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                prologue TEXT DEFAULT '',
                opening_situation TEXT DEFAULT '',
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, set_id)
            );

            CREATE TABLE IF NOT EXISTS shortcuts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                shortcut_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                prompt TEXT DEFAULT '',
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, variant, shortcut_id)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, filename)
            );
            """)

    # ─────────────────────────────────────────────────────────────
    # Project CRUD
    # ─────────────────────────────────────────────────────────────
    def get_project(self, name_or_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM projects WHERE id = ? OR name = ?",
                (name_or_id, name_or_id)
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC")
            return [dict(r) for r in cur.fetchall()]

    def upsert_project(self, name: str, title: str, short_summary: str = "",
                       premise: str = "", player_role: str = "",
                       active_variant: str = "safe", source_path: str = "") -> str:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT id FROM projects WHERE name = ?", (name,))
            row = cur.fetchone()
            if row:
                project_id = row["id"]
            else:
                clean = re.sub(r'[^a-zA-Z0-9_\-]', '', name).strip('_').lower()
                project_id = clean if clean else ("proj_" + hashlib.sha256(name.encode('utf-8')).hexdigest()[:10])

            conn.execute("""
            INSERT INTO projects (id, name, title, short_summary, premise, player_role, active_variant, source_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                title = excluded.title,
                short_summary = excluded.short_summary,
                premise = excluded.premise,
                player_role = excluded.player_role,
                active_variant = excluded.active_variant,
                source_path = excluded.source_path,
                updated_at = CURRENT_TIMESTAMP
            """, (project_id, name, title, short_summary, premise, player_role, active_variant, source_path))
        return project_id

    # ─────────────────────────────────────────────────────────────
    # Prompts
    # ─────────────────────────────────────────────────────────────
    def set_prompt(self, project_id: str, variant: str, main_prompt: str | None = None,
                   prologue: str | None = None, opening_situation: str | None = None) -> None:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM prompts WHERE project_id = ? AND variant = ?",
                (project_id, variant)
            )
            row = cur.fetchone()
            if row:
                mp = main_prompt if main_prompt is not None else row["main_prompt"]
                pl = prologue if prologue is not None else row["prologue"]
                os_text = opening_situation if opening_situation is not None else row["opening_situation"]
                conn.execute("""
                UPDATE prompts SET main_prompt = ?, prologue = ?, opening_situation = ?, updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ? AND variant = ?
                """, (mp, pl, os_text, project_id, variant))
            else:
                conn.execute("""
                INSERT INTO prompts (project_id, variant, main_prompt, prologue, opening_situation, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (project_id, variant, main_prompt or "", prologue or "", opening_situation or ""))

    def get_prompt(self, project_id: str, variant: str) -> dict[str, str]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM prompts WHERE project_id = ? AND variant = ?",
                (project_id, variant)
            )
            row = cur.fetchone()
            if not row:
                return {"main_prompt": "", "prologue": "", "opening_situation": ""}
            return {
                "main_prompt": row["main_prompt"] or "",
                "prologue": row["prologue"] or "",
                "opening_situation": row["opening_situation"] or "",
            }

    # ─────────────────────────────────────────────────────────────
    # Keywords
    # ─────────────────────────────────────────────────────────────
    def upsert_keyword(self, project_id: str, variant: str, title: str,
                       keywords: list[str], content: str, order_index: int = 0) -> None:
        kw_json = json.dumps(keywords, ensure_ascii=False)
        char_count = len(content)
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO keywords (project_id, variant, title, keywords_json, content, char_count, order_index)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, variant, title) DO UPDATE SET
                keywords_json = excluded.keywords_json,
                content = excluded.content,
                char_count = excluded.char_count,
                order_index = excluded.order_index
            """, (project_id, variant, title, kw_json, content, char_count, order_index))

    def list_keywords(self, project_id: str, variant: str) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM keywords WHERE project_id = ? AND variant = ? ORDER BY order_index ASC, id ASC",
                (project_id, variant)
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["keywords"] = json.loads(d["keywords_json"])
                out.append(d)
            return out

    def delete_keyword(self, project_id: str, variant: str, title: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM keywords WHERE project_id = ? AND variant = ? AND title = ?",
                (project_id, variant, title)
            )
            return cur.rowcount > 0

    # ─────────────────────────────────────────────────────────────
    # Start Sets
    # ─────────────────────────────────────────────────────────────
    def upsert_start_set(self, project_id: str, set_id: str, title: str,
                         description: str, prologue: str, opening_situation: str) -> None:
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO start_sets (project_id, set_id, title, description, prologue, opening_situation)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, set_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                prologue = excluded.prologue,
                opening_situation = excluded.opening_situation
            """, (project_id, set_id, title, description, prologue, opening_situation))

    def list_start_sets(self, project_id: str) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM start_sets WHERE project_id = ? ORDER BY set_id ASC",
                (project_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    # ─────────────────────────────────────────────────────────────
    # Shortcuts
    # ─────────────────────────────────────────────────────────────
    def upsert_shortcut(self, project_id: str, variant: str, shortcut_id: str,
                        name: str, description: str, prompt: str) -> None:
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO shortcuts (project_id, variant, shortcut_id, name, description, prompt)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, variant, shortcut_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                prompt = excluded.prompt
            """, (project_id, variant, shortcut_id, name, description, prompt))

    def list_shortcuts(self, project_id: str, variant: str) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM shortcuts WHERE project_id = ? AND variant = ? ORDER BY shortcut_id ASC",
                (project_id, variant)
            )
            return [dict(r) for r in cur.fetchall()]

    # ─────────────────────────────────────────────────────────────
    # Import Markdown Directory into DB
    # ─────────────────────────────────────────────────────────────
    def import_from_markdown(self, project_dir: Path | str) -> dict[str, Any]:
        p = Path(project_dir).resolve()
        if not p.is_dir():
            raise ValueError(f"Directory '{p}' not found")

        root = p.parent if p.name == "build" else p
        build_dir = root / "build" if (root / "build").is_dir() else root

        # 1. Title & Premise from story.md
        story_md = root / "story.md"
        title = root.name
        short_summary = ""
        premise = ""
        player_role = ""

        if story_md.exists():
            text = story_md.read_text(encoding="utf-8")
            tm = re.search(r"^-\s*Title:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if tm:
                title = tm.group(1).strip()
            sm = re.search(r"^-\s*Short Summary:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if sm:
                short_summary = sm.group(1).strip()
            pm = re.search(r"^-\s*Premise:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if pm:
                premise = pm.group(1).strip()
            prm = re.search(r"^-\s*Player Role:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if prm:
                player_role = prm.group(1).strip()

        # Check description files
        desc_file = build_dir / "story-description.md"
        if desc_file.exists() and not short_summary:
            short_summary = desc_file.read_text(encoding="utf-8").strip()

        project_id = self.upsert_project(
            name=root.name,
            title=title,
            short_summary=short_summary,
            premise=premise,
            player_role=player_role,
            source_path=str(root)
        )

        # 2. Prompts
        for v in ("safe", "unsafe"):
            mp_file = build_dir / f"integrated-prompt-{v}.md"
            main_p = mp_file.read_text(encoding="utf-8") if mp_file.exists() else ""
            pl_file = build_dir / "prologue.md"
            pl_text = pl_file.read_text(encoding="utf-8") if pl_file.exists() else ""
            os_file = build_dir / "opening-situation.md"
            os_text = os_file.read_text(encoding="utf-8") if os_file.exists() else ""
            if main_p or pl_text or os_text:
                self.set_prompt(project_id, v, main_prompt=main_p, prologue=pl_text, opening_situation=os_text)

        # 3. Keywords from build/keyword-book.md or build/keyword-book-{variant}.md
        imported_kw_count = 0
        for v in ("safe", "unsafe"):
            kw_files = [build_dir / f"keyword-book-{v}.md", build_dir / "keyword-book.md"]
            chosen = next((f for f in kw_files if f.exists()), None)
            if chosen:
                raw = chosen.read_text(encoding="utf-8")
                # Split entries by ## Header
                sections = re.split(r"^##\s+", raw, flags=re.MULTILINE)
                idx = 0
                for sec in sections[1:]:
                    lines = sec.strip().splitlines()
                    if not lines:
                        continue
                    sec_title = lines[0].strip()
                    # Skip shortcuts block
                    if sec_title.lower() in ("shortcuts", "shortcut") or sec_title.startswith("shortcut_"):
                        continue
                    # Parse keywords
                    kw_list: list[str] = []
                    content_lines: list[str] = []
                    is_content = False
                    for ln in lines[1:]:
                        if not is_content:
                            m_kw = re.match(r"^-\s*키워드:\s*(.+)$", ln.strip())
                            if m_kw:
                                kw_list = [k.strip() for k in m_kw.group(1).split(",") if k.strip()]
                                continue
                            if re.match(r"^-\s*내용:\s*$", ln.strip()):
                                is_content = True
                                continue
                        content_lines.append(ln)

                    body = "\n".join(content_lines).strip()
                    if sec_title and (kw_list or body):
                        self.upsert_keyword(project_id, v, sec_title, kw_list, body, order_index=idx)
                        idx += 1
                        imported_kw_count += 1

                # Parse shortcuts from keyword-book
                sc_pattern = re.compile(
                    r"^##\s+(shortcut_[a-zA-Z0-9_\-]+)\s*\n"
                    r"(?:-\s*name:\s*(.+)\n)?"
                    r"(?:-\s*description:\s*(.+)\n)?"
                    r"(?:-\s*prompt:\s*\n?)(.*?)(?=\n##|\Z)",
                    re.MULTILINE | re.DOTALL
                )
                for sm in sc_pattern.finditer(raw):
                    sid = sm.group(1).strip()
                    sname = (sm.group(2) or sid).strip()
                    sdesc = (sm.group(3) or "").strip()
                    sprompt = (sm.group(4) or "").strip()
                    self.upsert_shortcut(project_id, v, sid, sname, sdesc, sprompt)

        # 4. Start Sets from build/start-sets/
        start_sets_dir = build_dir / "start-sets"
        imported_sets_count = 0
        if start_sets_dir.is_dir():
            for sdir in sorted(start_sets_dir.iterdir()):
                if sdir.is_dir():
                    meta_f = sdir / "meta.json"
                    pl_f = sdir / "prologue.md"
                    os_f = sdir / "opening-situation.md"
                    stitle = sdir.name
                    sdesc = ""
                    if meta_f.exists():
                        try:
                            mdata = json.loads(meta_f.read_text(encoding="utf-8"))
                            stitle = mdata.get("title", stitle)
                            sdesc = mdata.get("description", "")
                        except Exception:
                            pass
                    pl_c = pl_f.read_text(encoding="utf-8") if pl_f.exists() else ""
                    os_c = os_f.read_text(encoding="utf-8") if os_f.exists() else ""
                    self.upsert_start_set(project_id, sdir.name, stitle, sdesc, pl_c, os_c)
                    imported_sets_count += 1

        return {
            "success": True,
            "project_id": project_id,
            "name": root.name,
            "title": title,
            "keywords_imported": imported_kw_count,
            "start_sets_imported": imported_sets_count,
            "source_path": str(root),
        }

    # ─────────────────────────────────────────────────────────────
    # Export DB to Markdown Files
    # ─────────────────────────────────────────────────────────────
    def export_to_markdown(self, project_id: str, target_dir: Path | str | None = None) -> dict[str, Any]:
        proj = self.get_project(project_id)
        if not proj:
            raise ValueError(f"Project '{project_id}' not found in database")

        out_root = Path(target_dir).resolve() if target_dir else Path(proj["source_path"] or workspace_root() / proj["name"])
        build_dir = out_root / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        files_written = []

        # 1. story.md
        story_path = out_root / "story.md"
        story_content = f"""# Story Design

- Title: {proj['title']}
- Short Summary: {proj['short_summary']}
- Premise: {proj['premise']}
- Player Role: {proj['player_role']}

## World System & Core Axioms
- 장르: 어반 판타지 오피스 일상 / 착각 코미디
- 핵심 규칙: 인지저해 결계, 사원증 해제, 사내 정복률 KPI 관리
"""
        story_path.write_text(story_content, encoding="utf-8")
        files_written.append("story.md")

        # 2. Prompts
        for v in ("safe", "unsafe"):
            p_data = self.get_prompt(project_id, v)
            if p_data["main_prompt"]:
                f_path = build_dir / f"integrated-prompt-{v}.md"
                f_path.write_text(p_data["main_prompt"], encoding="utf-8")
                files_written.append(f"build/integrated-prompt-{v}.md")

        safe_p = self.get_prompt(project_id, "safe")
        if safe_p["prologue"]:
            (build_dir / "prologue.md").write_text(safe_p["prologue"], encoding="utf-8")
            files_written.append("build/prologue.md")
        if safe_p["opening_situation"]:
            (build_dir / "opening-situation.md").write_text(safe_p["opening_situation"], encoding="utf-8")
            files_written.append("build/opening-situation.md")

        # 3. Keywords & Shortcuts into keyword-book.md
        for v in ("safe", "unsafe"):
            kws = self.list_keywords(project_id, v)
            scs = self.list_shortcuts(project_id, v)
            if not kws and not scs:
                continue

            lines = ["# Keyword Book", ""]
            for kw in kws:
                lines.append(f"## {kw['title']}")
                lines.append(f"- 키워드: {', '.join(kw['keywords'])}")
                lines.append("- 내용:")
                lines.append(kw["content"])
                lines.append("")

            if scs:
                lines.append("---")
                lines.append("")
                lines.append("## Shortcuts")
                lines.append("")
                for sc in scs:
                    lines.append(f"## {sc['shortcut_id']}")
                    lines.append(f"- name: {sc['name']}")
                    lines.append(f"- description: {sc['description']}")
                    lines.append("- prompt:")
                    lines.append(sc["prompt"])
                    lines.append("")

            kw_fname = f"keyword-book-{v}.md" if v == "unsafe" else "keyword-book.md"
            (build_dir / kw_fname).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
            files_written.append(f"build/{kw_fname}")

        # 4. Start sets
        ssets = self.list_start_sets(project_id)
        if ssets:
            start_dir = build_dir / "start-sets"
            start_dir.mkdir(parents=True, exist_ok=True)
            for ss in ssets:
                ss_dir = start_dir / ss["set_id"]
                ss_dir.mkdir(parents=True, exist_ok=True)
                (ss_dir / "meta.json").write_text(json.dumps({
                    "title": ss["title"],
                    "description": ss["description"]
                }, indent=2, ensure_ascii=False), encoding="utf-8")
                (ss_dir / "prologue.md").write_text(ss["prologue"], encoding="utf-8")
                (ss_dir / "opening-situation.md").write_text(ss["opening_situation"], encoding="utf-8")
            files_written.append("build/start-sets/")

        return {
            "success": True,
            "project_id": project_id,
            "target_dir": str(out_root),
            "files_written": files_written,
        }

    # ─────────────────────────────────────────────────────────────
    # Export Project to ZIP Archive
    # ─────────────────────────────────────────────────────────────
    def create_zip_archive(self, project_id: str, output_dir: Path | str | None = None) -> dict[str, Any]:
        proj = self.get_project(project_id)
        if not proj:
            raise ValueError(f"Project '{project_id}' not found")

        # First ensure markdown build is exported to a staging location
        out_dir = Path(output_dir or str(exports_dir())).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        zip_filename = f"{proj['name']}_export.zip"
        zip_path = out_dir / zip_filename

        staging_dir = out_dir / f"_stage_{proj['id']}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.export_to_markdown(project_id, target_dir=staging_dir)

            # Copy assets directory if available in source project
            src_root = Path(proj["source_path"]) if proj["source_path"] else None
            if src_root and (src_root / "build" / "assets").is_dir():
                shutil.copytree(src_root / "build" / "assets", staging_dir / "build" / "assets", dirs_exist_ok=True)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in staging_dir.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(staging_dir)
                        zf.write(f, arcname=str(rel))

            file_size = zip_path.stat().st_size
            return {
                "success": True,
                "project_id": project_id,
                "name": proj["name"],
                "zip_path": str(zip_path),
                "filename": zip_filename,
                "file_size_bytes": file_size,
                "download_path": f"/api/download/{zip_filename}",
            }
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

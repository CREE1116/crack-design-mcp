"""Project and keyword book asset manager with code-enforced formatting and validation."""

from __future__ import annotations
import json
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

from ..config import (
    MAX_PROMPT, TARGET_PROMPT,
    MAX_OPENING, TARGET_OPENING,
    MAX_KEYWORD_ENTRY, MIN_KEYWORDS, MAX_KEYWORDS
)
from ..audit.length import count_chars
from ..emulator.parser import parse_keyword_book, update_keyword_book_entry, delete_keyword_book_entry

@dataclass
class KeywordEntryData:
    title: str
    keywords: list[str]
    content: str
    measured_length: int = 0
    passed: bool = True
    errors: list[str] | None = None

def format_keyword_content(content_or_bullets: str | list[str], header_tag: str | None = None) -> str:
    """Format bullet points or raw content into compact Crack keyword book body."""
    if isinstance(content_or_bullets, list):
        body_lines = [f"-{line.strip().lstrip('-').strip()}" for line in content_or_bullets if line.strip()]
        body = "\n".join(body_lines)
    else:
        body = content_or_bullets.strip()

    if header_tag:
        clean_tag = header_tag.strip().strip("[]")
        if not body.startswith(f"[{clean_tag}]"):
            body = f"[{clean_tag}]\n{body}"
    return body

class PathEscape(ValueError):
    """A caller-supplied name resolved outside the project it names."""


def contained(base: Path, name: str) -> Path:
    """Join `name` under `base`, refusing anything that escapes it.

    Artifact and project names arrive straight from a tool call, and the server
    is meant to be reachable from outside this machine. `..` segments and
    absolute paths therefore have to be rejected rather than normalised away:
    without this, get_artifact reads any file the process can read and
    save_artifact writes any file it can write.
    """
    candidate = Path(name)
    if candidate.is_absolute() or candidate.drive or name.startswith("~"):
        raise PathEscape(f"경로 '{name}' — 절대경로는 허용되지 않습니다")
    base = base.resolve()
    target = (base / candidate).resolve()
    if target != base and base not in target.parents:
        raise PathEscape(f"경로 '{name}' 이 프로젝트 밖({base})을 가리킵니다")
    return target


class ProjectManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.build_dir = self.root / "build"

    def init_project(self, title: str, premise: str, player_role: str = "") -> dict[str, Any]:
        """Initialize a standard Crack project structure with initial files."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.build_dir.mkdir(parents=True, exist_ok=True)
        (self.build_dir / "start-sets").mkdir(parents=True, exist_ok=True)
        (self.build_dir / "assets").mkdir(parents=True, exist_ok=True)

        story_file = self.root / "story.md"
        if not story_file.exists():
            story_content = f"""# Story

## Core
- Title: {title}
- Premise: {premise}
- Core loop: 
- Tone / genre: 

## Player's place in the world
- Invariant role: {player_role or '평범한 인물'}
- Facts player may freely define: 이름, 외모, 성격
- Initial NPC knowledge:

## World canon
### Hard rules
- : 

### Factions
- : 

## Story engine
### 
- Trigger:
- Player choice:

## Opening material
### Prologue draft
[소설 형식의 800~1,000자 오프닝 지문 작성]

### First situation draft
[첫 턴에 플레이어가 직면하는 800~1,000자 상황 및 인물 조우 지문 작성]

## Optional lore candidates
### 
- Factual detail: 
- Suggested keywords: 키워드1, 키워드2
"""
            story_file.write_text(story_content, encoding="utf-8")

        chars_file = self.root / "characters.md"
        if not chars_file.exists():
            chars_content = f"""# Characters

## 조력자 이름 「직책」
- Role / goal / fear:
- Behavior:
  - relaxed→
  - pressure→
  - trust→
  - boundary breach→
- Speech:
  - 어조/말투:
  - 샘플 대사 1: "..."
  - 샘플 대사 2: "..."
- Knowledge:
- Ability / limits:
- Intimacy / Delta:
  - SAFE:
  - UNSAFE:
"""
            chars_file.write_text(chars_content, encoding="utf-8")

        # Initialize build artifacts so it is immediately playable
        safe_prompt = self.build_dir / "integrated-prompt-safe.md"
        if not safe_prompt.exists():
            safe_prompt.write_text(f"""# 표기
- 치환: {{user}}=ⓤ, NPC=ⓒ, 서술자=AI
- 문법: IF(조건)→결과; 불가→대안, A·B(병렬), A｜B(속성), A→B(인과)

# 1. 필수 추론 및 사칭·에코 방지 잠금 (Thinking Check)
- {{user}} information = ⓤ프로필
- ⓤ 자율성 6중 잠금: ⓤ 대사·행동·생각·감정·선택 대리서술 절대금지
- ⓤ 대사 재인용·에코 절대금지 (Anti-Echo): 지문 내 ⓤ 대사 재인용 금지

# 2. 3단 입력 파싱 및 오프닝 규약
- 1순위 [ ]: 메타 지시문
- 2순위 *text*: 설정/지문 입력
- 3순위 평문: 대사/행동

# 3. 서사 인과율 및 부정편향 완충
- 톤앤매너 유지 및 긍정적 서사 전개

# 4. 세계관 및 인물 명부
- Title: {title}
- Premise: {premise}

# 5. 출력 계약 & 상태창(HUD)
- 지문은 기울임표(*...*), 대사는 이름 | "대사"
- 하단 [INFO] 상태창 출력

# SAFE 연출 프로필
- 전연령 일상 서사
""", encoding="utf-8")

        unsafe_prompt = self.build_dir / "integrated-prompt-unsafe.md"
        if not unsafe_prompt.exists():
            unsafe_prompt.write_text(safe_prompt.read_text(encoding="utf-8").replace("# SAFE 연출 프로필\n- 전연령 일상 서사", "# UNSAFE 연출 프로필\n- 성인 연출 프로필 허용 (정책 우회 문구 배제)"), encoding="utf-8")

        pro_p = self.build_dir / "prologue.md"
        if not pro_p.exists():
            pro_p.write_text(f"""*[{title}의 프롤로그 지문]*\n\n새로운 이야기가 시작됩니다.\n""", encoding="utf-8")

        start_p = self.build_dir / "start-prompt.md"
        if not start_p.exists():
            start_p.write_text(f"""[시작 상황: {title}]\n눈앞에 첫 번째 상황이 펼쳐집니다.\n""", encoding="utf-8")

        kb_safe = self.build_dir / "keyword-book-safe.md"
        if not kb_safe.exists():
            kb_safe.write_text("""# 키워드북 (SAFE)\n\n## 기본 설정\n- 키워드: 세계관, 배경\n- 내용:\n[기본 설정]\n-이야기의 기본 배경 로어입니다.\n""", encoding="utf-8")

        kb_unsafe = self.build_dir / "keyword-book-unsafe.md"
        if not kb_unsafe.exists():
            kb_unsafe.write_text(kb_safe.read_text(encoding="utf-8"), encoding="utf-8")

        kb_default = self.build_dir / "keyword-book.md"
        if not kb_default.exists():
            kb_default.write_text(kb_safe.read_text(encoding="utf-8"), encoding="utf-8")

        # Initialize default start-set
        def_set = self.build_dir / "start-sets" / "01_default"
        def_set.mkdir(parents=True, exist_ok=True)
        (def_set / "meta.md").write_text(f"""# 기본 오프닝\n{title} 기본 시작 세트\n\n- order: 0\n- default: true\n""", encoding="utf-8")
        (def_set / "prologue.md").write_text(pro_p.read_text(encoding="utf-8"), encoding="utf-8")
        (def_set / "start-prompt.md").write_text(start_p.read_text(encoding="utf-8"), encoding="utf-8")

        return {
            "success": True,
            "project_dir": str(self.root),
            "title": title,
            "files_created": ["story.md", "characters.md", "build/ (5 core artifacts, start-set, assets)"],
        }

    def _resolve_kb_file(self, target_file: str) -> Path:
        tf = target_file.strip()
        if not tf.endswith(".md"):
            tf += ".md"
        if tf in ("keyword-book-safe.md", "keyword-book-unsafe.md", "keyword-book.md"):
            return self.build_dir / tf
        p = Path(tf)
        if p.is_absolute():
            return p
        return self.build_dir / tf

    def list_keywords(self, target_file: str = "keyword-book-safe.md") -> list[dict[str, Any]]:
        """Parse the markdown file and return all keyword entries as structured dicts."""
        kb_path = self._resolve_kb_file(target_file)
        if not kb_path.exists():
            return []

        text = kb_path.read_text(encoding="utf-8")
        entries, _ = parse_keyword_book(text, source=kb_path.name)
        result = []
        for e in entries:
            measured, cp, u16 = count_chars(e.content)
            errors = []
            if measured > MAX_KEYWORD_ENTRY:
                errors.append(f"내용 길이 초과: {measured}/{MAX_KEYWORD_ENTRY}자 ({measured - MAX_KEYWORD_ENTRY}자 초과)")
            if len(e.keywords) < MIN_KEYWORDS or len(e.keywords) > MAX_KEYWORDS:
                errors.append(f"키워드 개수 오류: {len(e.keywords)}개 (1~5개여야 함)")

            result.append({
                "title": e.title,
                "keywords": e.keywords,
                "content": e.content,
                "measured_length": measured,
                "passed": len(errors) == 0,
                "errors": errors,
            })
        return result

    def get_keyword(self, target_file: str, title: str) -> dict[str, Any] | None:
        """Get a specific keyword entry by title."""
        entries = self.list_keywords(target_file)
        target = title.strip().lower()
        for e in entries:
            if e["title"].strip().lower() == target:
                return e
        return None

    def upsert_keyword(
        self,
        target_file: str,
        title: str,
        keywords: list[str] | str,
        content: str | list[str],
        header_tag: str | None = None,
        new_title: str | None = None,
    ) -> dict[str, Any]:
        """Add or update a keyword entry in the markdown file with strict validation."""
        kb_path = self._resolve_kb_file(target_file)
        kb_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(keywords, str):
            kw_list = [k.strip() for k in re.split(r"[,/|]+", keywords) if k.strip()]
        else:
            kw_list = [str(k).strip() for k in keywords if str(k).strip()]

        formatted_content = format_keyword_content(content, header_tag)
        measured, codepoints, utf16 = count_chars(formatted_content)

        errors: list[str] = []
        if len(kw_list) < MIN_KEYWORDS or len(kw_list) > MAX_KEYWORDS:
            errors.append(f"키워드 개수 오류: 1~5개여야 합니다 (현재 {len(kw_list)}개: {kw_list})")
        if measured > MAX_KEYWORD_ENTRY:
            errors.append(f"본문 길이 초과: 400자 이하여야 합니다 (현재 {measured}자, {measured - MAX_KEYWORD_ENTRY}자 초과)")

        if errors:
            return {
                "success": False,
                "title": title,
                "keywords": kw_list,
                "measured_length": measured,
                "errors": errors,
                "message": "유효성 검사 실패로 저장되지 않았습니다.",
            }

        existing_text = kb_path.read_text(encoding="utf-8") if kb_path.exists() else "# 키워드북\n\n"
        new_text, is_update = update_keyword_book_entry(
            existing_text, title=title, keywords=kw_list, content=formatted_content, new_title=new_title
        )
        kb_path.write_text(new_text, encoding="utf-8")

        return {
            "success": True,
            "is_update": is_update,
            "target_file": kb_path.name,
            "title": new_title or title,
            "keywords": kw_list,
            "measured_length": measured,
            "needs_compression": needs_compression,
            "alert": alert,
            "message": alert or f"성공적으로 {'수정' if is_update else '추가'}되었습니다 ({measured}/400자).",
        }

    def batch_import_keywords(
        self,
        target_file: str,
        entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Batch upsert multiple keyword entries from JSON / dict format."""
        kb_path = self._resolve_kb_file(target_file)
        kb_path.parent.mkdir(parents=True, exist_ok=True)

        success_count = 0
        update_count = 0
        create_count = 0
        errors: list[dict[str, Any]] = []

        existing_text = kb_path.read_text(encoding="utf-8") if kb_path.exists() else "# 키워드북\n\n"

        for item in entries:
            title = item.get("title", "").strip()
            keywords = item.get("keywords", [])
            content = item.get("content", "")
            header_tag = item.get("header_tag")

            if not title:
                errors.append({"entry": item, "error": "제목(title)이 없습니다."})
                continue

            if isinstance(keywords, str):
                kw_list = [k.strip() for k in re.split(r"[,/|]+", keywords) if k.strip()]
            else:
                kw_list = [str(k).strip() for k in keywords if str(k).strip()]

            formatted_content = format_keyword_content(content, header_tag)
            measured, _, _ = count_chars(formatted_content)

            item_errs = []
            if len(kw_list) < MIN_KEYWORDS or len(kw_list) > MAX_KEYWORDS:
                item_errs.append(f"키워드 1~5개 제한 위반 ({len(kw_list)}개)")
            if measured > MAX_KEYWORD_ENTRY:
                item_errs.append(f"400자 초과 ({measured}자)")

            existing_text, is_update = update_keyword_book_entry(
                existing_text, title=title, keywords=kw_list, content=formatted_content
            )
            if item_errs:
                errors.append({"title": title, "errors": item_errs, "needs_compression": measured > MAX_KEYWORD_ENTRY})
            success_count += 1
            if is_update:
                update_count += 1
            else:
                create_count += 1

        kb_path.write_text(existing_text, encoding="utf-8")
        return {
            "success": len(errors) == 0,
            "target_file": kb_path.name,
            "total_processed": len(entries),
            "created": create_count,
            "updated": update_count,
            "error_count": len(errors),
            "errors": errors,
        }

    def delete_keyword(self, target_file: str, title: str) -> dict[str, Any]:
        """Delete a keyword entry from the markdown file."""
        kb_path = self._resolve_kb_file(target_file)
        if not kb_path.exists():
            return {"success": False, "message": f"{kb_path.name} 파일이 없습니다."}

        text = kb_path.read_text(encoding="utf-8")
        new_text, found = delete_keyword_book_entry(text, title)
        if found:
            kb_path.write_text(new_text, encoding="utf-8")
            return {"success": True, "message": f"'{title}' 항목이 삭제되었습니다."}
        return {"success": False, "message": f"'{title}' 항목을 찾을 수 없습니다."}

    def save_prompt(self, prompt_type: str, content: str) -> dict[str, Any]:
        """Save a prompt file (integrated-safe, integrated-unsafe, prologue, start-prompt) with length check."""
        file_map = {
            "integrated-safe": ("integrated-prompt-safe.md", MAX_PROMPT, TARGET_PROMPT),
            "integrated-unsafe": ("integrated-prompt-unsafe.md", MAX_PROMPT, TARGET_PROMPT),
            "prologue": ("prologue.md", MAX_OPENING, TARGET_OPENING),
            "start-prompt": ("start-prompt.md", MAX_OPENING, TARGET_OPENING),
            "story": ("../story.md", 0, 0),
            "characters": ("../characters.md", 0, 0),
        }

        key = prompt_type.strip().lower()
        if key in file_map:
            fname, limit, target = file_map[key]
            out_path = self.build_dir / fname
        else:
            out_path = self.build_dir / prompt_type
            limit, target = MAX_PROMPT, TARGET_PROMPT

        measured, codepoints, utf16 = count_chars(content)
        passed = limit <= 0 or measured <= limit
        is_warn = passed and limit > 0 and measured > target

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content.strip() + "\n", encoding="utf-8")

        return {
            "success": True,
            "file": out_path.name,
            "measured": measured,
            "limit": limit,
            "remaining": (limit - measured) if limit > 0 else 0,
            "passed": passed,
            "is_warn": is_warn,
            "status": "PASS" if (passed and not is_warn) else ("WARN" if is_warn else "FAIL"),
        }

    def read_prompt(self, prompt_type: str) -> dict[str, Any]:
        """Read a prompt file and return content and length metrics."""
        file_map = {
            "integrated-safe": "integrated-prompt-safe.md",
            "integrated-unsafe": "integrated-prompt-unsafe.md",
            "prologue": "prologue.md",
            "start-prompt": "start-prompt.md",
            "story": "../story.md",
            "characters": "../characters.md",
        }
        fname = file_map.get(prompt_type.strip().lower(), prompt_type)
        p = self.build_dir / fname
        if not p.exists():
            return {"exists": False, "file": fname, "content": ""}

        content = p.read_text(encoding="utf-8")
        measured, codepoints, utf16 = count_chars(content)
        return {
            "exists": True,
            "file": fname,
            "measured": measured,
            "codepoints": codepoints,
            "utf16": utf16,
            "content": content,
        }

    # ── Start Sets CRUD ───────────────────────────────────────────
    def list_start_sets(self) -> list[dict[str, Any]]:
        sets_dir = self.build_dir / "start-sets"
        if not sets_dir.is_dir():
            return []
        res = []
        for folder in sorted(sets_dir.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            meta_p = folder / "meta.md"
            title, desc, order, is_def = folder.name, "", 0, False
            if meta_p.exists():
                text = meta_p.read_text(encoding="utf-8")
                m_title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                if m_title:
                    title = m_title.group(1).strip()
                m_ord = re.search(r"-\s*order:\s*(\d+)", text, re.IGNORECASE)
                if m_ord:
                    order = int(m_ord.group(1))
                m_def = re.search(r"-\s*default:\s*(true|false)", text, re.IGNORECASE)
                if m_def:
                    is_def = m_def.group(1).lower() == "true"
                desc_lines = [l for l in text.splitlines() if not l.startswith("#") and not l.startswith("-") and l.strip()]
                desc = " ".join(desc_lines)

            pro_p = folder / "prologue.md"
            sp_p = folder / "start-prompt.md"
            res.append({
                "id": folder.name,
                "title": title,
                "description": desc,
                "order": order,
                "default": is_def,
                "has_prologue": pro_p.exists(),
                "prologue_len": len(pro_p.read_text(encoding="utf-8")) if pro_p.exists() else 0,
                "has_start_prompt": sp_p.exists(),
                "start_prompt_len": len(sp_p.read_text(encoding="utf-8")) if sp_p.exists() else 0,
            })
        return sorted(res, key=lambda x: (x["order"], x["id"]))

    def get_start_set(self, set_id: str) -> dict[str, Any] | None:
        folder = self.build_dir / "start-sets" / set_id
        if not folder.is_dir():
            return None
        meta_p = folder / "meta.md"
        title, desc, order, is_def = folder.name, "", 0, False
        if meta_p.exists():
            text = meta_p.read_text(encoding="utf-8")
            m_title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            if m_title:
                title = m_title.group(1).strip()
            m_ord = re.search(r"-\s*order:\s*(\d+)", text, re.IGNORECASE)
            if m_ord:
                order = int(m_ord.group(1))
            m_def = re.search(r"-\s*default:\s*(true|false)", text, re.IGNORECASE)
            if m_def:
                is_def = m_def.group(1).lower() == "true"
            desc_lines = [l for l in text.splitlines() if not l.startswith("#") and not l.startswith("-") and l.strip()]
            desc = " ".join(desc_lines)

        pro_p = folder / "prologue.md"
        sp_p = folder / "start-prompt.md"
        pro_text = pro_p.read_text(encoding="utf-8") if pro_p.exists() else ""
        sp_text = sp_p.read_text(encoding="utf-8") if sp_p.exists() else ""
        pro_len, _, _ = count_chars(pro_text)
        sp_len, _, _ = count_chars(sp_text)

        return {
            "id": set_id,
            "title": title,
            "description": desc,
            "order": order,
            "default": is_def,
            "prologue": pro_text,
            "prologue_chars": pro_len,
            "start_prompt": sp_text,
            "start_prompt_chars": sp_len,
        }

    def save_start_set(self, set_id: str, title: str, description: str = "",
                       order: int = 0, is_default: bool = False,
                       prologue: str = "", start_prompt: str = "") -> dict[str, Any]:
        folder = self.build_dir / "start-sets" / set_id
        folder.mkdir(parents=True, exist_ok=True)

        meta_content = f"# {title}\n{description}\n\n- order: {order}\n- default: {'true' if is_default else 'false'}\n"
        (folder / "meta.md").write_text(meta_content, encoding="utf-8")

        warnings = []
        if prologue:
            p_len, _, _ = count_chars(prologue)
            if p_len > MAX_OPENING:
                warnings.append(f"프롤로그가 {p_len}자로 1,000자를 {p_len - MAX_OPENING}자 초과했습니다.")
            (folder / "prologue.md").write_text(prologue.strip() + "\n", encoding="utf-8")

        if start_prompt:
            sp_len, _, _ = count_chars(start_prompt)
            if sp_len > MAX_OPENING:
                warnings.append(f"시작 프롬프트가 {sp_len}자로 1,000자를 {sp_len - MAX_OPENING}자 초과했습니다.")
            (folder / "start-prompt.md").write_text(start_prompt.strip() + "\n", encoding="utf-8")

        # If set as default, sync to root build files
        if is_default:
            if prologue:
                (self.build_dir / "prologue.md").write_text(prologue.strip() + "\n", encoding="utf-8")
            if start_prompt:
                (self.build_dir / "start-prompt.md").write_text(start_prompt.strip() + "\n", encoding="utf-8")

        return {
            "success": True,
            "id": set_id,
            "title": title,
            "order": order,
            "default": is_default,
            "warnings": warnings,
            "needs_compression": len(warnings) > 0,
        }

    def delete_start_set(self, set_id: str) -> dict[str, Any]:
        folder = self.build_dir / "start-sets" / set_id
        if not folder.is_dir():
            return {"success": False, "message": f"start-set '{set_id}'를 찾을 수 없습니다."}
        import shutil
        shutil.rmtree(folder)
        return {"success": True, "deleted_id": set_id}

    # ── Generic Artifacts CRUD ────────────────────────────────────
    def list_artifacts(self) -> list[dict[str, Any]]:
        assets_dir = self.build_dir / "assets"
        res = []
        if assets_dir.is_dir():
            for p in sorted(assets_dir.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    measured, _, _ = count_chars(p.read_text(encoding="utf-8"))
                    res.append({
                        "name": f"assets/{p.name}",
                        "chars": measured,
                        "path": str(p.relative_to(self.root))
                    })
        for p in sorted(self.build_dir.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                measured, _, _ = count_chars(p.read_text(encoding="utf-8"))
                res.append({
                    "name": p.name,
                    "chars": measured,
                    "path": str(p.relative_to(self.root))
                })
        return res

    def get_artifact(self, name: str) -> dict[str, Any] | None:
        clean_name = name.strip().lstrip("/")
        for base, rel in ((self.build_dir, clean_name),
                          (self.build_dir, f"assets/{clean_name}"),
                          (self.root, clean_name)):
            cand = contained(base, rel)
            if cand.is_file():
                p = cand
                break
        else:
            return None
        content = p.read_text(encoding="utf-8")
        measured, cp, u16 = count_chars(content)
        return {
            "name": clean_name,
            "chars": measured,
            "content": content,
            "path": str(p.relative_to(self.root))
        }

    def save_artifact(self, name: str, content: str) -> dict[str, Any]:
        clean_name = name.strip().lstrip("/")
        if clean_name.startswith("assets/"):
            p = contained(self.build_dir, clean_name)
        elif clean_name in ("story-description.md", "summary-comment.md", "image-prompts.md", "prompts.json"):
            p = contained(self.build_dir, f"assets/{clean_name}")
        elif clean_name in ("story.md", "characters.md"):
            p = contained(self.root, clean_name)
        else:
            p = contained(self.build_dir, clean_name)

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
        measured, _, _ = count_chars(content)
        return {
            "success": True,
            "name": clean_name,
            "chars": measured,
            "path": str(p.relative_to(self.root))
        }

    def delete_artifact(self, name: str) -> dict[str, Any]:
        clean_name = name.strip().lstrip("/")
        for base, rel in ((self.build_dir, clean_name),
                          (self.build_dir, f"assets/{clean_name}")):
            cand = contained(base, rel)
            if cand.is_file():
                p = cand
                break
        else:
            return {"success": False, "message": f"산출물 '{name}'을 찾을 수 없습니다."}
        p.unlink()
        return {"success": True, "deleted": clean_name}

    # ── Shortcuts CRUD (from crack_sync.py spec) ───────────────────
    def list_shortcuts(self, target_file: str = "keyword-book.md") -> list[dict[str, Any]]:
        kb_path = self._resolve_kb_file(target_file)
        if not kb_path.exists():
            return []
        text = kb_path.read_text(encoding="utf-8")
        _, shortcuts = parse_keyword_book(text, source=kb_path.name)
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "prompt": s.prompt,
                "prompt_chars": len(s.prompt),
            }
            for s in shortcuts
        ]

    def update_shortcut(self, name: str, description: str, prompt: str,
                        shortcut_id: str | None = None,
                        target_file: str = "keyword-book.md") -> dict[str, Any]:
        kb_path = self._resolve_kb_file(target_file)
        text = kb_path.read_text(encoding="utf-8") if kb_path.exists() else "# 키워드북\n\n"

        sc_id = shortcut_id or (f"/{name.lstrip('/')}" if not name.startswith("/") else name)
        clean_name = name.lstrip("/")

        # Check if # Shortcuts section exists
        sc_match = re.search(r"^#+\s*(?:Shortcuts?|단축어)\b", text, re.MULTILINE | re.IGNORECASE)
        sc_block = f"\n## {sc_id}\n- name: {clean_name}\n- description: {description.strip()}\n- prompt:\n{prompt.strip()}\n"

        if not sc_match:
            text = text.rstrip() + "\n\n# Shortcuts\n" + sc_block
            is_update = False
        else:
            prefix = text[:sc_match.start()]
            sc_section = text[sc_match.start():]
            
            pattern = rf"^##\s+(?:{re.escape(sc_id)}|{re.escape(clean_name)})\b.*?(?=^##|\Z)"
            found = re.search(pattern, sc_section, re.MULTILINE | re.DOTALL)
            if found:
                new_sc_section = sc_section[:found.start()] + sc_block.lstrip() + sc_section[found.end():]
                is_update = True
            else:
                new_sc_section = sc_section.rstrip() + "\n" + sc_block
                is_update = False
            text = prefix + new_sc_section

        kb_path.write_text(text, encoding="utf-8")
        return {
            "success": True,
            "is_update": is_update,
            "id": sc_id,
            "name": clean_name,
            "description": description,
            "prompt_chars": len(prompt),
        }

    def delete_shortcut(self, name: str, target_file: str = "keyword-book.md") -> dict[str, Any]:
        kb_path = self._resolve_kb_file(target_file)
        if not kb_path.exists():
            return {"success": False, "message": f"{kb_path.name} 파일이 없습니다."}

        text = kb_path.read_text(encoding="utf-8")
        sc_match = re.search(r"^#+\s*(?:Shortcuts?|단축어)\b", text, re.MULTILINE | re.IGNORECASE)
        if not sc_match:
            return {"success": False, "message": "단축어 섹션이 없습니다."}

        prefix = text[:sc_match.start()]
        sc_section = text[sc_match.start():]
        clean_name = name.lstrip("/")
        pattern = rf"^##\s+(?:{re.escape(name)}|{re.escape(clean_name)}|/{re.escape(clean_name)})\b.*?(?=^##|\Z)"
        found = re.search(pattern, sc_section, re.MULTILINE | re.DOTALL)
        if not found:
            return {"success": False, "message": f"단축어 '{name}'을 찾을 수 없습니다."}

        new_sc_section = sc_section[:found.start()] + sc_section[found.end():]
        kb_path.write_text(prefix + new_sc_section.rstrip() + "\n", encoding="utf-8")
        return {"success": True, "deleted_shortcut": name}

    # ── Inspection payload for Crack Web Sync ──────────────────────
    def get_sync_payload(self, variant: str = "safe") -> dict[str, Any]:
        """Assembles the exact payload required to sync into Crack web editor via crack_sync.py."""
        story_file = self.root / "story.md"
        title = self.root.name
        short_summary = ""
        if story_file.exists():
            s_text = story_file.read_text(encoding="utf-8")
            t_match = re.search(r"^-\s*Title:\s*(.+)$", s_text, re.MULTILINE | re.IGNORECASE)
            if t_match:
                title = t_match.group(1).strip()
            sum_m = re.search(r"^-\s*(?:Logline|한줄소개|한줄설명|Tagline|Premise|로그라인|소개):\s*(.+)$", s_text, re.MULTILINE | re.IGNORECASE)
            if sum_m:
                short_summary = sum_m.group(1).strip()

        # Artifacts
        pro_p = self.build_dir / "prologue.md"
        sp_p = self.build_dir / "start-prompt.md"
        sys_p = self.build_dir / f"integrated-prompt-{variant}.md"
        desc_p = self.build_dir / "assets" / "story-description.md"
        comm_p = self.build_dir / "assets" / "summary-comment.md"

        prologue = pro_p.read_text(encoding="utf-8").strip() if pro_p.exists() else ""
        start_prompt = sp_p.read_text(encoding="utf-8").strip() if sp_p.exists() else ""
        system_prompt = sys_p.read_text(encoding="utf-8").strip() if sys_p.exists() else ""
        story_desc = desc_p.read_text(encoding="utf-8").strip() if desc_p.exists() else ""
        summary_comm = comm_p.read_text(encoding="utf-8").strip() if comm_p.exists() else ""

        if not short_summary and story_desc:
            c_m = re.search(r"「제작자 코멘트」\s*\n+(.+?)(?:\n\n|\Z)", story_desc, re.DOTALL)
            if c_m:
                short_summary = c_m.group(1).strip().splitlines()[0]

        kb_file = f"keyword-book-{variant}.md"
        if not (self.build_dir / kb_file).exists():
            kb_file = "keyword-book.md"

        kw_entries = self.list_keywords(kb_file)
        shortcuts = self.list_shortcuts(kb_file)
        start_sets = self.list_start_sets()

        return {
            "title": title,
            "short_summary": short_summary,
            "variant": variant,
            "prologue_chars": count_chars(prologue)[0],
            "start_prompt_chars": count_chars(start_prompt)[0],
            "system_prompt_chars": count_chars(system_prompt)[0],
            "prologue": prologue,
            "start_prompt": start_prompt,
            "system_prompt": system_prompt,
            "keyword_entries_count": len(kw_entries),
            "keyword_entries": kw_entries,
            "shortcuts_count": len(shortcuts),
            "shortcuts": shortcuts,
            "start_sets_count": len(start_sets),
            "start_sets": start_sets,
            "story_description_chars": len(story_desc),
            "story_description": story_desc,
            "summary_comment_chars": len(summary_comm),
            "summary_comment": summary_comm,
        }

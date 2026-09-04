"""MCP server over stdio. No SDK dependency — the protocol is JSON-RPC 2.0.

Exposes the harness as tools so an agent can play a Crack build itself and read
back what broke. The point is the loop: `play_turn` returns the reply together
with the contract violations, the keyword-book entries that fired, and the ones
that lost their slot, so the agent sees the consequence of its own input in the
same call.

    crack-emu mcp --project <build> [--store DIR] [--provider NAME]

Everything is the same engine the CLI and the web UI use. A second
implementation of the rules would drift, and then no result could be trusted.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

from ..emulator import memory, qa
from ..emulator.config import Config
from ..emulator.engine import Engine
from ..emulator.llm import make_client
from ..emulator.log import ActivationLog, report
from ..emulator.models import Session
from ..emulator.parser import lint, load_project, update_keyword_book_entry, delete_keyword_book_entry
from ..emulator.session import Store
from ..emulator.sets import use as use_start_set
from ..designer.manager import ProjectManager
from ..designer.templates import get_template
from ..designer.guides import get_guide
from ..audit.full_audit import audit_project as run_audit_project
from ..audit.length import count_chars
from ..config import workspace_root, exports_dir, state_root
from ..keys import KeyStore
from ..designer.manager import contained, PathEscape


PROTOCOL_VERSION = "2024-11-05"


class Server:
    def __init__(self, project: str, store: str | None, spec: str | None,
                 provider: str | None, variant: str, key_file: str | None):
        self.project_root = project
        self.spec = spec
        self.provider = provider
        self.variant = variant
        self.store = Store(store)
        self.log = ActivationLog(self.store.root.parent / "logs")
        self.cfg = Config.load(spec)
        self.project = load_project(project)
        # Same store the web UI writes to, so a key set in either is visible
        # to both and survives a restart.
        self.keys = KeyStore(key_file)
        self.key_file = self.keys.path

    # ── engine ────────────────────────────────────────────────────
    def _engine(self, overrides: dict | None = None, variant: str | None = None) -> Engine:
        cfg = self.cfg.override(overrides or {})
        provider = (overrides or {}).get("llm.provider") or self.provider \
            or cfg.get("llm.provider")
        client = make_client(cfg, provider, api_key=self.keys.get(provider))
        return Engine(self.project_root, cfg, client, self.store,
                      variant=variant or self.variant, log=self.log)

    @staticmethod
    def _overrides(args: dict) -> dict:
        out = {
            "llm.provider": args.get("provider"),
            "llm.model": args.get("model"),
            "context.window_turns": args.get("window_turns"),
            "keyword.scan_turns": args.get("scan_turns"),
            "memory.recalled_selection": args.get("recall"),
            "fidelity": args.get("fidelity"),
        }
        return {k: v for k, v in out.items() if v not in (None, "")}

    # ── tools ─────────────────────────────────────────────────────
    def tool_describe_project(self, a: dict) -> dict:
        p_name = a.get("project_name") or a.get("project")
        if p_name:
            self.tool_switch_project({"project_name": p_name})
        p = self.project
        c = p.contract
        return {
            "name": p.name,
            "root": p.root,
            "variants": p.variants,
            "characters": [{"number": ch.number, "name": ch.name} for ch in p.characters],
            "start_sets": [{"id": x.id, "title": x.title, "description": x.description,
                            "default": x.is_default, "source": x.source}
                           for x in p.start_sets],
            "shortcuts": {v: [{"name": s.name, "description": s.description}
                              for s in p.shortcut_list(v)] for v in p.variants},
            "keyword_entries": {v: [{"title": e.title, "keywords": e.keywords,
                                     "chars": e.char_count} for e in p.entries(v)]
                                for v in p.variants},
            "contract": {
                "dialogue_separators": c.dialogue_separators,
                "narration_wrapper": c.narration_wrapper,
                "hud_fence": c.hud_fence,
                "hud_fields": c.hud_fields,
                "hud_required": c.hud_required,
                "image_id_kind": c.image_id_kind,
                "situation_codes": c.situation_codes,
                "restricted_codes": c.restricted_codes,
                "length_min": c.length_min, "length_max": c.length_max,
                "length_unit": c.length_unit,
                "evidence": c.detected,
                "note": "규칙은 이 빌드의 프롬프트에서 유도했습니다. "
                        "값이 없는 항목의 규칙은 실행되지 않습니다.",
            },
        }

    def tool_lint_build(self, a: dict) -> dict:
        findings = lint(self.project)
        counts: dict[str, int] = {}
        for f in findings:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        return {"findings": findings, "counts": counts,
                "passed": counts.get("error", 0) == 0}

    def _resolve_variant(self, requested: str | None) -> str:
        """Pick a variant, and never quietly hand back a different one."""
        if not requested:
            return self.variant
        requested = requested.strip().lower()
        if requested in self.project.variants:
            return requested
        raise ValueError(
            f"variant '{requested}' 는 이 프로젝트에 없습니다. "
            f"사용 가능: {', '.join(self.project.variants)}")

    def tool_start_session(self, a: dict) -> dict:
        p_name = a.get("project_name") or a.get("project")
        if p_name:
            self.tool_switch_project({"project_name": p_name})
        # "default" is a real variant — keyword-book.md / integrated-prompt.md —
        # not a word meaning "whatever the server was started with". Treating it
        # as the latter silently ran default sessions against SAFE, which is
        # exactly the substitution that makes a variant test worthless.
        variant = self._resolve_variant(a.get("variant"))
        eng = self._engine(self._overrides(a), variant=variant)
        sid = a["session"]
        if self.store.exists(sid) and a.get("overwrite"):
            self.store.delete(sid)
        if not self.store.exists(sid):
            eng.start(sid,
                      persona_name=a.get("persona_name") or "{{user}}",
                      persona_body=a.get("persona_body") or "",
                      user_note=a.get("user_note") or "",
                      goal=a.get("goal") or "",
                      start_set=a.get("start_set"))
        s = self.store.load(sid)
        chosen = self.project.start_set(s.start_set)
        requested = (a.get("variant") or "").strip().lower()
        return {"session": s.id, "variant": s.variant,
                # Surface a substitution instead of letting it pass unnoticed.
                "variant_requested": requested or None,
                "variant_matches_request": (not requested) or requested == s.variant,
                "start_set": s.start_set,
                "turns": len(s.turns),
                "prologue": s.turns[0].content if s.turns else "",
                "opening_situation": chosen.opening_situation if chosen else ""}

    def tool_play_turn(self, a: dict) -> dict:
        sid = a["session"]
        if not self.store.exists(sid):
            raise ValueError(f"session '{sid}' not started; call start_session first")
        s = self.store.load(sid)
        variant = self._resolve_variant(a.get("variant")) if a.get("variant") \
            else (s.variant or self.variant)
        eng = self._engine(self._overrides(a), variant=variant)
        for field in ("persona_name", "persona_body", "user_note", "goal"):
            if a.get(field) is not None:
                setattr(s, field, a[field])
        res = eng.turn(s, a["input"], reply=a.get("reply"))
        d = res.to_dict()
        d["note"] = ("findings 는 이 빌드의 출력 계약 위반입니다. "
                     "dropped 는 3슬롯을 넘겨 주입되지 못한 키워드북 항목입니다.")
        return d


    def tool_get_tester_context(self, a: dict) -> dict:
        """Provide isolated persona prompts to prevent omniscience and knowledge leakage in simulation."""
        sid = a.get("session")
        if not sid or not self.store.exists(sid):
            raise ValueError(f"session '{sid}' not found. call start_session first")
        role = (a.get("role") or "player").strip().lower()
        s = self.store.load(sid)
        variant = self._resolve_variant(a.get("variant")) if a.get("variant") \
            else (s.variant or self.variant)
        eng = self._engine(self._overrides(a), variant=variant)

        if role == "player":
            # Blind Player Context (Knows only visible conversation, zero lore knowledge)
            visible_turns = [
                {"role": t.role, "content": t.content}
                for t in s.turns
            ]
            last_turn = s.turns[-1].content if s.turns else ""
            return {
                "role": "player",
                "persona_name": s.persona_name or "신입 {{user}}",
                "knowledge_boundary": "[정보 격리 원칙] 당신은 세계관의 비밀, 키워드북, 미공개 설정을 전혀 모르는 평범한 인물입니다.",
                "visible_conversation": visible_turns,
                "last_scene": last_turn,
                "prompting_instruction": (
                    f"당신은 '{s.persona_name or '신입'}'의 시선에서만 생각해야 합니다. "
                    "세계관의 숨겨진 지식이나 시스템 키워드를 미리 알고 행동하지 마세요. "
                    "방금 눈앞에서 일어난 대사나 상황에 대해 당황하거나, 호기심을 갖거나, 상식적으로 답변하는 1~2줄의 현실적인 플레이어 입력을 생성하세요."
                )
            }
        elif role == "narrator":
            # Scoped Narrator Context (Knows ONLY the main prompt + currently activated 3 keywords)
            user_input = a.get("input") or (s.turns[-1].content if s.turns and s.turns[-1].role == "user" else "")
            if not user_input:
                raise ValueError("input is required to build narrator context")
            p = eng.build_prompt(s, user_input)
            return {
                "role": "narrator",
                "scoped_system_prompt": p.system,
                "recent_messages": p.messages,
                "activated_keywords": [x.entry.title for x in p.activations],
                "dropped_keywords": [x.entry.title for x in p.dropped],
                "prompting_instruction": (
                    "당신은 Crack 롤플레잉 서술자입니다. "
                    "위 scoped_system_prompt에 명시된 규칙과 현재 활성화된 키워드 내용 범위 내에서만 서술하세요. "
                    "주입되지 않은 다른 키워드북의 비밀을 섣불리 먼저 누설(Knowledge Leak)하지 마세요. "
                    '지문은 기울임표(*...*), 대사는 이름 | "대사", 최하단에는 [INFO] 상태창을 반드시 출력하세요.'
                )
            }
        else:
            raise ValueError(f"unknown role '{role}'. Choose 'player' or 'narrator'")
    def tool_inspect_prompt(self, a: dict) -> dict:
        sid = a["session"]
        s = self.store.load(sid) if self.store.exists(sid) else Session(
            id=sid, project_root=self.project.root, variant=a.get("variant") or self.variant)
        variant = self._resolve_variant(a.get("variant")) if a.get("variant") \
            else (s.variant or self.variant)
        eng = self._engine(self._overrides(a), variant=variant)
        p = eng.build_prompt(s, a.get("input") or "")
        window = int(eng.cfg.get("context.window_turns", 20))
        return {"system": p.system, "messages": p.messages, "char_count": p.char_count,
                # Same key as play_turn: an agent driving both should not have
                # to remember which one calls it what.
                "activations": [{"title": x.entry.title, "matched": x.matched,
                                 "where": x.where, "chars": x.entry.char_count}
                                for x in p.activations],
                "activated": [x.entry.title for x in p.activations],   # kept for compatibility
                # dropped mirrors activations' shape — it was a bare title list
                # while its sibling held objects, which breaks any caller that
                # walks both the same way.
                "dropped": [{"title": x.entry.title, "matched": x.matched,
                             "where": x.where, "chars": x.entry.char_count}
                            for x in p.dropped],
                # play_turn names the matched shortcut; this reported nothing,
                # so an agent could see the injected text without being told
                # which shortcut put it there.
                "shortcut": ({"id": p.shortcut.id, "name": p.shortcut.name,
                              "description": p.shortcut.description,
                              "prompt": p.shortcut.prompt}
                             if p.shortcut else None),
                "live_turns": len(memory.live_turns(s, window)),
                "evicted_turns": len(memory.evicted_turns(s, window))}

    def tool_check_response(self, a: dict) -> dict:
        s = self.store.load(a["session"]) if a.get("session") and \
            self.store.exists(a["session"]) else Session(
                id="adhoc", project_root=self.project.root, variant=self.variant)
        findings = qa.check(a["text"], self.project, s, user_input=a.get("input") or "")
        return {"findings": [f.to_dict() for f in findings],
                "qa": qa.summarize(findings)}

    def tool_get_session(self, a: dict) -> dict:
        s = self.store.load(a["session"])
        return {"session": s.id, "variant": s.variant, "start_set": s.start_set,
                "persona_name": s.persona_name, "persona_body": s.persona_body,
                "user_note": s.user_note, "goal": s.goal,
                "summaries": [memory.render_summary(x) for x in s.summaries],
                "relations": s.relations,
                "turns": [{"index": t.index, "role": t.role, "content": t.content}
                          for t in s.turns]}

    def tool_list_sessions(self, a: dict) -> dict:
        """Sessions for the active project. Pass all_projects to see the store."""
        if a.get("all_projects"):
            return {"store": str(self.store.root), "scope": "all_projects",
                    "sessions": self.store.entries()}
        entries = self.store.entries(self.project_root)
        return {"store": str(self.store.root), "scope": "active_project",
                "project_root": self.project_root,
                "sessions": entries,
                "hidden_other_projects": len(self.store.orphans(self.project_root))}

    def tool_delete_session(self, a: dict) -> dict:
        sid = a["session"]
        return {"session": sid, "removed": self.store.delete(sid),
                "log_removed": self.log.delete(sid)}

    def tool_get_memory(self, a: dict) -> dict:
        cfg = self.cfg.override(self._overrides(a))
        s = self.store.load(a["session"])
        window = int(cfg.get("context.window_turns", 20))
        return memory.snapshot(self.project.root, s, cfg, window, a.get("input") or "")

    def tool_set_memory(self, a: dict) -> dict:
        s = self.store.load(a["session"])
        if "summaries" in a:
            s.summaries = [memory.parse_summary(x, i)
                       for i, x in enumerate(a["summaries"]) if str(x).strip()]
        if "relations" in a:
            s.relations = [x for x in a["relations"] if x.strip()]
        if "recalled" in a:
            s.recalled = [x for x in a["recalled"] if x.strip()]
        self.store.save(s)
        return self.tool_get_memory(a)

    def tool_set_goal(self, a: dict) -> dict:
        """Write `[주어진 목표]` by hand, replacing or extending what is there.

        The block is normally maintained as long-term memory, so a value set
        here is locked: the per-turn refresh will not overwrite it again unless
        the lock is lifted.
        """
        sess = self.store.load(a["session"])
        goal = (a.get("goal") or "").strip()
        mode = (a.get("mode") or "replace").strip().lower()
        if mode not in ("replace", "append"):
            raise ValueError("mode must be replace or append")
        before = sess.goal

        if a.get("unlock"):
            sess.goal_locked = False
            if not goal:
                self.store.save(sess)
                return {"session": sess.id, "goal": sess.goal, "goal_locked": False,
                        "message": "잠금 해제 — 다음 갱신부터 모델이 다시 목표를 씁니다."}
        if not goal and not a.get("unlock"):
            raise ValueError("goal is required (or pass unlock=true)")

        sess.goal = f"{before}\n{goal}".strip() if mode == "append" and before else goal
        sess.goal_locked = not a.get("unlock", False)
        self.store.save(sess)
        return {"session": sess.id, "mode": mode, "goal_before": before,
                "goal": sess.goal, "goal_locked": sess.goal_locked}

    def tool_set_session_meta(self, a: dict) -> dict:
        """Edit a live session's persona, user note or goal without restarting it.

        These are set at start_session and were unreachable afterwards, which
        makes the common case — noticing mid-play that the note is wrong —
        require throwing the session away.
        """
        sess = self.store.load(a["session"])
        changed = {}
        for field_name in ("persona_name", "persona_body", "user_note"):
            if a.get(field_name) is not None:
                changed[field_name] = {"before": getattr(sess, field_name),
                                       "after": a[field_name]}
                setattr(sess, field_name, a[field_name])
        if a.get("goal") is not None:
            changed["goal"] = {"before": sess.goal, "after": a["goal"]}
            sess.goal = a["goal"]
            sess.goal_locked = True
        if not changed:
            raise ValueError("nothing to change: pass persona_name, persona_body, "
                             "user_note or goal")
        self.store.save(sess)
        return {"session": sess.id, "changed": changed, "goal_locked": sess.goal_locked}

    def tool_reorder_keyword_entries(self, a: dict) -> dict:
        """Promote entries in the keyword book, which is what the 3 slots follow."""
        order = a.get("order")
        if not isinstance(order, list) or not order:
            raise ValueError("order must be a non-empty list of entry titles")
        variant = (a.get("variant") or "both").strip().lower()
        root = Path(self.project_root)
        targets = []
        if variant in ("safe", "both"):
            targets.append(root / "keyword-book-safe.md")
        if variant in ("unsafe", "both"):
            targets.append(root / "keyword-book-unsafe.md")
        kb_default = root / "keyword-book.md"
        if kb_default not in targets:
            targets.append(kb_default)

        from ..emulator.parser import reorder_keyword_book
        results = {}
        for path in targets:
            if not path.exists():
                continue
            new_text, titles = reorder_keyword_book(
                path.read_text(encoding="utf-8"), order)
            path.write_text(new_text, encoding="utf-8")
            results[path.name] = titles
        self.reload()
        return {"reordered": results, "note": "문서 순서가 3슬롯 우선순위입니다 (상단 우선)."}

    def tool_activation_report(self, a: dict) -> dict:
        sessions = a.get("sessions")
        records = self.log.read_all(sessions)
        if not records:
            return {"error": "no activation logs yet", "sessions": self.log.sessions()}
        out = report(records, self.project, a.get("variant") or self.variant,
                     min_turns=int(a.get("min_turns", 10)))
        out["sessions"] = sessions or self.log.sessions()
        return out

    def tool_use_start_set(self, a: dict) -> dict:
        result = use_start_set(self.project, a["start_set"])
        self.reload()
        return result

    def reload(self) -> None:
        self.project = load_project(self.project_root)
        if hasattr(self, "on_reload") and callable(self.on_reload):
            try:
                self.on_reload(self.project_root)
            except TypeError:
                try:
                    self.on_reload()
                except Exception:
                    pass
            except Exception:
                pass

    def tool_reload_project(self, a: dict) -> dict:
        self.reload()
        return {
            "reloaded": True,
            "project": self.project.name,
            "variants": self.project.variants,
            "keyword_entries_count": {v: len(self.project.entries(v)) for v in self.project.variants},
        }

    def tool_get_prompt(self, a: dict) -> dict:
        variant = a.get("variant") or self.variant
        target = a.get("target") or "all"
        out: dict[str, Any] = {"variant": variant}
        if target in ("all", "main"):
            prompt_text = self.project.main_prompt.get(variant, "")
            out["main_prompt"] = prompt_text
            out["main_prompt_chars"] = len(prompt_text)
        if target in ("all", "prologue"):
            out["prologue"] = self.project.prologue
            out["prologue_chars"] = len(self.project.prologue)
        if target in ("all", "opening_situation"):
            out["opening_situation"] = self.project.opening_situation
            out["opening_situation_chars"] = len(self.project.opening_situation)
        return out



    def _mgr(self) -> ProjectManager:
        p = Path(self.project_root)
        root = p.parent if p.name == "build" else p
        return ProjectManager(root)

    def tool_create_project(self, a: dict) -> dict:
        """Create a brand new Crack story-chat project and enter it."""
        p_name = a.get("project_name") or a.get("title")
        if not p_name:
            raise ValueError("project_name or title is required")
        clean_name = re.sub(r'[\\/:*?"<>|]', "_", p_name.strip())
        target_dir = contained(workspace_root(), clean_name)

        title = a.get("title", clean_name)
        premise = a.get("premise", "")
        player_role = a.get("player_role", "")

        mgr = ProjectManager(target_dir)
        res = mgr.init_project(title=title, premise=premise, player_role=player_role)

        auto_switch = a.get("auto_switch", True)
        if auto_switch:
            self.project_root = str(target_dir / "build")
            self.reload()
            res["switched_to_active"] = True
            res["active_project"] = clean_name
        return res

    def tool_list_projects(self, a: dict) -> dict:
        """List all Crack story projects in the workspace with metadata and playable status."""
        base = workspace_root()
        current_root = Path(self.project_root).resolve()
        if current_root.name == "build":
            current_root = current_root.parent

        projects = []
        for p in sorted(base.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.name not in ("crack-story-chat-skill", "crack-design-mcp", "novel-ai-image-skill"):
                story_p = p / "story.md"
                build_p = p / "build"
                title = p.name
                premise = ""
                genre = ""
                if story_p.exists():
                    try:
                        stext = story_p.read_text(encoding="utf-8")
                        t_m = re.search(r"^-\s*Title:\s*(.+)$", stext, re.MULTILINE | re.IGNORECASE)
                        if t_m:
                            title = t_m.group(1).strip()
                        p_m = re.search(r"^-\s*(?:Premise|Logline|핵심 한 줄):\s*(.+)$", stext, re.MULTILINE | re.IGNORECASE)
                        if p_m:
                            premise = p_m.group(1).strip()
                        g_m = re.search(r"^-\s*(?:Tone / genre|장르):\s*(.+)$", stext, re.MULTILINE | re.IGNORECASE)
                        if g_m:
                            genre = g_m.group(1).strip()
                    except Exception:
                        pass
                has_safe = (build_p / "integrated-prompt-safe.md").exists() if build_p.is_dir() else False
                has_unsafe = (build_p / "integrated-prompt-unsafe.md").exists() if build_p.is_dir() else False
                playable = has_safe or has_unsafe
                projects.append({
                    "name": p.name,
                    "title": title,
                    "genre": genre,
                    "premise": premise,
                    "playable": playable,
                    "path": str(p),
                    "is_active": p.resolve() == current_root,
                })

        playable_count = sum(1 for p in projects if p["playable"])
        return {
            "instructions": "진입 시 사용자가 특정 프로젝트를 지정하지 않았다면, 위 목록 중 playable이 true인 프로젝트들을 사용자에게 안내하고 어떤 작품을 플레이할지 선택하도록 물어보세요.",
            "active_project": current_root.name,
            "total_count": len(projects),
            "playable_count": playable_count,
            "projects": projects,
        }

    def tool_switch_project(self, a: dict) -> dict:
        """Switch (enter) another Crack story project in the workspace."""
        p_name = a.get("project_name") or a.get("path")
        if not p_name:
            raise ValueError("project_name is required")

        target = Path(p_name)
        if not target.is_absolute():
            base = workspace_root()
            candidates = [contained(base, p_name), contained(base, Path(p_name).name)]
            found = next((c for c in candidates if c.is_dir()), None)
            if not found:
                raise ValueError(f"project '{p_name}' not found in {workspace_root()}")
            target = found

        build_dir = target / "build" if (target / "build").is_dir() else target
        self.project_root = str(build_dir)
        self.reload()
        return {
            "success": True,
            "active_project": target.name,
            "project_root": str(self.project_root),
            "message": f"성공적으로 '{target.name}' 프로젝트로 진입(전환)했습니다.",
        }

    def tool_delete_project(self, a: dict) -> dict:
        """Delete a project directory."""
        p_name = a.get("project_name")
        confirm = bool(a.get("confirm", False))
        if not p_name:
            raise ValueError("project_name is required")
        if not confirm:
            return {"success": False, "message": f"프로젝트 '{p_name}'를 삭제하려면 confirm=True 파라미터를 함께 전달하세요."}

        target = contained(workspace_root(), p_name)
        if not target.is_dir():
            raise ValueError(f"project '{p_name}' not found")

        import shutil
        shutil.rmtree(target)

        current_root = Path(self.project_root).resolve()
        if current_root.name == "build":
            current_root = current_root.parent
        if current_root == target.resolve():
            fallback = workspace_root() / "마왕성주식회사" / "build"
            if fallback.exists():
                self.project_root = str(fallback)
                self.reload()

        return {"success": True, "deleted_project": p_name}

    def tool_get_current_project(self, a: dict) -> dict:
        """Get information about the currently active/entered project."""
        current_root = Path(self.project_root).resolve()
        if current_root.name == "build":
            current_root = current_root.parent
        return {
            "active_project": current_root.name,
            "project_root": str(self.project_root),
            "title": self.project.name,
            "has_story": (current_root / "story.md").exists(),
            "has_characters": (current_root / "characters.md").exists(),
            "has_build": (current_root / "build").is_dir(),
        }

    def _db(self):
        from ..designer.db import CrackDatabase
        return CrackDatabase()

    def tool_import_project_from_md(self, a: dict) -> dict:
        """Import markdown project files into SQLite DB."""
        p_name = a.get("project_name")
        db = self._db()
        if p_name:
            target = Path(p_name)
            if not target.is_absolute():
                target = contained(workspace_root(), p_name)
        else:
            p = Path(self.project_root)
            target = p.parent if p.name == "build" else p
        return db.import_from_markdown(target)

    def tool_export_project_to_md(self, a: dict) -> dict:
        """Export project from SQLite DB to markdown files."""
        current_root = Path(self.project_root).resolve()
        p_name = a.get("project_name") or (current_root.parent.name if current_root.name == "build" else current_root.name)
        db = self._db()
        proj = db.get_project(p_name)
        if not proj:
            target = contained(workspace_root(), p_name)
            if target.is_dir():
                db.import_from_markdown(target)
                proj = db.get_project(p_name)
        if not proj:
            raise ValueError(f"project '{p_name}' not found in database")
        target_dir = a.get("target_dir")
        res = db.export_to_markdown(proj["id"], target_dir=target_dir)
        self.reload()
        return res

    def tool_export_project_zip(self, a: dict) -> dict:
        """Export project as a downloadable ZIP archive and return download links."""
        current_root = Path(self.project_root).resolve()
        p_name = a.get("project_name") or (current_root.parent.name if current_root.name == "build" else current_root.name)
        db = self._db()
        proj = db.get_project(p_name)
        if not proj:
            target = contained(workspace_root(), p_name)
            if target.is_dir():
                db.import_from_markdown(target)
                proj = db.get_project(p_name)
        if not proj:
            raise ValueError(f"project '{p_name}' not found in database")
        res = db.create_zip_archive(proj["id"])
        fn = res["filename"]
        res["local_download_url"] = f"http://127.0.0.1:8787/api/download/{fn}"
        # The remote URL used to be a hardcoded tunnel hostname, which quick
        # tunnels reissue on every restart — it had been pointing at a dead
        # host for a while. The caller knows the address it reached us on.
        res["download_path"] = f"/api/download/{fn}"

        # Inline the archive when asked. A download URL is useless to an agent
        # talking to a remote instance over MCP; base64 is how a project
        # actually crosses to another machine.
        if a.get("as_base64"):
            import base64
            raw = Path(res["zip_path"]).read_bytes()
            limit = int(a.get("max_bytes") or 8_000_000)
            if len(raw) > limit:
                res["zip_base64"] = None
                res["base64_error"] = (
                    f"ZIP 이 {len(raw)//1024}KB 로 한도 {limit//1024}KB 를 넘습니다. "
                    f"에셋을 빼거나 download_path 로 내려받으십시오.")
            else:
                res["zip_base64"] = base64.b64encode(raw).decode()
                res["base64_bytes"] = len(res["zip_base64"])
        return res

    def tool_sync_to_crack_draft(self, a: dict) -> dict:
        """Automate Crack web editor via Playwright: inject fields and click [임시저장] (DRAFT SAVE ONLY)."""
        from ..designer.crack_sync_draft import sync_project_to_draft
        payload = self.tool_inspect_sync_payload({})
        target_url = a.get("target_url") or "https://crack.wrtn.ai"
        headless = a.get("headless", True)
        return sync_project_to_draft(payload, target_url=target_url, headless=headless)

    def tool_create_start_set(self, a: dict) -> dict:
        """Create or update a start-set (multi-opening) with prologue, start-prompt, and metadata."""
        set_id = a.get("set_id")
        title = a.get("title")
        if not set_id or not title:
            raise ValueError("set_id and title are required")
        res = self._mgr().save_start_set(
            set_id=set_id,
            title=title,
            description=a.get("description", ""),
            order=int(a.get("order", 0)),
            is_default=bool(a.get("is_default", False)),
            prologue=a.get("prologue", ""),
            start_prompt=a.get("start_prompt", "")
        )
        self.reload()
        return res

    def tool_get_start_set(self, a: dict) -> dict:
        """Fetch a specific start-set by ID."""
        set_id = a.get("set_id")
        if not set_id:
            raise ValueError("set_id is required")
        res = self._mgr().get_start_set(set_id)
        if not res:
            raise ValueError(f"start-set '{set_id}' not found")
        return res

    def tool_delete_start_set(self, a: dict) -> dict:
        """Delete a start-set directory."""
        set_id = a.get("set_id")
        if not set_id:
            raise ValueError("set_id is required")
        res = self._mgr().delete_start_set(set_id)
        self.reload()
        return res

    def tool_save_artifact(self, a: dict) -> dict:
        """Save any publish or derived artifact (e.g. story-description.md, image-prompts.md)."""
        name = a.get("name")
        content = a.get("content")
        if not name or content is None:
            raise ValueError("name and content are required")
        res = self._mgr().save_artifact(name, content)
        self.reload()
        return res

    def tool_get_artifact(self, a: dict) -> dict:
        """Fetch the content of any publish or derived artifact."""
        name = a.get("name")
        if not name:
            raise ValueError("name is required")
        res = self._mgr().get_artifact(name)
        if not res:
            raise ValueError(f"artifact '{name}' not found")
        return res

    def tool_list_artifacts(self, a: dict) -> dict:
        """List all build and derived artifact files."""
        return {"artifacts": self._mgr().list_artifacts()}

    def tool_delete_artifact(self, a: dict) -> dict:
        """Delete a build or derived artifact file."""
        name = a.get("name")
        if not name:
            raise ValueError("name is required")
        res = self._mgr().delete_artifact(name)
        self.reload()
        return res

    def tool_inspect_sync_payload(self, a: dict) -> dict:
        """Inspect the full synchronization payload expected by Crack Studio / sync tool."""
        variant = a.get("variant") or self.variant or "safe"
        return self._mgr().get_sync_payload(variant=variant)

    def tool_list_shortcuts(self, a: dict) -> dict:
        """List all registered shortcuts in keyword-book.md."""
        return {"shortcuts": self._mgr().list_shortcuts()}

    def tool_update_shortcut(self, a: dict) -> dict:
        """Add or update a shortcut in keyword-book.md (name, description, prompt)."""
        name = a.get("name")
        description = a.get("description", "")
        prompt = a.get("prompt", "")
        if not name or not prompt:
            raise ValueError("name and prompt are required")
        res = self._mgr().update_shortcut(name=name, description=description, prompt=prompt, shortcut_id=a.get("shortcut_id"))
        self.reload()
        return res

    def tool_delete_shortcut(self, a: dict) -> dict:
        """Delete a shortcut from keyword-book.md."""
        name = a.get("name")
        if not name:
            raise ValueError("name is required")
        res = self._mgr().delete_shortcut(name=name)
        self.reload()
        return res

    def tool_write_main_prompt(self, a: dict) -> dict:
        """Write the entire integrated main prompt as a whole, measuring characters and alerting if > 7000."""
        content = a.get("content")
        if not content:
            raise ValueError("content is required")
        variant = (a.get("variant") or self.variant or "safe").strip().lower()
        return self.tool_update_prompt({"content": content, "target": "main", "variant": variant})

    def tool_write_opening(self, a: dict) -> dict:
        """Write prologue and/or start-prompt opening as a whole, alerting if > 1000 chars."""
        prologue = a.get("prologue")
        opening_situation = a.get("opening_situation")
        results = {}
        if prologue is not None:
            results["prologue"] = self.tool_update_prompt({"content": prologue, "target": "prologue"})
        if opening_situation is not None:
            results["opening_situation"] = self.tool_update_prompt({"content": opening_situation, "target": "opening_situation"})
        return {
            "success": True,
            "results": results,
        }
    def tool_update_prompt(self, a: dict) -> dict:
        content = a.get("content")
        if content is None:
            raise ValueError("content is required")
        variant = a.get("variant") or self.variant
        target = a.get("target") or "main"
        root = Path(self.project_root)

        if target == "main":
            target_path = root / f"integrated-prompt-{variant}.md"
            if not target_path.exists() and (root / "integrated-prompt.md").exists():
                target_path = root / "integrated-prompt.md"
            target_path.write_text(content, encoding="utf-8")
        elif target == "prologue":
            target_path = root / "prologue.md"
            target_path.write_text(content, encoding="utf-8")
        elif target == "opening_situation":
            target_path = root / "start-prompt.md"
            current_raw = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            contract_fence = ""
            if "```" in current_raw:
                idx = current_raw.find("```")
                contract_fence = "\n\n" + current_raw[idx:].strip()
            target_path.write_text(content.strip() + contract_fence, encoding="utf-8")
        else:
            raise ValueError(f"unknown target: {target}. Choose from main, prologue, opening_situation")

        self.reload()
        findings = lint(self.project)
        errors = [f for f in findings if f.get("severity") == "error"]
        limit = 7000 if target == "main" else 1000
        measured, cp, u16 = count_chars(content)
        needs_compression = measured > limit
        alert = None
        if needs_compression:
            alert = f"⚠️ [글자 수 초과 경고] {target} 분량이 {measured}자로 {limit}자 한도를 {measured - limit}자 초과했습니다! 파일에는 정상 저장되었으니 모델은 내용을 압축해주세요."

        return {
            "success": True,
            "target": target,
            "variant": variant,
            "saved_to": target_path.name,
            "chars": measured,
            "limit": limit,
            "needs_compression": needs_compression,
            "alert": alert,
            "lint_passed": len(errors) == 0,
            "lint_findings_count": len(findings),
        }

    def tool_list_keyword_entries(self, a: dict) -> dict:
        variant = a.get("variant") or self.variant
        entries = self.project.entries(variant)
        return {
            "variant": variant,
            "count": len(entries),
            "entries": [
                {
                    "title": e.title,
                    "keywords": e.keywords,
                    "chars": e.char_count,
                    "order": e.order,
                    "preview": (e.content[:80] + "...") if len(e.content) > 80 else e.content,
                }
                for e in entries
            ],
        }

    def tool_get_keyword_entry(self, a: dict) -> dict:
        title = a.get("title")
        if not title:
            raise ValueError("title is required")
        variant = a.get("variant") or self.variant
        clean_title = title.strip().lower()
        entry = next((e for e in self.project.entries(variant) if e.title.strip().lower() == clean_title), None)
        if not entry:
            raise ValueError(f"keyword entry not found: {title}")
        return {
            "variant": variant,
            "title": entry.title,
            "keywords": entry.keywords,
            "content": entry.content,
            "chars": entry.char_count,
            "order": entry.order,
        }

    def _kb_targets(self, variant: str) -> list[Path]:
        """Keyword-book files a variant refers to.

        SAFE and UNSAFE are separate books on purpose: adult material lives in
        one and not the other. A tool that ignores the variant and globs
        keyword-book*.md will carry an entry across that line.
        """
        root = Path(self.project_root)
        variant = (variant or "both").strip().lower()
        names = {"safe": ["keyword-book-safe.md"],
                 "unsafe": ["keyword-book-unsafe.md"],
                 "default": ["keyword-book.md"],
                 "both": ["keyword-book-safe.md", "keyword-book-unsafe.md",
                          "keyword-book.md"]}.get(variant)
        if names is None:
            raise ValueError(f"variant '{variant}' 는 safe, unsafe, default, both 중 하나여야 합니다")
        return [root / n for n in names]

    def tool_update_keyword_entry(self, a: dict) -> dict:
        title = a.get("title")
        keywords = a.get("keywords")
        content = a.get("content")
        if not title:
            raise ValueError("title is required")
        if keywords is None or not isinstance(keywords, list):
            raise ValueError("keywords must be a list of strings")
        keywords_only = bool(a.get("keywords_only"))
        if content is None and not keywords_only:
            raise ValueError("content is required (or pass keywords_only=true)")

        variant = a.get("variant") or "both"
        new_title = a.get("new_title")
        root = Path(self.project_root)

        targets = [p for p in self._kb_targets(variant) if p.exists()]
        if not targets:
            targets = [root / "keyword-book.md"]

        from ..emulator.parser import update_keyword_book_entry, read_keyword_book_entry

        # Editing with variant "both" must not create the entry in a book that
        # never had it — that is how adult material ends up in SAFE. A new
        # entry has to name the variant it belongs to.
        if variant == "both":
            present = [p for p in targets
                       if read_keyword_book_entry(p.read_text(encoding="utf-8"), title) is not None]
            if present:
                targets = present

        # SAFE and UNSAFE routinely carry different prose for the same entry,
        # and keyword-book.md is written no matter which variant was asked for.
        # Sending one body to all of them silently replaces the others' text,
        # which is a content loss the caller never asked for: trimming a trigger
        # list should not rewrite a sentence. Bodies that already differ from
        # the incoming content are reported and left alone unless the caller
        # says otherwise.
        current = {}
        for path in targets:
            if path.exists():
                current[path.name] = read_keyword_book_entry(
                    path.read_text(encoding="utf-8"), title)

        # The risk is one body being written over a *different* body in another
        # variant — not the ordinary case of editing text. So the guard fires
        # only when the target files disagree among themselves; rewriting the
        # entry when they all currently say the same thing is just an edit.
        existing = {n: b.strip() for n, b in current.items() if b is not None}
        divergent = sorted(existing) if len({*existing.values()}) > 1 else []
        overwrite = bool(a.get("overwrite_divergent"))
        if divergent and not overwrite and not keywords_only and content is not None:
            return {
                "ok": False,
                "error": "body_divergence",
                "title": title,
                "divergent_files": divergent,
                "message": (
                    f"{', '.join(divergent)} 의 본문이 넘긴 content 와 다릅니다. "
                    "그대로 쓰면 해당 변형의 문장이 덮어써집니다. "
                    "트리거만 고치려면 keywords_only=true, "
                    "정말 본문을 통일하려면 overwrite_divergent=true 를 주십시오."),
                "current_bodies": current,
            }

        updated_files = []
        is_update = False
        for path in targets:
            text = path.read_text(encoding="utf-8") if path.exists() else "# Keyword Book\n\n"
            body = current.get(path.name) if keywords_only else content
            if body is None:
                body = content or ""
            new_text, was_up = update_keyword_book_entry(text, title, keywords, body, new_title=new_title)
            path.write_text(new_text, encoding="utf-8")
            updated_files.append(path.name)
            is_update = is_update or was_up

        self.reload()

        warnings = []
        if len(keywords) < 1 or len(keywords) > 5:
            warnings.append(f"키워드 수가 {len(keywords)}개입니다. Crack 권장 한도는 1~5개입니다.")
        content_len = len(content.strip())
        if content_len > 400:
            warnings.append(f"항목 본문이 {content_len}자입니다. Crack 권장 한도는 400자 이하입니다.")

        needs_compression = content_len > 400
        alert = None
        if needs_compression:
            alert = f"⚠️ [글자 수 초과 경고] '{title}' 항목이 {content_len}자로 400자 한도를 {content_len - 400}자 초과했습니다! 파일에는 정상 저장되었으니 모델은 내용을 압축해주세요."

        return {
            "success": True,
            "action": "updated" if is_update else "created",
            "title": new_title or title,
            "keywords": keywords,
            "chars": content_len,
            "limit": 400,
            "needs_compression": needs_compression,
            "alert": alert,
            "updated_files": updated_files,
            "warnings": warnings,
        }


    def tool_edit_prompt_section(self, a: dict) -> dict:
        """Replace one section of a prompt instead of resending the whole file.

        update_prompt takes the entire document, so changing one line of a
        7,000-character prompt means retyping all of it — the surest way to
        lose something by accident. A section is addressed by its heading.
        """
        heading = (a.get("heading") or "").strip()
        if not heading:
            raise ValueError("heading is required (예: '## 이미지 출력 규칙')")
        content = a.get("content")
        if content is None:
            raise ValueError("content is required")
        target = (a.get("target") or "main").strip().lower()
        variant = self._resolve_variant(a.get("variant")) if a.get("variant") else self.variant
        root = Path(self.project_root)
        path = {"main": root / f"integrated-prompt-{variant}.md",
                "prologue": root / "prologue.md",
                "opening_situation": root / "start-prompt.md"}.get(target)
        if path is None:
            raise ValueError("target must be main, prologue or opening_situation")
        if not path.exists() and target == "main" and (root / "integrated-prompt.md").exists():
            path = root / "integrated-prompt.md"
        if not path.exists():
            raise ValueError(f"{path.name} 이 없습니다")

        text = path.read_text(encoding="utf-8")
        level = len(heading) - len(heading.lstrip("#"))
        pattern = re.compile(
            rf"^{re.escape(heading)}[ \t]*$.*?(?=^#{{1,{max(level, 1)}}} |\Z)",
            re.M | re.S)
        m = pattern.search(text)
        if not m:
            headings = re.findall(r"^#+ .*$", text, re.M)
            raise ValueError(f"'{heading}' 섹션을 찾지 못했습니다. 있는 헤딩: {headings}")

        body = content if content.startswith("#") else f"{heading}\n{content.strip()}\n"
        if not body.endswith("\n"):
            body += "\n"
        new_text = text[:m.start()] + body + text[m.end():]
        before = count_chars(text)[0]
        path.write_text(new_text, encoding="utf-8")
        self.reload()

        after, _cp, _u16 = count_chars(new_text)
        limit = 7000 if target == "main" else 1000
        return {"success": True, "target": target, "variant": variant,
                "heading": heading, "saved_to": path.name,
                "chars_before": before, "chars": after, "limit": limit,
                "needs_compression": after > limit,
                "alert": (f"⚠️ {after}자로 {limit}자 한도를 {after - limit}자 초과했습니다. "
                          f"파일에는 저장되었으니 압축해 주세요.") if after > limit else None,
                "replaced_chars": count_chars(m.group(0))[0]}

    def tool_list_prompt_sections(self, a: dict) -> dict:
        """Headings of a prompt with their sizes — what edit_prompt_section takes."""
        variant = self._resolve_variant(a.get("variant")) if a.get("variant") else self.variant
        text = self.project.prompt(variant)
        heads = [(m.start(), m.group(0)) for m in re.finditer(r"^#+ .*$", text, re.M)]
        out = []
        for i, (pos, h) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            out.append({"heading": h.strip(), "chars": count_chars(text[pos:end])[0]})
        return {"variant": variant, "total_chars": count_chars(text)[0],
                "limit": 7000, "sections": out}

    def tool_list_free_models(self, a: dict) -> dict:
        """OpenRouter models that cost nothing right now."""
        from ..emulator.llm import list_free_models, pick_free_model
        key = self.keys.get("openrouter")
        models = list_free_models(self.cfg, key)
        best = pick_free_model(self.cfg, key)
        return {"count": len(models), "models": models[:int(a.get("limit") or 20)],
                "recommended": best,
                "note": "무료 티어는 수시로 바뀝니다. 고정하지 말고 필요할 때 다시 조회하세요."}

    def tool_set_api_key(self, a: dict) -> dict:
        """Store a provider key for this server (file mode 600, never echoed)."""
        provider = (a.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError("provider is required (gemini, openrouter, openai 등)")
        key = (a.get("key") or "").strip()
        self.keys.set(provider, key)
        return {"provider": provider, "stored": bool(key),
                "providers_with_keys": sorted(self.keys.keys),
                "path": str(self.keys.path) if self.keys.path else None}

    def tool_import_project_zip(self, a: dict) -> dict:
        """Unpack a base64 ZIP into the workspace as a project.

        The counterpart to export_project_zip, and the piece that makes a
        remote instance usable: without it a project can leave the server but
        never arrive. Entries are checked against the destination before
        anything is written, so an archive carrying `../` or an absolute path
        cannot place a file outside the project it claims to be.
        """
        import base64, io, zipfile
        blob = a.get("zip_base64")
        if not blob:
            raise ValueError("zip_base64 is required")
        p_name = (a.get("project_name") or "").strip()
        if not p_name:
            raise ValueError("project_name is required")
        target = contained(workspace_root(), re.sub(r'[\\/:*?"<>|]', "_", p_name))
        if target.exists() and not a.get("overwrite"):
            raise ValueError(
                f"'{p_name}' 이 이미 있습니다. 덮어쓰려면 overwrite=true 를 주십시오.")

        try:
            raw = base64.b64decode(blob, validate=True)
        except Exception as exc:
            raise ValueError(f"base64 디코드 실패: {exc}") from exc

        written, skipped = [], []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # A zip may be wrapped in a single top directory; strip it so the
            # project does not end up one level deeper than it was exported.
            tops = {n.split("/", 1)[0] for n in names}
            strip = len(tops) == 1 and all("/" in n for n in names)
            for n in names:
                rel = n.split("/", 1)[1] if strip else n
                if not rel:
                    continue
                try:
                    dest = contained(target, rel)
                except PathEscape:
                    skipped.append(n)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
                written.append(rel)

        # The upload succeeded once the files are on disk. Switching to the new
        # project is a convenience on top, and an archive that is missing a
        # prompt fails to parse — reporting that as a failed upload would send
        # the caller to re-send the whole zip over something already done.
        switched, switch_error, previous = False, None, self.project_root
        if a.get("auto_switch", True):
            build = target / "build" if (target / "build").is_dir() else target
            try:
                self.project_root = str(build)
                self.reload()
                switched = True
            except Exception as exc:
                self.project_root = previous
                self.reload()
                switch_error = f"{type(exc).__name__}: {exc}"

        return {"success": True, "project": target.name, "path": str(target),
                "files_written": len(written), "files": written[:40],
                "rejected_paths": skipped,
                "switched": switched, "switch_error": switch_error,
                "active_project": Path(self.project_root).parent.name}

    def tool_get_template(self, a: dict) -> dict:
        t_name = a.get("template_name", "main_prompt")
        name = a.get("name", "인물 이름")
        title = a.get("title", "직책/이명")
        role = a.get("role", "역할")
        template_text = get_template(t_name, name=name, title=title, role=role)
        return {
            "template_name": t_name,
            "template": template_text,
            "instruction": "이 양식을 기반으로 세계관과 캐릭터에 맞는 창작 내용을 채워넣으세요.",
        }

    def tool_get_design_guide(self, a: dict) -> dict:
        topic = a.get("topic", "prompt_structure")
        guide_text = get_guide(topic)
        return {
            "topic": topic,
            "guide": guide_text,
        }

    def tool_audit_project(self, a: dict) -> dict:
        # Check parent if project_root is build/
        root_path = Path(self.project_root)
        if root_path.name == "build" and (root_path.parent / "story.md").exists():
            scan_root = root_path.parent
        else:
            scan_root = root_path
        report = run_audit_project(scan_root)
        return {
            "passed": report.passed,
            "summary": report.summary,
            "errors": report.errors,
            "warnings": report.warnings,
            "sections": report.sections,
        }

    def tool_batch_import_keywords(self, a: dict) -> dict:
        entries = a.get("entries", [])
        variant = (a.get("variant") or "both").strip().lower()
        if not entries or not isinstance(entries, list):
            raise ValueError("entries must be a non-empty list of keyword entry dicts")

        root = Path(self.project_root)
        targets = [p for p in self._kb_targets(variant) if p.exists()]
        if not targets:
            targets = [root / "keyword-book.md"]

        total_processed = 0
        created_count = 0
        updated_count = 0
        all_warnings = []

        for item in entries:
            title = item.get("title", "").strip()
            keywords = item.get("keywords", [])
            content = item.get("content", "").strip()
            new_title = item.get("new_title")

            if not title:
                continue

            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]

            # Format bullets if given as list
            if isinstance(item.get("bullets"), list):
                bullet_lines = [f"-{b.strip().lstrip('-').strip()}" for b in item["bullets"] if b.strip()]
                if bullet_lines:
                    bullets_joined = chr(10).join(bullet_lines)
                    content = bullets_joined if not content else (content + chr(10) + bullets_joined)
            # Length check
            measured, cp, u16 = count_chars(content)
            if measured > 400:
                all_warnings.append(f"{title}: {measured}/400자 ({measured - 400}자 초과)")
            if len(keywords) < 1 or len(keywords) > 5:
                all_warnings.append(f"{title}: 키워드 {len(keywords)}개 (1~5개 권장)")

            is_up = False
            for path in targets:
                text = path.read_text(encoding="utf-8") if path.exists() else "# Keyword Book\n\n"
                new_text, was_up = update_keyword_book_entry(text, title, keywords, content, new_title=new_title)
                path.write_text(new_text, encoding="utf-8")
                is_up = is_up or was_up

            total_processed += 1
            if is_up:
                updated_count += 1
            else:
                created_count += 1

        self.reload()
        return {
            "success": True,
            "total_processed": total_processed,
            "created": created_count,
            "updated": updated_count,
            "updated_files": [p.name for p in targets],
            "warnings": all_warnings,
        }
    def tool_delete_keyword_entry(self, a: dict) -> dict:
        title = a.get("title")
        if not title:
            raise ValueError("title is required")
        root = Path(self.project_root)

        from ..emulator.parser import delete_keyword_book_entry
        # The variant was accepted in the schema and then ignored: every book
        # got globbed, so deleting from SAFE also deleted from UNSAFE.
        targets = [p for p in self._kb_targets(a.get("variant") or "both") if p.exists()]
        if not targets:
            targets = [root / "keyword-book.md"]

        deleted_from = []
        for path in targets:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            new_text, was_del = delete_keyword_book_entry(text, title)
            if was_del:
                path.write_text(new_text, encoding="utf-8")
                deleted_from.append(path.name)

        self.reload()
        return {
            "deleted": bool(deleted_from),
            "title": title,
            "deleted_from": deleted_from,
        }

    TOOLS: dict[str, dict[str, Any]] = {}

    # ── JSON-RPC ──────────────────────────────────────────────────
    def handle(self, req: dict) -> dict | None:
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "crack-emu", "version": "0.1.0"},
                "instructions": (
                    "Crack 스토리 챗 프로젝트 설계 및 플레이 테스트 에뮬레이터입니다. "
                    "대화 시작 시 또는 사용자가 특정 프로젝트를 지정하지 않고 진입했다면, "
                    "반드시 가장 먼저 `list_projects`를 호출하여 현재 작업공간에서 플레이 가능한 프로젝트 목록(이름, 제목, 장르, 개요)을 "
                    "사용자에게 보여주고 어떤 프로젝트를 플레이할지 먼저 선택하도록 질문하세요. "
                    "사용자가 특정 프로젝트를 선택하면 `start_session(session=..., project_name=...)` 또는 `switch_project(project_name=...)`으로 "
                    "해당 프로젝트 세션을 시작하고 `play_turn`으로 플레이를 진행하세요. "
                    "play_turn 응답의 findings는 출력 계약 위반이고, dropped는 3슬롯 초과 탈락 키워드입니다."),
            })
        if method == "ping":
            return self._ok(rid, {})
        if method == "prompts/list":
            return self._ok(rid, {
                "prompts": [
                    {
                        "name": "crack_story_design",
                        "description": "Crack 스토리 챗 프로젝트 설계, 프롬프트 통작성, JSON 키워드북 입력, 종합 감사 및 테스트 스킬 워크플로우",
                        "arguments": [
                            {"name": "topic", "description": "특정 안내 주제 (writing_tips, prompt_structure, character_schema, keyword_rules, opening_design, output_contract, story_craft, content_variants, image_prompts)", "required": False}
                        ]
                    },
                    {
                        "name": "writing_tips",
                        "description": "4단계 반복 압축 루프(Squeeze Loop) 및 전역 공유 기호(Global Shared Syntax) 작성 팁",
                    },
                    {
                        "name": "character_crafting",
                        "description": "Crack 인물 3분리 원칙(신체/심리/관찰행동) 및 4단 동적 호칭 벡터 가이드",
                    },
                    {
                        "name": "keyword_topology",
                        "description": "3슬롯 Starvation 방지, 상시 인물 단독 트리거 금지 및 심층 키워드북 설계 가이드",
                    },
                    {
                        "name": "review_and_audit",
                        "description": "Crack 프로젝트 7,000자 규격, UTF-16 카운트, 4부 출력 계약 및 종합 감사 가이드",
                    }
                ]
            })
        if method == "prompts/get":
            params = req.get("params") or {}
            p_name = params.get("name", "crack_story_design")
            topic = (params.get("arguments") or {}).get("topic") or ""

            if p_name == "writing_tips":
                content = get_guide("writing_tips")
            elif p_name == "character_crafting":
                content = get_guide("character_schema")
            elif p_name == "keyword_topology":
                content = get_guide("keyword_rules")
            elif p_name == "review_and_audit":
                content = get_guide("output_contract") + "\n\n" + get_guide("prompt_structure")
            elif topic:
                content = get_guide(topic)
            else:
                skill_text = Path(__file__).resolve().parent.parent.parent / "SKILL.md"
                content = skill_text.read_text(encoding="utf-8") if skill_text.exists() else "Crack Design Skill"

            return self._ok(rid, {
                "description": f"Skill prompt for {p_name}",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": content}
                    }
                ]
            })
        if method == "tools/list":
            return self._ok(rid, {"tools": list(TOOL_SCHEMAS.values())})
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            fn: Callable | None = getattr(self, f"tool_{name}", None)
            if fn is None:
                return self._err(rid, -32601, f"unknown tool: {name}")
            try:
                result = fn(args)
                return self._ok(rid, {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": False,
                })
            except Exception as e:
                return self._ok(rid, {
                    "content": [{"type": "text",
                                 "text": f"{type(e).__name__}: {e}"}],
                    "isError": True,
                })
        if rid is None:
            return None
        return self._err(rid, -32601, f"unknown method: {method}")

    @staticmethod
    def _ok(rid, result) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code, message) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def run(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                resp = self.handle(req)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                resp = self._err(req.get("id"), -32603, "internal error")
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        return 0


def _s(desc: str, **props) -> dict:
    cleaned = {}
    required = []
    for k, v in props.items():
        v_copy = dict(v)
        if v_copy.pop("_required", False):
            required.append(k)
        cleaned[k] = v_copy
    return {"description": desc,
            "inputSchema": {"type": "object", "properties": cleaned,
                            "required": required}}


_SESSION = {"type": "string", "description": "세션 id", "_required": True}
_MODEL = {"type": "string", "description": "모델 id (생략 시 기본값)"}
_PROVIDER = {"type": "string", "description": "ollama | openrouter | gemini | openai | echo | agent"}

TOOL_SCHEMAS = {

    "list_prompt_sections": {"name": "list_prompt_sections", **_s(
        "프롬프트의 헤딩 목록과 섹션별 글자 수를 조회합니다. edit_prompt_section 에 넣을 heading 을 여기서 확인합니다.",
        variant={"type": "string", "description": "safe | unsafe | default"})},
    "edit_prompt_section": {"name": "edit_prompt_section", **_s(
        "프롬프트의 특정 섹션만 교체합니다. 전문을 다시 보내지 않아도 되므로 한 줄 수정에 안전합니다.",
        heading={"type": "string", "description": "교체할 섹션 헤딩 (예: '## 이미지 출력 규칙')", "_required": True},
        content={"type": "string", "description": "새 섹션 내용. 헤딩을 포함하지 않으면 기존 헤딩을 유지합니다", "_required": True},
        target={"type": "string", "description": "main | prologue | opening_situation (기본 main)"},
        variant={"type": "string", "description": "safe | unsafe | default"})},
    "list_free_models": {"name": "list_free_models", **_s(
        "OpenRouter 에서 현재 무료로 쓸 수 있는 모델을 조회하고, 이 하네스의 프롬프트 길이를 감당할 만한 것을 추천합니다.",
        limit={"type": "integer", "description": "반환 개수 (기본 20)"})},
    "set_api_key": {"name": "set_api_key", **_s(
        "프로바이더 API 키를 이 서버에 저장합니다. 파일 권한 600 으로 저장되며 응답에 키를 다시 싣지 않습니다. "
        "빈 문자열을 주면 삭제합니다.",
        provider={"type": "string", "description": "gemini | openrouter | openai", "_required": True},
        key={"type": "string", "description": "API 키. 빈 값이면 삭제"})},
    "import_project_zip": {"name": "import_project_zip", **_s(
        "base64 로 인코딩한 ZIP 을 워크스페이스에 풀어 프로젝트로 등록합니다. export_project_zip 의 짝으로, "
        "원격 서버에 작품을 올릴 때 씁니다.",
        zip_base64={"type": "string", "description": "ZIP 파일의 base64 문자열", "_required": True},
        project_name={"type": "string", "description": "생성할 프로젝트 디렉터리 이름", "_required": True},
        overwrite={"type": "boolean", "description": "기존 프로젝트를 덮어쓸지 (기본 false)"},
        auto_switch={"type": "boolean", "description": "업로드 후 해당 프로젝트로 전환 (기본 true)"})},
    "get_template": {"name": "get_template", **_s(
        "메인 프롬프트(main_prompt), 캐릭터(character), 프롤로그(prologue), 시작상황(start_prompt)의 표준 뼈대 양식을 불러옵니다. 모델은 이 양식을 받아 창작 내용을 채워넣으면 됩니다.",
        template_name={"type": "string", "description": "main_prompt | character | prologue | start_prompt", "_required": True},
        name={"type": "string", "description": "캐릭터 양식 호출 시 캐릭터 이름"},
        title={"type": "string", "description": "캐릭터 양식 호출 시 직책/이명"},
        role={"type": "string", "description": "캐릭터 양식 호출 시 역할"})},
    "get_design_guide": {"name": "get_design_guide", **_s(
        "Crack 설계 및 작성 팁 가이드(압축 루프, 프롬프트 구조, 캐릭터 3분리, 3슬롯 키워드북, 오프닝 7단계, 4부 출력 계약, 서사 갈등 엔진, SAFE/UNSAFE 프로필, 이미지 프롬프트)를 조회합니다.",
        topic={"type": "string", "description": "writing_tips | prompt_structure | character_schema | keyword_rules | opening_design | output_contract | story_craft | content_variants | image_prompts", "_required": True})},
    "audit_project": {"name": "audit_project", **_s(
        "프로젝트 전체(글자 수 한도, SAFE/UNSAFE 헤딩 일치, 미정의 기호/이모지, 폐기된 네임스페이스, 키워드북 1~5개 및 400자 한도, 3슬롯 충돌)를 일괄 감사하고 상세 보고서를 반환합니다.")},
    "batch_import_keywords": {"name": "batch_import_keywords", **_s(
        "JSON 포맷으로 여러 키워드북 항목을 한 번에 입력하여 Markdown 파일에 자동으로 포맷팅하여 엔트리시킵니다. 400자 한도 및 1~5개 키워드를 검증합니다.",
        entries={"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string", "description": "항목 제목"},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "키워드 목록 (1~5개)"},
            "content": {"type": "string", "description": "항목 본문"},
            "bullets": {"type": "array", "items": {"type": "string"}, "description": "(선택) 불릿 목록으로 입력 시 자동 포맷팅"}
        }, "required": ["title", "keywords"]}, "description": "등록할 키워드 항목 목록", "_required": True},
        variant={"type": "string", "description": "safe | unsafe | both (기본값 both)"})},
    "describe_project": {"name": "describe_project", **_s(
        "빌드 구조와 이 빌드에서 유도한 출력 계약을 반환합니다. project_name을 넘기면 해당 프로젝트로 즉시 전환 후 조회합니다.",
        project_name={"type": "string", "description": "조회 및 전환할 프로젝트 이름 (예: convenience, god, redhood, turtlesoup, 공순이, 마왕성주식회사 등)"})},
    "lint_build": {"name": "lint_build", **_s(
        "모델 호출 없이 빌드 자체를 검사합니다(항목 한도, 키워드 오발동, 슬롯 배치).")},
    "list_start_sets": {"name": "list_start_sets", **_s("선택 가능한 시작 세트 목록.")},
    "use_start_set": {"name": "use_start_set", **_s(
        "고른 시작 세트를 build/ 에 반영합니다.",
        start_set={"type": "string", "description": "세트 id", "_required": True})},
    "start_session": {"name": "start_session", **_s(
        "플레이 세션을 만들고 프롤로그를 첫 턴으로 넣습니다. project_name을 지정하면 해당 프로젝트로 전환 후 시작합니다.",
        session=_SESSION,
        project_name={"type": "string", "description": "플레이할 프로젝트 이름 (예: convenience, god, redhood, turtlesoup, 공순이, 마왕성주식회사 등)"},
        start_set={"type": "string", "description": "시작 세트 id"},
        variant={"type": "string", "description": "safe | unsafe | default"},
        persona_name={"type": "string"}, persona_body={"type": "string"},
        user_note={"type": "string", "description": "매 턴 최하단에 주입되는 지침"},
        goal={"type": "string"},
        overwrite={"type": "boolean", "description": "기존 세션을 지우고 새로 시작"})},
    "play_turn": {"name": "play_turn", **_s(
        "한 턴을 진행하고 응답·계약 위반·발동/드롭된 키워드북 항목을 함께 반환합니다. "
        "reply 인자를 주면 외부 API 호출 없이 모델/에이전트가 직접 작성한 롤플레이 응답을 세션에 반영하고 계약 검사를 수행합니다.",
        session=_SESSION,
        input={"type": "string", "description": "플레이어 입력", "_required": True},
        reply={"type": "string", "description": "외부 API 대신 모델/에이전트가 직접 생성한 응답 텍스트"},
        provider=_PROVIDER, model=_MODEL,
        variant={"type": "string"},
        window_turns={"type": "integer", "description": "컨텍스트 창(턴)"},
        scan_turns={"type": "integer", "description": "키워드 스캔 깊이(턴)"})},

    "get_tester_context": {"name": "get_tester_context", **_s(
        "시뮬레이션/리플라이 모드에서 모델이 모든 설정을 다 알아서 생기는 '지식 누출(Omniscience)'을 방지하기 위해, 플레이어 페르소나(순수 무지 상태) 또는 서술자 페르소나(3개 활성 키워드만 주입된 상태)를 엄격히 분리한 전용 프롬프팅 컨텍스트를 제공합니다.",
        session=_SESSION,
        role={"type": "string", "description": "player (순수 무지 플레이어 관점) | narrator (3슬롯 격리 서술자 관점)", "_required": True},
        input={"type": "string", "description": "role이 narrator일 때 플레이어의 입력 텍스트"})},
    "inspect_prompt": {"name": "inspect_prompt", **_s(
        "모델 호출 없이, 이 입력이면 실제로 무엇이 전송되는지 보여줍니다.",
        session=_SESSION, input={"type": "string"},
        window_turns={"type": "integer"}, scan_turns={"type": "integer"})},
    "check_response": {"name": "check_response", **_s(
        "임의의 응답 텍스트를 이 빌드의 출력 계약에 대조합니다.",
        text={"type": "string", "_required": True},
        input={"type": "string", "description": "그 응답을 만든 플레이어 입력"},
        session={"type": "string"})},
    "get_session": {"name": "get_session", **_s("세션 전체 기록.", session=_SESSION)},
    "set_goal": {"name": "set_goal", **_s(
        "[주어진 목표] 블록을 직접 씁니다. 이 블록은 평소 장기메모리로서 모델이 갱신하지만, "
        "여기서 설정하면 잠겨서 이후 자동 갱신이 덮어쓰지 않습니다.",
        session=_SESSION,
        goal={"type": "string", "description": "설정할 목표 문장"},
        mode={"type": "string", "description": "replace(기본) | append — append 는 기존 목표에 줄을 추가"},
        unlock={"type": "boolean", "description": "true 면 잠금을 풀어 모델이 다시 갱신하도록 되돌립니다"})},
    "set_session_meta": {"name": "set_session_meta", **_s(
        "진행 중인 세션의 페르소나·유저노트·목표를 세션을 버리지 않고 수정합니다.",
        session=_SESSION,
        persona_name={"type": "string"}, persona_body={"type": "string"},
        user_note={"type": "string", "description": "매 턴 최하단 <system_note> 에 주입되는 지침"},
        goal={"type": "string", "description": "[주어진 목표] (설정 시 잠김)"})},
    "reorder_keyword_entries": {"name": "reorder_keyword_entries", **_s(
        "키워드북 항목 순서를 바꿉니다. 문서 순서가 곧 3슬롯 우선순위(상단 우선)이므로, "
        "계속 드롭되는 항목을 위로 올릴 때 씁니다. 지정하지 않은 항목은 뒤에 원래 순서로 남습니다.",
        order={"type": "array", "items": {"type": "string"},
               "description": "맨 위로 올릴 항목 제목들 (지정한 순서대로)", "_required": True},
        variant={"type": "string", "description": "safe | unsafe | both (기본값 both)"})},
    "list_sessions": {"name": "list_sessions", **_s(
        "현재 활성 프로젝트에 속한 세션 목록만 조회합니다. 세션 파일에 기록된 "
        "project_root 로 걸러지므로 다른 작품의 세션은 나타나지 않습니다.",
        all_projects={"type": "boolean", "description": "true 면 프로젝트 구분 없이 스토어 전체를 조회"})},
    "delete_session": {"name": "delete_session", **_s(
        "세션과 발동 로그를 삭제합니다.", session=_SESSION)},
    "get_memory": {"name": "get_memory", **_s(
        "요약메모리·관계도·장기기억 슬롯의 내용과 밀려난 턴을 봅니다.",
        session=_SESSION, window_turns={"type": "integer"},
        recall={"type": "string", "description": "recent | lexical | manual"})},
    "set_memory": {"name": "set_memory", **_s(
        "메모리 슬롯을 직접 씁니다.",
        session=_SESSION,
        summaries={"type": "array", "items": {"type": "string"}},
        relations={"type": "array", "items": {"type": "string"}},
        recalled={"type": "array", "items": {"type": "string"}})},
    "activation_report": {"name": "activation_report", **_s(
        "여러 턴의 발동 로그를 모아 슬롯 초과·미발동·상시발동 항목을 집계합니다. "
        "한 턴만 봐서는 보이지 않는 문제가 여기서 나옵니다.",
        sessions={"type": "array", "items": {"type": "string"}},
        variant={"type": "string"},
        min_turns={"type": "integer", "description": "미발동 판정에 필요한 최소 턴 수"})},
    "get_prompt": {"name": "get_prompt", **_s(
        "메인 프롬프트(integrated-prompt), 프롤로그, 오프닝 상황의 내용과 글자 수를 조회합니다.",
        variant={"type": "string", "description": "safe | unsafe | default"},
        target={"type": "string", "description": "main | prologue | opening_situation | all (기본값 all)"})},


"list_projects": {"name": "list_projects", **_s(
        "사용자의 전체 Crack 스토리 프로젝트 목록을 조회하고, 현재 어느 프로젝트에 진입해 있는지 확인합니다.")},
    "switch_project": {"name": "switch_project", **_s(
        "작업할 스토리 프로젝트로 진입(전환)합니다. 이후 모든 프롬프트 작성, 키워드북 수정, 시뮬레이션이 해당 프로젝트에서 수행됩니다.",
        project_name={"type": "string", "description": "진입할 프로젝트 이름 (예: convenience, god, 마왕성주식회사 등)", "_required": True})},
    "delete_project": {"name": "delete_project", **_s(
        "프로젝트 디렉터리를 영구 삭제합니다 (confirm=true 필수).",
        project_name={"type": "string", "description": "삭제할 프로젝트 이름", "_required": True},
        confirm={"type": "boolean", "description": "삭제 확인 플래그 (true 필수)", "_required": True})},
    "get_current_project": {"name": "get_current_project", **_s(
        "현재 진입(활성화)되어 있는 프로젝트의 이름과 경로, 상태를 확인합니다.")},
    "create_project": {"name": "create_project", **_s(
        "새로운 Crack 스토리-챗 프로젝트 디렉터리와 기본 마크다운 소스 파일(story.md, characters.md, build/)을 "
        "생성합니다. 위치는 항상 워크스페이스(CRACK_WORKSPACE, 기본 ~/crack) 아래이며 임의 경로는 받지 않습니다.",
        project_name={"type": "string", "description": "디렉터리 이름 (생략 시 title 사용)"},
        auto_switch={"type": "boolean", "description": "생성 후 해당 프로젝트로 전환 (기본 true)"},
        title={"type": "string", "description": "스토리 제목", "_required": True},
        premise={"type": "string", "description": "로그라인/핵심 시놉시스", "_required": True},
        player_role={"type": "string", "description": "플레이어의 역할/신분"})},
    "create_start_set": {"name": "create_start_set", **_s(
        "시작 상황(오프닝 분기) 세트를 생성하거나 수정합니다. 메타데이터(순서, 기본여부)와 프롤로그, 시작 프롬프트를 함께 저장합니다.",
        set_id={"type": "string", "description": "세트 디렉터리 ID (예: 01_product, 02_rd)", "_required": True},
        title={"type": "string", "description": "시작 상황 타이틀", "_required": True},
        description={"type": "string", "description": "시작 상황 설명"},
        order={"type": "integer", "description": "정렬 순서 (0, 1, 2...)"},
        is_default={"type": "boolean", "description": "기본 오프닝 여부 (true 시 루트 build 파일에도 동기화)"},
        prologue={"type": "string", "description": "해당 시작 상황 전용 프롤로그 지문"},
        start_prompt={"type": "string", "description": "해당 시작 상황 전용 시작 프롬프트 지문"})},
    "get_start_set": {"name": "get_start_set", **_s(
        "특정 시작 상황(세트 ID)의 메타데이터와 프롤로그, 시작 프롬프트 전문을 조회합니다.",
        set_id={"type": "string", "description": "세트 ID", "_required": True})},
    "delete_start_set": {"name": "delete_start_set", **_s(
        "특정 시작 상황(세트 ID) 디렉터리를 삭제합니다.",
        set_id={"type": "string", "description": "삭제할 세트 ID", "_required": True})},
    "save_artifact": {"name": "save_artifact", **_s(
        "빌드 산출물 및 파생물(story-description.md, summary-comment.md, image-prompts.md 등)을 저장/수정합니다.",
        name={"type": "string", "description": "산출물 파일명 (예: story-description.md)", "_required": True},
        content={"type": "string", "description": "저장할 파일 내용", "_required": True})},
    "get_artifact": {"name": "get_artifact", **_s(
        "빌드 산출물 및 파생물(story-description.md, image-prompts.md 등)의 내용을 조회합니다.",
        name={"type": "string", "description": "산출물 파일명", "_required": True})},
    "list_artifacts": {"name": "list_artifacts", **_s(
        "빌드 및 파생물 산출물 파일 전체 목록을 조회합니다.")},
    "delete_artifact": {"name": "delete_artifact", **_s(
        "특정 산출물 파일을 삭제합니다.",
        name={"type": "string", "description": "삭제할 산출물 파일명", "_required": True})},
    "inspect_sync_payload": {"name": "inspect_sync_payload", **_s(
        "Crack Studio(웹 에디터) 동기화 도구(crack_sync.py)에 주입되는 전체 산출물 데이터(제목, 한줄소개, 프롬프트, 키워드북, 단축어, 시작세트, 발행설명)를 사전에 종합 검토합니다.",
        variant={"type": "string", "description": "safe | unsafe (기본값 safe)"})},
    "list_shortcuts": {"name": "list_shortcuts", **_s(
        "등록된 단축어(/명령어) 목록을 조회합니다.")},
    "update_shortcut": {"name": "update_shortcut", **_s(
        "단축어를 신규 등록하거나 수정합니다.",
        name={"type": "string", "description": "단축어 이름 (예: 상태창, /status)", "_required": True},
        description={"type": "string", "description": "단축어 설명"},
        prompt={"type": "string", "description": "단축어 실행 시 모델에 주입될 프롬프트", "_required": True},
        shortcut_id={"type": "string", "description": "슬래시 커맨드 ID (예: /status)"})},
    "delete_shortcut": {"name": "delete_shortcut", **_s(
        "특정 단축어를 삭제합니다.",
        name={"type": "string", "description": "삭제할 단축어 이름 또는 ID", "_required": True})},
    "write_main_prompt": {"name": "write_main_prompt", **_s(
        "메인 프롬프트(통합 프롬프트) 전문을 통으로 작성하여 저장합니다. 로직이 UTF-16 글자수를 측정하고 7,000자 초과 시 압축 알림을 반환합니다.",
        content={"type": "string", "description": "작성한 메인 프롬프트 전문 (통으로 입력)", "_required": True},
        variant={"type": "string", "description": "safe | unsafe (기본값 safe)"})},
    "write_opening": {"name": "write_opening", **_s(
        "프롤로그 및 시작 상황 지문을 통으로 작성하여 저장합니다. 각 항목당 1,000자 한도를 측정하고 초과 시 압축 알림을 반환합니다.",
        prologue={"type": "string", "description": "프롤로그 소설 지문 전문 (1,000자 이하)"},
        opening_situation={"type": "string", "description": "첫 턴 부트스트랩 및 시작 상황 지문 전문 (1,000자 이하)"})},
    "update_prompt": {"name": "update_prompt", **_s(
        "메인 프롬프트, 프롤로그, 오프닝 상황을 수정하고 프로젝트를 리로드합니다.",
        content={"type": "string", "description": "수정할 내용 텍스트", "_required": True},
        variant={"type": "string", "description": "safe | unsafe | default"},
        target={"type": "string", "description": "main | prologue | opening_situation (기본값 main)"})},
    "list_keyword_entries": {"name": "list_keyword_entries", **_s(
        "키워드북의 전체 항목 목록(제목, 키워드 목록, 글자 수, 순서)을 조회합니다.",
        variant={"type": "string", "description": "safe | unsafe | default"})},
    "get_keyword_entry": {"name": "get_keyword_entry", **_s(
        "특정 제목의 키워드북 항목 상세(트리거 키워드, 내용 전문, 글자 수)를 조회합니다.",
        title={"type": "string", "description": "항목 제목", "_required": True},
        variant={"type": "string", "description": "safe | unsafe | default"})},
    "update_keyword_entry": {"name": "update_keyword_entry", **_s(
        "키워드북 항목을 신규 등록하거나 수정합니다. 400자 한도와 키워드 1~5개 규칙을 검증합니다.",
        title={"type": "string", "description": "항목 제목 (기존 제목)", "_required": True},
        keywords={"type": "array", "items": {"type": "string"}, "description": "발동 트리거 키워드 목록 (1~5개)", "_required": True},
        content={"type": "string", "description": "항목 본문 내용 (400자 이하 권장). keywords_only=true 면 생략 가능", "_required": True},
        keywords_only={"type": "boolean", "description": "트리거 키워드만 교체하고 각 변형의 본문은 그대로 둡니다"},
        overwrite_divergent={"type": "boolean", "description": "변형마다 본문이 다를 때도 넘긴 content 로 통일합니다"},
        new_title={"type": "string", "description": "제목을 변경할 경우 새 제목"},
        variant={"type": "string", "description": "safe | unsafe | both (기본값 both)"})},
    "delete_keyword_entry": {"name": "delete_keyword_entry", **_s(
        "키워드북에서 지정한 제목의 항목을 삭제하고 프로젝트를 리로드합니다.",
        title={"type": "string", "description": "삭제할 항목 제목", "_required": True},
        variant={"type": "string", "description": "safe | unsafe | both (기본값 both)"})},
    "reload_project": {"name": "reload_project", **_s(
        "디스크의 빌드 파일을 다시 읽어 메모리의 프로젝트 및 키워드북을 즉시 갱신합니다.")},
    "import_project_from_md": {"name": "import_project_from_md", **_s(
        "마크다운 디렉터리(story.md, characters.md, build/)의 내용을 파싱하여 SQLite DB(crack.db)로 안전하게 임포트합니다.",
        project_name={"type": "string", "description": "임포트할 프로젝트 이름 또는 경로 (생략 시 현재 활성 프로젝트)"})},
    "export_project_to_md": {"name": "export_project_to_md", **_s(
        "SQLite DB(crack.db)에 저장된 프로젝트의 프롬프트, 키워드북, 시작세트 등을 5대 마크다운 파일셋으로 즉시 렌더링하여 디렉터리에 내보냅니다.",
        project_name={"type": "string", "description": "내보낼 프로젝트 이름 (생략 시 현재 활성 프로젝트)"},
        target_dir={"type": "string", "description": "내보낼 대상 디렉터리 경로 (생략 시 원본 프로젝트 경로)"})},
    "export_project_zip": {"name": "export_project_zip", **_s(
        "빌드 산출물(build/)을 ZIP 으로 패키징합니다. as_base64=true 면 내용을 base64 로 함께 반환하므로 "
        "원격 인스턴스에 import_project_zip 으로 그대로 넘길 수 있습니다. 프로젝트 루트의 원본 이미지 폴더는 포함하지 않습니다.",
        project_name={"type": "string", "description": "ZIP으로 압축할 프로젝트 이름 (생략 시 현재 활성 프로젝트)"},
        as_base64={"type": "boolean", "description": "ZIP 내용을 base64 문자열로 함께 반환"},
        max_bytes={"type": "integer", "description": "base64 로 실어 보낼 최대 원본 크기 (기본 8MB)"})},
    "sync_to_crack_draft": {"name": "sync_to_crack_draft", **_s(
        "Playwright 브라우저 자동화를 실행하여 크랙(Crack) 웹 에디터에 제목/한줄소개/프롬프트/키워드북을 자동 주입하고 [임시저장(Draft Save)] 버튼만 안전하게 클릭합니다. (최종 발행은 절대 진행하지 않음)",
        target_url={"type": "string", "description": "크랙 에디터 또는 로그인 페이지 URL (기본값 https://crack.wrtn.ai)"},
        headless={"type": "boolean", "description": "백그라운드 실행 여부 (true: 백그라운드, false: 브라우저 화면 표시)"})},
}


def _tool_list_start_sets(self, a: dict) -> dict:
    return {"start_sets": [
        {"id": x.id, "title": x.title, "description": x.description,
         "default": x.is_default, "generated": x.generated, "source": x.source,
         "prologue_chars": len(x.prologue), "opening_chars": len(x.opening_situation)}
        for x in self.project.start_sets]}


Server.tool_list_start_sets = _tool_list_start_sets


def main(project: str, store: str | None = None, spec: str | None = None,
         provider: str | None = None, variant: str = "safe",
         key_file: str | None = None) -> int:
    return Server(project, store, spec, provider, variant, key_file).run()

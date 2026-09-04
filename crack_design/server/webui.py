"""Local web UI. Stdlib http.server only — no framework, no extra dependency.

Binds to localhost by default: the provider API key stays in this process's
environment and is never sent to the browser.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote, quote

from ..emulator import memory, qa
from ..emulator.config import Config
from ..emulator.engine import Engine
from ..emulator.llm import make_client
from ..emulator.log import ActivationLog
from ..emulator.models import Session, Turn
from ..emulator.parser import lint, load_project
from ..emulator.session import Store
from ..config import workspace_root, exports_dir

STATIC = Path(__file__).resolve().parent.parent / "static"


class KeyStore:
    """Provider keys for the running server.

    In memory by default, which means a restart loses them. `--key-file` opts
    into writing them to a file outside the repository with owner-only
    permissions, because reloading the server is a normal part of iterating and
    retyping a key every time invites pasting it somewhere worse.
    """

    def __init__(self, path: str | None = None):
        self.path = Path(path).expanduser() if path else None
        self.keys: dict[str, str] = {}
        if self.path and self.path.is_file():
            try:
                self.keys = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.keys = {}

    def get(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def __contains__(self, provider: str) -> bool:
        return provider in self.keys

    def set(self, provider: str, key: str) -> None:
        if key:
            self.keys[provider] = key
        else:
            self.keys.pop(provider, None)
        self._flush()

    def _flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.keys, ensure_ascii=False), encoding="utf-8")
        os.chmod(self.path, 0o600)

    @property
    def persistent(self) -> bool:
        return self.path is not None


class State:
    """Everything the handler needs, rebuilt when settings change."""

    def __init__(self, project_root: str, spec: str | None, store: str | None,
                 key_file: str | None = None):
        self.project_root = project_root
        self.spec = spec
        self.store = Store(store)
        self.base_cfg = Config.load(spec)
        self.project = load_project(project_root)
        self.lock = threading.Lock()
        # Never sent to a client, never logged.
        self.keys = KeyStore(key_file)

    def reload(self, new_root: str | None = None) -> None:
        with self.lock:
            if new_root:
                self.project_root = new_root
            self.project = load_project(self.project_root)
            self.base_cfg = Config.load(self.spec)

    def config(self, overrides: dict) -> Config:
        return self.base_cfg.override(overrides)

    def engine(self, overrides: dict, variant: str) -> Engine:
        cfg = self.config(overrides)
        provider = overrides.get("llm.provider") or cfg.get("llm.provider")
        client = make_client(cfg, provider, api_key=self.keys.get(provider))
        return Engine(self.project_root, cfg, client, self.store, variant=variant)

    def key_status(self) -> dict:
        """Per provider: whether a key is available, and where it came from."""
        out = {}
        for name, preset in (self.base_cfg.get("providers") or {}).items():
            env = preset.get("api_key_env")
            out[name] = {
                "required": bool(env),
                "env_var": env,
                "from_ui": name in self.keys,
                "persistent": self.keys.persistent,
                "from_env": bool(env and os.environ.get(env)),
            }
        return out


def _overrides(body: dict) -> dict:
    """Map the UI's flat settings object onto dotted spec keys."""
    s = body.get("settings") or {}
    out = {
        "fidelity": s.get("fidelity"),
        "context.window_turns": s.get("window_turns"),
        "keyword.scan_turns": s.get("scan_turns"),
        "keyword.max_entries": s.get("max_entries"),
        "memory.recalled_selection": s.get("recall"),
        "llm.provider": s.get("provider"),
        "llm.model": s.get("model") or None,
        "llm.temperature": s.get("temperature"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


class Handler(BaseHTTPRequestHandler):
    # Keep-alive for the same reason as the MCP handler: cloudflared reuses
    # origin connections and 502s when an HTTP/1.0 origin closes them.
    protocol_version = "HTTP/1.1"
    state: State  # injected

    server_version = "crack-emu"

    def log_message(self, fmt, *args):  # quieter console
        if args and isinstance(args[0], str) and "/api/" in args[0]:
            super().log_message(fmt, *args)

    # ── plumbing ──────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str, headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        """Read the request body, chunked or not.

        A proxy is free to re-frame the body it forwards, and cloudflared does:
        it can drop Content-Length and stream the request chunked instead.
        Reading Content-Length alone then yields an empty body and every call
        comes back 400, but only from the tunnel — never from localhost.
        """
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            buf = bytearray()
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()        # trailing CRLF
                    break
                buf += self.rfile.read(size)
                self.rfile.readline()            # CRLF after each chunk
            return bytes(buf)
        return self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))

    def _read_json(self) -> dict:
        return json.loads(self._read_body() or b"{}")

    # ── routes ────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/" or url.path == "/index.html":
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path.startswith("/api/download/"):
                raw_filename = unquote(url.path[len("/api/download/"):])
                safe_filename = Path(raw_filename).name
                export_file = exports_dir() / safe_filename
                if export_file.is_file():
                    data = export_file.read_bytes()
                    utf8_name = quote(safe_filename)
                    return self._send(200, data, "application/zip", headers=[
                        ("Content-Disposition", f"attachment; filename=\"export.zip\"; filename*=UTF-8''{utf8_name}")
                    ])
                return self._json({"error": f"file '{safe_filename}' not found"}, 404)
            if url.path == "/api/projects":
                base = workspace_root()
                current_root = Path(self.state.project_root).resolve()
                if current_root.name == "build":
                    current_root = current_root.parent
                projects = []
                for p in sorted(base.iterdir()):
                    if p.is_dir() and not p.name.startswith(".") and p.name not in ("crack-story-chat-skill", "crack-design-mcp", "novel-ai-image-skill"):
                        has_build = (p / "build").is_dir()
                        title = p.name
                        premise = ""
                        story_p = p / "story.md"
                        if story_p.exists():
                            try:
                                stext = story_p.read_text(encoding="utf-8")
                                t_m = re.search(r"^-\s*Title:\s*(.+)$", stext, re.MULTILINE | re.IGNORECASE)
                                if t_m:
                                    title = t_m.group(1).strip()
                                p_m = re.search(r"^-\s*(?:Premise|Logline|핵심 한 줄):\s*(.+)$", stext, re.MULTILINE | re.IGNORECASE)
                                if p_m:
                                    premise = p_m.group(1).strip()
                            except Exception:
                                pass
                        projects.append({
                            "name": p.name,
                            "title": title,
                            "premise": premise,
                            "path": str(p),
                            "has_build": has_build,
                            "is_active": p.resolve() == current_root,
                        })
                return self._json({"active_project": current_root.name, "projects": projects})
            if url.path == "/api/project":
                return self._json(self._project_payload())
            if url.path == "/api/sessions":
                return self._json({"sessions": self.state.store.list()})
            if url.path == "/api/session":
                sid = (q.get("id") or [""])[0]
                if not sid or not self.state.store.exists(sid):
                    return self._json({"error": f"session '{sid}' not found"}, 404)
                return self._json(self._session_payload(sid))
            if url.path == "/api/health":
                # Honour the model typed into the UI, otherwise the panel keeps
                # showing the preset while turns run against something else.
                overrides = {}
                if q.get("model"):
                    overrides["llm.model"] = q["model"][0]
                if q.get("base_url"):
                    overrides["llm.base_url"] = q["base_url"][0]
                cfg = self.state.config(overrides)
                names = q.get("provider") or list(cfg.get("providers", {}))
                return self._json({"providers": [
                    make_client(cfg, p, api_key=self.state.keys.get(p)).health()
                    for p in names]})
            if url.path == "/api/keys":
                return self._json({"keys": self.state.key_status()})
            if url.path == "/api/memory":
                return self._json(self._memory(
                    q["id"][0],
                    query=(q.get("input") or [""])[0],
                    recall=(q.get("recall") or [None])[0],
                    window=(q.get("window") or [None])[0]))
            if url.path == "/api/lint":
                findings = lint(self.state.project)
                return self._json({"findings": findings})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        try:
            body = self._read_json()
            if url.path == "/api/session":
                return self._json(self._create_session(body))
            if url.path == "/api/turn":
                return self._json(self._turn(body))
            if url.path == "/api/prompt":
                return self._json(self._prompt(body))
            if url.path == "/api/session/delete":
                sid = body["id"]
                removed = self.state.store.delete(sid)
                log_removed = ActivationLog(
                    self.state.store.root.parent / "logs").delete(sid)
                return self._json({"ok": True, "id": sid, "session_removed": removed,
                                   "log_removed": log_removed,
                                   "sessions": self.state.store.list()})
            if url.path == "/api/check":
                return self._json(self._check(body))
            if url.path == "/api/memory":
                return self._json(self._memory_write(body))
            if url.path == "/api/memory/refresh":
                return self._json(self._memory_refresh(body))
            if url.path == "/api/project/switch":
                p_name = body.get("project") or body.get("project_name")
                if not p_name:
                    return self._json({"error": "project name required"}, 400)
                base = workspace_root()
                candidates = [base / p_name, base / Path(p_name).name]
                target = next((c for c in candidates if c.is_dir()), None)
                if not target:
                    return self._json({"error": f"project '{p_name}' not found"}, 404)
                build_dir = target / "build" if (target / "build").is_dir() else target
                self.state.reload(str(build_dir))
                return self._json({"ok": True, "active_project": target.name, "project": self._project_payload()})
            if url.path == "/api/keys":
                provider = body["provider"]
                key = (body.get("key") or "").strip()
                self.state.keys.set(provider, key)
                return self._json({"keys": self.state.key_status()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ── handlers ──────────────────────────────────────────────────
    def _static(self, rel: str):
        path = (STATIC / rel).resolve()
        if not str(path).startswith(str(STATIC)) or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return self._send(200, path.read_bytes(), ctype)

    def _project_payload(self) -> dict:
        p = self.state.project
        cfg = self.state.base_cfg
        return {
            "name": p.name,
            "root": p.root,
            "variants": p.variants,
            "characters": [{"number": c.number, "name": c.name} for c in p.characters],
            "shortcuts": {v: [{"id": s.id, "name": s.name, "description": s.description}
                              for s in p.shortcut_list(v)] for v in p.variants},
            "entries": {v: [{"title": e.title, "keywords": e.keywords, "chars": e.char_count}
                            for e in p.entries(v)] for v in p.variants},
            "prologue": p.prologue,
            "opening_situation": p.opening_situation,
            "start_sets": [{"id": x.id, "title": x.title, "description": x.description,
                            "prologue_chars": len(x.prologue),
                            "opening_chars": len(x.opening_situation),
                            "source": x.source} for x in p.start_sets],
            "providers": list(cfg.get("providers", {})),
            "keys": self.key_status_ref(),
            "defaults": {
                "provider": cfg.get("llm.provider"),
                "fidelity": cfg.fidelity,
                "window_turns": cfg.get("context.window_turns"),
                "scan_turns": cfg.get("keyword.scan_turns"),
                "recall": cfg.get("memory.recalled_selection"),
                "temperature": cfg.get("llm.temperature"),
            },
        }

    def key_status_ref(self) -> dict:
        return self.state.key_status()

    def _session_payload(self, sid: str) -> dict:
        s = self.state.store.load(sid)
        return {
            "id": s.id, "variant": s.variant, "start_set": s.start_set,
            "persona_name": s.persona_name, "persona_body": s.persona_body,
            "user_note": s.user_note, "goal": s.goal,
            "summaries": s.summaries, "relations": s.relations,
            "turns": [{"index": t.index, "role": t.role, "content": t.content,
                       "meta": t.meta} for t in s.turns],
        }

    def _create_session(self, body: dict) -> dict:
        sid = body["id"]
        if self.state.store.exists(sid) and body.get("overwrite"):
            self.state.store.delete(sid)
        if not self.state.store.exists(sid):
            variant = body.get("variant") or "safe"
            eng = self.state.engine(_overrides(body), variant)
            eng.start(sid,
                      persona_name=body.get("persona_name") or "{{user}}",
                      persona_body=body.get("persona_body") or "",
                      user_note=body.get("user_note") or "",
                      goal=body.get("goal") or "",
                      seed_prologue=body.get("seed_prologue", True),
                      start_set=body.get("start_set"))
        else:
            s = self.state.store.load(sid)
            wanted = body.get("start_set")
            if wanted and wanted != s.start_set:
                spoken = any(t.role == "user" for t in s.turns)
                if spoken and not body.get("restart"):
                    return {**self._session_payload(sid),
                            "start_set_change_needs_restart": True,
                            "requested_start_set": wanted}
                if spoken and body.get("restart"):
                    variant = body.get("variant") or s.variant
                    eng = self.state.engine(_overrides(body), variant)
                    eng.store.delete(sid)
                    eng.start(sid,
                              persona_name=body.get("persona_name") or s.persona_name,
                              persona_body=body.get("persona_body", s.persona_body),
                              user_note=body.get("user_note", s.user_note),
                              goal=body.get("goal", s.goal),
                              start_set=wanted)
                    return {**self._session_payload(sid), "restarted": True}
        return self._session_payload(sid)

    def _turn(self, body: dict) -> dict:
        sid = body["id"]
        with self.state.lock:
            session = self.state.store.load(sid)
            variant = session.variant or body.get("variant") or "safe"
            eng = self.state.engine(_overrides(body), variant)
            for field in ("persona_name", "persona_body", "user_note", "goal"):
                if field in body and body[field] is not None:
                    setattr(session, field, body[field])
            res = eng.turn(session, body["input"])
            return res.to_dict()

    def _prompt(self, body: dict) -> dict:
        sid = body["id"]
        session = self.state.store.load(sid)
        variant = session.variant or body.get("variant") or "safe"
        eng = self.state.engine(_overrides(body), variant)
        for field in ("persona_name", "persona_body", "user_note", "goal"):
            if field in body and body[field] is not None:
                setattr(session, field, body[field])
        p = eng.build_prompt(session, body.get("input") or "")
        window = int(eng.cfg.get("context.window_turns", 20))
        return {
            "system": p.system,
            "messages": p.messages,
            "char_count": p.char_count,
            "activations": [{"title": a.entry.title, "matched": a.matched,
                             "where": a.where, "chars": a.entry.char_count}
                            for a in p.activations],
            "dropped": [{"title": a.entry.title, "matched": a.matched,
                         "where": a.where, "chars": a.entry.char_count}
                        for a in p.dropped],
            "shortcut": p.shortcut.name if p.shortcut else None,
            "recalled": memory.select_recalled(session, eng.cfg, window, body.get("input") or ""),
            "evicted": len(memory.evicted_turns(session, window)),
            "live": len(memory.live_turns(session, window)),
        }

    def _memory(self, sid: str, query: str = "", recall: str | None = None,
                window: str | int | None = None) -> dict:
        # The panel has to describe the settings the UI is actually running,
        # or its "evicted" counts describe a different session than the chat.
        overrides: dict = {}
        if recall:
            overrides["memory.recalled_selection"] = recall
        if window not in (None, ""):
            try:
                overrides["context.window_turns"] = int(window)
            except (TypeError, ValueError):
                pass
        cfg = self.state.config(overrides)
        session = self.state.store.load(sid)
        window = int(cfg.get("context.window_turns", 20))
        return memory.snapshot(self.state.project.root, session, cfg, window, query)

    def _memory_write(self, body: dict) -> dict:
        with self.state.lock:
            session = self.state.store.load(body["id"])
            if "summaries" in body:
                session.summaries = [x for x in body["summaries"] if x.strip()]
            if "relations" in body:
                session.relations = [x for x in body["relations"] if x.strip()]
            if "recalled" in body:
                session.recalled = [x for x in body["recalled"] if x.strip()]
            if body.get("reset_summarized"):
                session.stats["_summarized_turns"] = 0
            self.state.store.save(session)
        return self._memory(body["id"], recall=body.get("recall"),
                            window=(body.get("settings") or {}).get("window_turns"))

    def _memory_refresh(self, body: dict) -> dict:
        variant = body.get("variant") or "safe"
        with self.state.lock:
            eng = self.state.engine(_overrides(body), variant)
            session = eng.load(body["id"])
            window = int(eng.cfg.get("context.window_turns", 20))
            result = memory.force_refresh(session, eng.cfg, window, eng.client)
            self.state.store.save(session)
        result["snapshot"] = self._memory(
            body["id"], recall=body.get("recall"),
            window=(body.get("settings") or {}).get("window_turns"))
        return result

    def _check(self, body: dict) -> dict:
        p = self.state.project
        eng_session = self.state.store.load(body["id"]) if body.get("id") else None
        session = eng_session or Session(id="adhoc", project_root=p.root, variant="safe")
        findings = qa.check(body["text"], p, session, user_input=body.get("input") or "")
        return {"findings": [f.to_dict() for f in findings], "qa": qa.summarize(findings)}


def serve(project_root: str, *, host: str = "127.0.0.1", port: int = 8765,
          spec: str | None = None, store: str | None = None,
          open_browser: bool = True, key_file: str | None = None) -> None:
    Handler.state = State(project_root, spec, store, key_file)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"crack-emu ui: {url}  (project: {Handler.state.project.name})")
    if key_file:
        print(f"api keys persist to: {key_file} (mode 600)")
    print("stop: ctrl-c")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()

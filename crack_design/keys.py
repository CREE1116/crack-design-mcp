"""Provider API keys, shared by the MCP server and the web UI.

Both servers needed the same store and had grown separate ones — the web UI a
class, the MCP server a bare dict it never wrote back — so a key set through one
was invisible to the other.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import state_root


class KeyStore:
    """Provider keys for the running server.

    In memory by default, which means a restart loses them. `--key-file` opts
    into writing them to a file outside the repository with owner-only
    permissions, because reloading the server is a normal part of iterating and
    retyping a key every time invites pasting it somewhere worse.
    """

    def __init__(self, path: str | None = None):
        # Defaults to a file under the state directory rather than memory:
        # restarting the server is routine here, and retyping a key every time
        # is what pushes people to paste it somewhere worse.
        self.path = Path(path).expanduser() if path else (state_root() / "keys.json")
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

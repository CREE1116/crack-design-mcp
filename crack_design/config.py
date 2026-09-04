"""Central configuration and constants for Crack design specifications."""

from __future__ import annotations

# Prompt budget constraints
MAX_PROMPT = 7000
TARGET_PROMPT = 6500

MAX_OPENING = 1000
TARGET_OPENING = 900

# Keyword book limits
MAX_KEYWORD_ENTRY = 400
MIN_KEYWORDS = 1
MAX_KEYWORDS = 5
MAX_ACTIVE_KEYWORD_SLOTS = 3

# Required artifacts in build/
CORE_EXPECTED = {
    "prologue.md",
    "integrated-prompt-safe.md",
    "integrated-prompt-unsafe.md",
    "start-prompt.md",
}

VALID_KB_SETS = [
    {"keyword-book-safe.md", "keyword-book-unsafe.md"},
    {"keyword-book-safe.md", "keyword-book-unsafe.md", "keyword-book.md"},
    {"keyword-book.md", "keyword-book-unsafe.md"},
    {"keyword-book.md"},
]

# Derived output directories
DERIVED_DIR = "assets"
ALLOWED_BUILD_DIRS = {DERIVED_DIR, "start-sets", "departments"}

# Banned safety bypass phrases (must never appear in unsafe prompts)
UNSAFE_BANNED = (
    "정책 무시",
    "정책 우회",
    "필터 우회",
    "검열 해제",
    "안전장치 해제",
    "무조건 수행",
    "unfiltered",
    "bypass safety",
    "ignore policy",
)

# Retired namespaces
RETIRED_NAMESPACES = (
    r"\b(?:char|canon)\.[A-Za-z0-9_-]+",
)

# Meaning-bearing operators requiring legend definition
OPERATORS = set("ⓤⓒⓝⓐ→←↑↓⇒≤≥±×÷·|｜/※≠≈")

# Visual layout scaffolding
LAYOUT_GLYPHS = set("━─│┃▸▪▶◆◇○●•…★☆♂♀§¶")


# ── Workspace roots ───────────────────────────────────────────────
# Story projects and emulator state live outside the package. Both were
# hardcoded to one developer's home directory, which made the paths useless on
# any other machine; they resolve from the environment now and fall back to the
# same layout relative to the current user's home.

import os
from pathlib import Path


def workspace_root() -> Path:
    """Directory holding the story projects, one subdirectory per project."""
    return Path(os.environ.get("CRACK_WORKSPACE", Path.home() / "crack")).expanduser()


def state_root() -> Path:
    """Directory for emulator state: sessions, logs, crack.db, exports."""
    return Path(os.environ.get("CRACK_STATE", Path.home() / ".crack-emu")).expanduser()


def exports_dir() -> Path:
    return state_root() / "exports"


def sync_tool_path() -> Path:
    """Playwright sync script from the companion crack-story-chat-skill repo."""
    env = os.environ.get("CRACK_SYNC_TOOL")
    if env:
        return Path(env).expanduser()
    return workspace_root() / "crack-story-chat-skill" / "tools" / "sync" / "crack_sync.py"

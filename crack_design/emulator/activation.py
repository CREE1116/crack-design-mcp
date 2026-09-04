"""Keyword-book activation.

Crack triggers a keyword-book entry when one of its keywords literally appears
in the text it scans. Scan scope per user report: the previous turn(s) plus the
current user input. Everything about the exact scope is marked UNVERIFIED in
the spec and driven from config, never hardcoded here.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config
from .models import KeywordEntry, Turn


@dataclass
class Activation:
    entry: KeywordEntry
    matched: list[str]
    where: list[str]  # "input" | "turn:<index>:<role>"


_HUD_CODEBLOCK = re.compile(r"```[ \t]*(?:Info|info|INFO|hud|HUD)?\s*\n.*?\n```", re.DOTALL)
_HUD_BRACKET = re.compile(r"\[(?:INFO|Info|info|HUD|상태창)\].*?(?=\n\n|\Z)", re.DOTALL)


def _strip_hud(text: str) -> str:
    """Strip HUD status blocks so repetitive HUD items never pollute keyword scan scope."""
    t = _HUD_CODEBLOCK.sub("", text)
    t = _HUD_BRACKET.sub("", t)
    return t


def _norm(text: str, cfg: Config) -> str:
    if cfg.get("keyword.normalize_nfkc", True):
        text = unicodedata.normalize("NFKC", text)
    if cfg.get("keyword.case_insensitive", True):
        text = text.casefold()
    if cfg.get("keyword.strip_whitespace", False):
        text = "".join(text.split())
    return text


def _contains_keyword(text: str, kw: str) -> bool:
    """Check if kw is contained in text, guarding against common false substring collisions."""
    if kw not in text:
        return False
    # Guard against common substring traps where short names fire on innocent words
    if kw == "라임" and "슬라임" in text:
        if "라임" not in text.replace("슬라임", " "):
            return False
    if kw == "니아" and ("라비니아" in text or "돌아가" in text):
        cleaned = text.replace("라비니아", " ").replace("돌아가", " ")
        if "니아" not in cleaned:
            return False
    if kw == "메리" and "메리수" in text:
        if "메리" not in text.replace("메리수", " "):
            return False
    return True


def scan_scope(turns: list[Turn], user_input: str, cfg: Config) -> list[tuple[str, str]]:
    """-> [(label, text), ...] oldest first, current input last."""
    n = int(cfg.get("keyword.scan_turns", 1) or 0)
    roles = set(cfg.get("keyword.scan_roles", ["user", "assistant"]))
    scoped = [t for t in turns if t.role in roles]
    window = scoped[-n:] if n > 0 else []
    # Crack scans the full turn text including HUD — do NOT strip here.
    out = [(f"turn:{t.index}:{t.role}", t.content) for t in window]
    out.append(("input", user_input))
    return out


def activate(entries: list[KeywordEntry], turns: list[Turn], user_input: str,
             cfg: Config) -> list[Activation]:
    return activate_detail(entries, turns, user_input, cfg)[0]


def activate_detail(entries: list[KeywordEntry], turns: list[Turn], user_input: str,
                    cfg: Config) -> tuple[list[Activation], list[Activation]]:
    """-> (loaded, dropped).

    Crack loads at most `keyword.max_entries` entries per turn and drops the
    rest. What it does when more than that match is NOT documented anywhere in
    the reverse-engineered dump, so the ordering is spec-driven:

      doc_order   — keyword-book document order alone. This is what the skill's
                    own `check_kb_slots.py` simulates (MAX_SLOTS = 3), and it is
                    the only rule with any evidence behind it, so it is the
                    default at fidelity=crack.
      input_first — matches in the current user input outrank matches that came
                    only from a scanned previous turn. Reads better in long
                    sessions, but it is our invention, so it is an [EXTENSION]
                    and stays off unless the spec turns it on.

    An earlier build hardcoded "longer keyword wins" inside input_first. That
    had no source at all and silently outranked document order, so it is gone.
    """
    scope = scan_scope(turns, user_input, cfg)
    normed = [(label, _norm(text, cfg)) for label, text in scope]

    hits: list[Activation] = []
    for entry in entries:
        matched: list[str] = []
        where: list[str] = []
        for kw in entry.keywords:
            nk = _norm(kw, cfg)
            if not nk:
                continue
            found = [label for label, text in normed if _contains_keyword(text, nk)]
            if found:
                matched.append(kw)
                where.extend(f for f in found if f not in where)
        if matched:
            hits.append(Activation(entry=entry, matched=matched, where=where))

    policy = cfg.get("keyword.priority", "doc_order")
    if policy == "input_first":
        def _tier(a: "Activation") -> int:
            if "input" in a.where:
                return 0          # said in this very input
            if any(w.endswith(":user") for w in a.where):
                return 1          # said by the player a turn ago
            return 2              # only the model echoed it
        hits.sort(key=lambda a: (_tier(a), a.entry.order))
    else:
        hits.sort(key=lambda a: a.entry.order)

    dropped: list[Activation] = []

    max_entries = cfg.get("keyword.max_entries")
    if max_entries:
        n = int(max_entries)
        dropped.extend(hits[n:])
        hits = hits[:n]

    max_chars = cfg.get("keyword.max_chars")
    if max_chars:
        budget, kept = int(max_chars), []
        for h in hits:
            if budget - h.entry.char_count < 0:
                dropped.append(h)
                continue
            budget -= h.entry.char_count
            kept.append(h)
        hits = kept
    return hits, dropped


def match_shortcut(user_input: str, shortcuts) -> tuple[object | None, str]:
    """-> (Shortcut, remaining argument text) if the input starts with a slash command."""
    text = user_input.strip()
    if not text.startswith("/"):
        return None, user_input
    head = text.split(None, 1)
    # Stored shortcut names carry no slash (Crack's UI adds it), so compare the
    # bare command against the bare name.
    cmd = head[0].lstrip("/")
    rest = head[1] if len(head) > 1 else ""
    for s in shortcuts:
        if cmd == s.name.lstrip("/"):
            return s, rest
    return None, user_input

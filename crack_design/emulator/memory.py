"""Summary memory, character relations and recalled history.

Slot counts come from the observed Crack structure: 4 summary entries under
`[최근 사건 타임라인]`, 5 under `[캐릭터 관계도]`, exactly 3 under
`<recalled_history>`. How Crack *selects* the recalled 3 is unknown, so the
default is `recent` rather than an invented scoring rule.
"""
from __future__ import annotations

import re

from .activation import _strip_hud
from .config import Config
from .lexical import BM25
from .models import Session, Turn

# Observed shape of a Crack summary-memory entry: a scene name, a
# date｜time｜place header, then the events. Matching it matters because the
# header is what makes several summaries readable as a timeline.
SUMMARY_SYSTEM = (
    "너는 롤플레이 세션의 기록 담당이다. 주어진 대화 조각을 아래 형식으로 압축한다.\n"
    "형식(3줄):\n"
    "1줄: 장면 이름 (예: 관악산 전투)\n"
    "2줄: 날짜｜시각｜장소 (대화에 없으면 그 줄은 생략)\n"
    "3줄: 사건 요약. 사건·결정·관계 변화만 남긴다. 묘사와 대사는 버린다.\n"
    "규칙: 추측 금지. 전체 최대 {max_chars}자. 다른 말 붙이지 말고 형식대로만 출력."
)

# Observed shape of a long-term entry: the turn number, then what happened.
LONGTERM_SYSTEM = (
    "너는 롤플레이 세션의 장기기억 담당이다. 주어진 대화 조각에서 나중에 다시 참조될 만한 "
    "사건만 한국어 한 문단으로 요약한다.\n"
    "규칙: 인물명을 주어로 쓴다. 사건·결정·약속·관계 변화만 남긴다. 묘사와 대사는 버린다. "
    "추측 금지. 최대 {max_chars}자. 다른 말 붙이지 말고 요약문만 출력."
)

RELATION_SYSTEM = (
    "너는 롤플레이 세션의 관계 기록 담당이다. 대화에서 등장인물과 플레이어의 관계 상태를 "
    "인물당 한 줄로 정리한다. 형식: `이름 — 관계단계 / 최근 변화 근거`. "
    "최대 {slots}줄. 등장하지 않은 인물은 쓰지 않는다. 추측 금지. 목록만 출력."
)


def evicted_turns(session: Session, window_turns: int) -> list[Turn]:
    """Turns that have fallen out of the live context window."""
    if window_turns <= 0 or len(session.turns) <= window_turns:
        return []
    return session.turns[: len(session.turns) - window_turns]


def live_turns(session: Session, window_turns: int) -> list[Turn]:
    if window_turns <= 0:
        return list(session.turns)
    return session.turns[-window_turns:]


def _render(turns: list[Turn]) -> str:
    """HUD 제거 후 렌더링 — recalled/summary 주입 시 과거 상태창 오염 방지."""
    return "\n".join(f"[{t.role}] {_strip_hud(t.content)}" for t in turns)


def should_summarize(session: Session, cfg: Config, window_turns: int) -> bool:
    every = int(cfg.get("memory.summarize_every_turns", 8) or 0)
    if every <= 0:
        return False
    pending = len(evicted_turns(session, window_turns)) - _summarized_count(session)
    return pending >= every


def _summarized_count(session: Session) -> int:
    return int(session.stats.get("_summarized_turns", 0))


_SENTENCE_END = re.compile(r"[.!?。…]|다\.|요\.|음\.|임\.")
# A summary the model got cut off mid-word ends on none of these.
_COMPLETE_TAIL = re.compile(r"(?:[.!?。…\u201d\u2019)\]]|다|요|음|임|함)\s*$")


def truncate_to_sentence(text: str, max_chars: int) -> str:
    """Trim to max_chars *and* to the last complete sentence.

    Two distinct cuts land here. The obvious one is our own max_chars cap. The
    one that actually bit in play was upstream: the model hit its completion
    token ceiling and returned a summary ending "...목표임을 이해하게 되".
    That text is under max_chars, so a length-only guard passes it straight
    through and the broken fragment then rides in every later system prompt.
    Completeness is therefore checked regardless of length.
    """
    text = text.strip()
    sliced = text if len(text) <= max_chars else text[:max_chars]

    if len(text) <= max_chars and _COMPLETE_TAIL.search(sliced):
        return sliced

    last_end = -1
    for m in _SENTENCE_END.finditer(sliced):
        last_end = m.end()
    if last_end >= max_chars // 2 or (last_end > 0 and len(text) <= max_chars):
        return sliced[:last_end].strip()
    # No sentence boundary to fall back on: drop the trailing partial clause
    # rather than emit a dangling one.
    cut = max(sliced.rfind(","), sliced.rfind("、"), sliced.rfind("\n"))
    if cut >= max_chars // 2:
        return sliced[:cut].strip()
    return sliced.strip()


def update_summaries(session: Session, cfg: Config, window_turns: int, llm) -> bool:
    """Compress newly evicted turns into one summary slot. Returns True if updated."""
    evicted = evicted_turns(session, window_turns)
    done = _summarized_count(session)
    pending = evicted[done:]
    if not pending:
        return False

    max_chars = int(cfg.get("memory.summary_max_chars", 300))
    text = llm.complete(
        system=SUMMARY_SYSTEM.format(max_chars=max_chars),
        messages=[{"role": "user", "content": _render(pending)}],
        # Korean summaries run ~1.5 tokens/char; 300 chars needs well under
        # this. The old 512 was tight enough that the model got clipped
        # mid-sentence, which is what produced the broken slot in play.
        max_tokens=1024,
        temperature=0.2,
    ).strip()

    summary_text = truncate_to_sentence(text, max_chars)
    if not summary_text:
        return False

    # Dedup: if identical to the previous summary, avoid polluting summary slots
    if session.summaries and summary_text == session.summaries[-1]:
        session.stats["_summarized_turns"] = len(evicted)
        return False

    session.summaries.append(summary_text)
    # Storage is uncapped by default; only the prompt injection is limited.
    store = cfg.get("memory.summary_store")
    if store:
        session.summaries = session.summaries[-int(store):]
    session.stats["_summarized_turns"] = len(evicted)
    return True


def update_relations(session: Session, cfg: Config, window_turns: int, llm) -> None:
    slots = int(cfg.get("memory.relation_slots", 5))   # asked of the model, not a store cap
    recent = live_turns(session, window_turns)
    if not recent:
        return
    text = llm.complete(
        system=RELATION_SYSTEM.format(slots=slots),
        messages=[{"role": "user", "content": _render(recent)}],
        max_tokens=512,
        temperature=0.2,
    ).strip()
    lines = [ln.strip("-• \t") for ln in text.splitlines() if ln.strip()]
    store = cfg.get("memory.relation_store")
    session.relations = lines[:int(store)] if store else lines


GOAL_SYSTEM = (
    "너는 롤플레이 세션의 목표 기록 담당이다. 대화에서 플레이어가 지금 추구하는 "
    "당면 목표를 한 문장으로 적는다. 규칙: 이미 끝난 일은 쓰지 않는다. 추측 금지. "
    "최대 {max_chars}자. 다른 말 붙이지 말고 목표 문장만 출력."
)


def update_goal(session: Session, cfg: Config, window_turns: int, llm) -> bool:
    """Refresh `[주어진 목표]` from recent play. Returns True if it changed.

    The block sits in the same summary-memory region as the timeline and the
    relation map, both of which the model maintains, so this one is maintained
    too. What the source dump does not say is who fills it, which is why the
    behaviour is spec-driven; and a goal the user set by hand always wins.
    """
    if cfg.get("memory.goal_source", "auto") != "auto" or session.goal_locked:
        return False
    recent = live_turns(session, window_turns)
    if not recent:
        return False
    max_chars = int(cfg.get("memory.goal_max_chars", 120))
    text = llm.complete(
        system=GOAL_SYSTEM.format(max_chars=max_chars),
        messages=[{"role": "user", "content": _render(recent)}],
        max_tokens=256,
        temperature=0.2,
    ).strip()
    goal = truncate_to_sentence(text.splitlines()[0] if text else "", max_chars)
    if not goal or goal == session.goal:
        return False
    session.goal = goal
    return True


def _longterm_done(session: Session) -> int:
    return int(session.stats.get("_longterm_turns", 0))


def update_longterm(session: Session, cfg: Config, window_turns: int, llm) -> int:
    """Summarise newly evicted turns into `⌛<turn> <events>` entries.

    Crack keeps long-term memory as turn-numbered event summaries, not as the
    turns themselves. Injecting whole past replies instead — which is what this
    did before — spends the context budget of three full answers to deliver one
    remembered fact, and matches on the wrong text besides.

    Returns how many entries were added.
    """
    evicted = evicted_turns(session, window_turns)
    pending = evicted[_longterm_done(session):]
    if not pending:
        return 0

    every = max(1, int(cfg.get("memory.longterm_every_turns", 2)))
    max_chars = int(cfg.get("memory.longterm_max_chars", 300))
    added = 0
    for i in range(0, len(pending), every):
        chunk = pending[i:i + every]
        text = llm.complete(
            system=LONGTERM_SYSTEM.format(max_chars=max_chars),
            messages=[{"role": "user", "content": _render(chunk)}],
            max_tokens=512,
            temperature=0.2,
        ).strip()
        summary = truncate_to_sentence(text, max_chars)
        if not summary:
            continue
        session.longterm.append({"turn": chunk[-1].index, "text": summary})
        added += 1

    store = cfg.get("memory.longterm_store")
    if store:
        session.longterm = session.longterm[-int(store):]
    session.stats["_longterm_turns"] = len(evicted)
    return added


def render_longterm(item: dict) -> str:
    """`⌛85 서리화가 …` — the observed on-the-wire shape."""
    return f"⌛{item.get('turn', '?')} {item.get('text', '').strip()}".strip()


def force_refresh(session: Session, cfg: Config, window_turns: int, llm) -> dict:
    """Summarise and re-derive relations right now, regardless of the interval."""
    before = (list(session.summaries), list(session.relations))
    evicted = evicted_turns(session, window_turns)
    summarised = False
    if evicted[_summarized_count(session):]:
        summarised = update_summaries(session, cfg, window_turns, llm)
    update_relations(session, cfg, window_turns, llm)
    goal_before = session.goal
    goal_changed = update_goal(session, cfg, window_turns, llm)
    return {
        "summarised": summarised,
        "goal_changed": goal_changed,
        "goal_before": goal_before, "goal_after": session.goal,
        "goal_locked": session.goal_locked,
        "summaries_before": before[0], "summaries_after": list(session.summaries),
        "relations_before": before[1], "relations_after": list(session.relations),
        "pending_turns": len(evicted) - _summarized_count(session),
    }


def snapshot(project_root: str, session: Session, cfg: Config, window_turns: int,
             query: str = "") -> dict:
    """Everything the memory panel needs: slot contents, capacity and provenance."""
    evicted = evicted_turns(session, window_turns)
    live = live_turns(session, window_turns)
    mode = cfg.get("memory.recalled_selection", "recent")
    resolved = select_recalled(session, cfg, window_turns, query)
    return {
        "summary_slots": int(cfg.get("memory.summary_slots", 4)),
        "relation_slots": int(cfg.get("memory.relation_slots", 5)),
        "recalled_slots": int(cfg.get("memory.recalled_slots", 3)),
        "summaries": list(session.summaries),
        "relations": list(session.relations),
        "recalled_manual": list(session.recalled),
        "longterm_stored": len(session.longterm),
        "longterm": [render_longterm(x) for x in session.longterm[-8:]],
        "recalled_resolved": resolved,
        "recalled_selection": mode,
        "goal": session.goal,
        "goal_locked": session.goal_locked,
        "goal_source": cfg.get("memory.goal_source", "auto"),
        "summarize_every_turns": int(cfg.get("memory.summarize_every_turns", 8)),
        "summarized_turns": _summarized_count(session),
        "window_turns": window_turns,
        "turns_total": len(session.turns),
        "turns_live": len(live),
        "turns_evicted": len(evicted),
        "pending_turns": max(0, len(evicted) - _summarized_count(session)),
        "evicted_preview": [
            {"index": t.index, "role": t.role, "text": t.content[:160]}
            for t in evicted[-12:]
        ],
    }


_FRAGMENT_SPLIT = re.compile(r"(?<=[.!?。…])\s+|\n+")


def _fragments(turns: list[Turn]) -> list[str]:
    """Split evicted turns into sentence-level units for retrieval.

    Matching whole turns means a 350-word reply competes as one blob: the turn
    that actually held "특기: 엑셀 조금·요리" scores no better than any other
    long turn that shares its vocabulary, and then the whole 350 words get
    injected to deliver one fact. Sentences score on their own content and
    carry only themselves.
    """
    out: list[str] = []
    for t in turns:
        body = _strip_hud(t.content)
        for frag in _FRAGMENT_SPLIT.split(body):
            frag = frag.strip()
            if len(frag) >= 8:
                out.append(f"[{t.role}] {frag}")
    return out


def _cap(items: list[str], cfg: Config) -> list[str]:
    """Apply the per-slot character budget, if the spec sets one."""
    limit = cfg.get("memory.recalled_max_chars")
    if not limit:
        return items
    n = int(limit)
    return [x if len(x) <= n else truncate_to_sentence(x, n) for x in items]


def select_recalled(session: Session, cfg: Config, window_turns: int,
                    query: str) -> list[str]:
    """Fill `<recalled_history>` from stored long-term entries.

    Manual entries the user wrote take precedence; otherwise the pool is the
    turn-numbered summaries, newest last. A session recorded before long-term
    summaries existed has none, so it falls back to the evicted turns it does
    have rather than returning nothing.
    """
    slots = int(cfg.get("memory.recalled_slots", 3))
    mode = cfg.get("memory.recalled_selection", "recent")
    if slots <= 0:
        return []
    if mode == "manual":
        return _cap(session.recalled[:slots], cfg)

    pool = [render_longterm(x) for x in session.longterm]
    if not pool:
        evicted = evicted_turns(session, window_turns)
        if not evicted:
            return []
        granularity = cfg.get("memory.recalled_granularity", "turn")
        pool = _fragments(evicted) if granularity == "sentence" else [
            f"[{t.role}] {_strip_hud(t.content)}" for t in evicted
        ]

    if mode == "lexical":
        hits = BM25(pool).search(query, top_k=slots)
        return _cap([pool[i] for i, _ in hits], cfg)
    return _cap(pool[-slots:], cfg)

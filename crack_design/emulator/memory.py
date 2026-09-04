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

# [USER-OBSERVED] "[최근 사건 타임라인]" 축자. 항목마다 제목이 있고, 본문 앞에
# 턴번호와 시각·장소가 붙는다:
#     ### 저녁 식사
#     [⌛259] 2027년 12월 10일｜19:45｜크리의 집 크리가 준비한 김치찌개를 …
# 네 항목의 턴번호가 전부 [⌛259] 로 같았다. 사건이 일어난 턴이 아니라 요약이 쓰인
# 턴이라는 뜻이고, 따라서 타임라인은 항목을 덧붙이는 것이 아니라 갱신 때마다 통째로
# 다시 쓰인다. 작중 시각은 10:30 → 19:55 로 오래된 것이 위였다.
SUMMARY_SYSTEM = (
    "너는 롤플레이 세션의 기록 담당이다. 최근 대화를 장면 단위로 나눠 타임라인을 다시 쓴다.\n"
    "형식(장면당 2줄):\n"
    "1줄: ### 장면 이름\n"
    "2줄: 날짜｜시각｜장소 뒤에 이어서 사건 요약 (대화에 없으면 그 부분은 생략)\n"
    "규칙: 장면은 최대 {slots}개. 오래된 장면이 위, 최근이 아래. "
    "사건·결정·관계 변화만 남긴다. 묘사와 대사는 버린다. 추측 금지. "
    "장면당 최대 {max_chars}자. 다른 말 붙이지 말고 형식대로만 출력."
)

# Observed shape of a long-term entry: the turn number, then what happened.
# Entries are sparse — a real session put them 22 and 7 turns apart — so the
# judgement of whether anything happened at all is part of the job.
NOTHING = "없음"

LONGTERM_SYSTEM = (
    "너는 롤플레이 세션의 장기기억 담당이다. 주어진 대화 조각에 아래 기준을 만족하는 "
    "사건이 있으면 아래 형식으로 기록하고, 없으면 정확히 '{nothing}' 한 단어만 출력한다.\n"
    "형식(2줄):\n"
    "1줄: ### 장면 제목\n"
    "2줄: 날짜｜시각｜장소 뒤에 이어서 사건 요약 (날짜·시각·장소가 대화에 없으면 생략)\n"
    "기록할 것:\n{criteria}\n"
    "기록하지 않을 것: {exclude}\n"
    "요약 규칙: 인물명을 주어로 쓴다. 누가·무엇을·그 결과를 사실 위주로 쓴다. "
    "감정 묘사와 대사는 버린다. 추측 금지. 최대 {max_chars}자. "
    "다른 말 붙이지 말고 형식대로 또는 '{nothing}' 만 출력."
)


def _longterm_prompt(cfg: Config, max_chars: int) -> str:
    criteria = cfg.get("memory.longterm_criteria") or []
    exclude = cfg.get("memory.longterm_exclude") or "일상적인 대화"
    return LONGTERM_SYSTEM.format(
        nothing=NOTHING, max_chars=max_chars, exclude=exclude,
        criteria="\n".join(f"- {c}" for c in criteria) or "- 나중에 다시 참조될 사건",
    )

# [USER-OBSERVED] 관계도 항목은 한 줄 요약이 아니라 제목 + 산문 블록이다:
#     ### 서리화 (A급 헌터 / 부협회장)
#     크리의 아내이자 직속상관. 크리의 무모한 행동에 분노하면서도 그를 깊이 신뢰함. …
# 퇴장한 인물도 남는다 (### 리에발트 (군주) / EX급 재앙. … 완전히 소멸함.)
RELATION_SYSTEM = (
    "너는 롤플레이 세션의 관계 기록 담당이다. 대화에 등장한 인물별로 아래 형식으로 정리한다.\n"
    "형식(인물당 2줄):\n"
    "1줄: ### 이름 (직함 또는 등급)\n"
    "2줄: 플레이어와의 관계와 현재 태도를 사실 위주로 서술. 최근 변화가 있으면 함께 적는다.\n"
    "규칙: 등장하지 않은 인물은 쓰지 않는다. 퇴장하거나 사망한 인물도 그 결말을 적어 남긴다. "
    "추측 금지.{limit} 다른 말 붙이지 말고 형식대로만 출력."
)


def _slots(cfg: Config, name: str) -> int | None:
    """A slot budget, or None where the spec sets no limit."""
    v = cfg.get(f"memory.{name}")
    return int(v) if v else None


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


def _split_scenes(text: str) -> list[tuple[str, str]]:
    """-> [(title, body), ...] from the `### 제목` / 본문 pairs the model returns."""
    scenes: list[tuple[str, str]] = []
    title, body = "", []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if title or body:
                scenes.append((title, " ".join(body).strip()))
            title, body = stripped.lstrip("# ").strip(), []
        elif stripped:
            body.append(stripped)
    if title or body:
        scenes.append((title, " ".join(body).strip()))
    return [(t, b) for t, b in scenes if b]


def render_summary(item: dict) -> str:
    """`### 저녁 식사` / `[⌛259] 2027년 …｜크리의 집 크리가 …`"""
    if isinstance(item, str):
        return item                      # sessions written before the shape existed
    title = (item.get("title") or "").strip()
    turn = item.get("turn")
    body = (item.get("text") or "").strip()
    stamp = f"[⌛{turn}] " if turn is not None else ""
    head = f"### {title}" if title else ""
    return "\n".join(x for x in (head, f"{stamp}{body}".strip()) if x)


def parse_summary(text: str, turn: int) -> dict:
    """Read `### 제목` / `[⌛N] 본문` back into the stored shape.

    Summaries travel as rendered text at every boundary, so anything handed
    back — a hand-edited memory slot, a session file from an older build — has
    to survive the round trip.
    """
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    title, body = "", " ".join(lines)
    if lines and lines[0].startswith("#"):
        title = lines[0].lstrip("# ").strip()
        body = " ".join(lines[1:]).strip()
    stamp = re.match(r"\[?⌛(\d+)\]?\s*", body)
    if stamp:
        turn = int(stamp.group(1))
        body = body[stamp.end():]
    return {"turn": turn, "title": title, "text": body}


def update_summaries(session: Session, cfg: Config, window_turns: int, llm) -> bool:
    """Rewrite `[최근 사건 타임라인]` in full. Returns True if it changed.

    Every entry in the observed timeline carried the same turn stamp, so the
    block is regenerated wholesale at the current turn rather than accumulated
    one summary at a time. Rewriting also lets scenes merge and re-title as the
    story moves, which an append-only log cannot do.
    """
    recent = live_turns(session, window_turns)
    if not recent:
        return False

    slots = _slots(cfg, "summary_slots") or 0
    max_chars = int(cfg.get("memory.summary_max_chars", 300))
    turn = max((t.index for t in session.turns), default=0)

    text = llm.complete(
        system=SUMMARY_SYSTEM.format(slots=slots or "필요한 만큼", max_chars=max_chars),
        messages=[{"role": "user", "content": _render(recent)}],
        max_tokens=1024,
        temperature=0.2,
    ).strip()

    scenes = _split_scenes(text)
    # A generation that ran into its token ceiling ends mid-sentence, and that
    # fragment would then ride in every later prompt. Drop the tail scene when
    # it does not end like a finished sentence and something else survives.
    if len(scenes) > 1 and not _COMPLETE_TAIL.search(scenes[-1][1]):
        scenes = scenes[:-1]
    if not scenes:
        return False
    rebuilt = [{"turn": turn, "title": t, "text": truncate_to_sentence(b, max_chars)}
               for t, b in (scenes[-slots:] if slots else scenes)]

    before = [render_summary(x) for x in session.summaries]
    session.summaries = rebuilt
    session.stats["_summarized_turns"] = len(evicted_turns(session, window_turns))
    return [render_summary(x) for x in rebuilt] != before


def _relation_entries(lines: list[str]) -> list[list[str]]:
    """Group `### 이름` + its prose into one entry each."""
    out: list[list[str]] = []
    for ln in lines:
        if ln.lstrip().startswith("#") or not out:
            out.append([ln])
        else:
            out[-1].append(ln)
    return out


def update_relations(session: Session, cfg: Config, window_turns: int, llm) -> None:
    # Counted in entries, not lines: an entry is two lines, so a line budget
    # silently halves how many characters the map can hold.
    slots = cfg.get("memory.relation_slots")
    slots = int(slots) if slots else 0
    recent = live_turns(session, window_turns)
    if not recent:
        return
    text = llm.complete(
        system=RELATION_SYSTEM.format(
            limit=f" 최대 {slots}명." if slots else ""),
        messages=[{"role": "user", "content": _render(recent)}],
        max_tokens=512,
        temperature=0.2,
    ).strip()
    # Keep the "### 이름" heading markers: they are part of the observed shape.
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    entries = _relation_entries(lines)
    if slots:
        entries = entries[:slots]
    flat = [ln for e in entries for ln in e]
    store = cfg.get("memory.relation_store")
    session.relations = flat[:int(store)] if store else flat


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

    max_chars = int(cfg.get("memory.longterm_max_chars", 200))
    mode = cfg.get("memory.longterm_selection", "event")
    # In event mode the whole pending stretch is judged at once, so one
    # significant thing produces one entry however many turns it spanned.
    every = (max(1, int(cfg.get("memory.longterm_every_turns", 2)))
             if mode == "cadence" else len(pending))
    system = _longterm_prompt(cfg, max_chars)

    added = 0
    for i in range(0, len(pending), every):
        chunk = pending[i:i + every]
        text = llm.complete(
            system=system,
            messages=[{"role": "user", "content": _render(chunk)}],
            max_tokens=512,
            temperature=0.2,
        ).strip()
        if mode == "event" and (not text or text.strip().strip(".'\"") == NOTHING):
            continue                    # nothing here worth remembering
        summary = truncate_to_sentence(text, max_chars)
        if not summary or summary == NOTHING:
            continue
        session.longterm.append(_parse_longterm(summary, chunk[-1].index))
        added += 1

    store = cfg.get("memory.longterm_store")
    if store:
        session.longterm = session.longterm[-int(store):]
    session.stats["_longterm_turns"] = len(evicted)
    return added


def _parse_longterm(summary: str, turn: int) -> dict:
    """Split `### 제목 / 날짜｜시각｜장소 사건` back into its parts."""
    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    title, body = "", summary.strip()
    if lines and lines[0].startswith("#"):
        title = lines[0].lstrip("# ").strip()
        body = " ".join(lines[1:]).strip()
    header = ""
    if "｜" in body:
        head, _, rest = body.partition("｜")
        # The header runs to the last pipe-joined field before the prose.
        parts = body.split("｜")
        if len(parts) >= 2:
            tail = parts[-1].split(" ", 1)
            header = "｜".join(parts[:-1] + [tail[0]])
            body = tail[1].strip() if len(tail) > 1 else ""
    return {"turn": turn, "title": title, "header": header, "text": body}


def render_longterm(item: dict) -> str:
    """The observed `<recalled_history>` entry shape.

        ### 병원에서의 눈
        2027년 11월 28일｜08:15｜헌터협회 지정병원 특실 크리가 의식을 회복하고 …

    A titled scene block with a date｜time｜place header, not a turn-numbered
    line. A separately reported `⌛85 …` shape did not appear in the verbatim
    dump; where an entry carries no title or header this still falls back to
    the turn number so nothing is lost.
    """
    title = (item.get("title") or "").strip()
    header = (item.get("header") or "").strip()
    text = (item.get("text") or "").strip()
    if not title and not header:
        return f"⌛{item.get('turn', '?')} {text}".strip()
    lines = [f"### {title}" if title else "", f"{header} {text}".strip()]
    return "\n".join(x for x in lines if x)


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
        # null travels as null: the panel renders it as unlimited rather than
        # inventing a number.
        "summary_slots": _slots(cfg, "summary_slots"),
        "relation_slots": _slots(cfg, "relation_slots"),
        "recalled_slots": _slots(cfg, "recalled_slots"),
        "summaries": [render_summary(x) for x in session.summaries],
        "relations": list(session.relations),
        "relation_entries": len(_relation_entries(list(session.relations))),
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
    raw_slots = cfg.get("memory.recalled_slots", 3)
    slots = int(raw_slots) if raw_slots else 0        # 0/None = 제한 없음
    mode = cfg.get("memory.recalled_selection", "recent")
    if mode == "manual":
        return _cap(session.recalled[:slots] if slots else list(session.recalled), cfg)

    pool = [render_longterm(x) for x in session.longterm]
    if not pool:
        # No summarised entries yet — a young session, or one recorded before
        # they existed. The fallback reads the evicted turns directly, and it
        # must not paste whole replies back in: a 400-word answer returned in
        # full crowds out the live context it was supposed to supplement, and
        # over-represents early scenes for the rest of the session. Sentences
        # carry the fact without the bulk.
        evicted = evicted_turns(session, window_turns)
        if not evicted:
            return []
        granularity = cfg.get("memory.recalled_granularity", "sentence")
        pool = _fragments(evicted) if granularity == "sentence" else [
            f"[{t.role}] {_strip_hud(t.content)}" for t in evicted
        ]

    if mode == "lexical":
        hits = BM25(pool).search(query, top_k=slots or len(pool))
        return _cap([pool[i] for i, _ in hits], cfg)
    return _cap(pool[-slots:] if slots else pool, cfg)

"""Comprehensive all-in-one project and build audit for Crack story-chat."""

from __future__ import annotations
import re
from pathlib import Path
from dataclasses import dataclass, field
from .layout import audit_layout, LayoutAuditResult
from .length import check_length, LengthCheckResult
from .symbols import audit_symbols, SymbolAuditResult
from .naming import check_naming, NamingViolation
from .images import audit_images, ImageAuditResult
from ..config import (
    MAX_PROMPT, TARGET_PROMPT,
    MAX_OPENING, TARGET_OPENING,
    MAX_KEYWORD_ENTRY, MIN_KEYWORDS, MAX_KEYWORDS,
    UNSAFE_BANNED
)
from ..emulator.parser import parse_keyword_book

@dataclass
class AuditReport:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)

def audit_project(project_dir: str | Path, require_build: bool = True) -> AuditReport:
    root = Path(project_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    sections: dict[str, list[str]] = {}

    # 1. Layout audit
    layout_res = audit_layout(root, require_build=require_build)
    sections["layout"] = layout_res.report_lines
    if not layout_res.passed:
        errors.extend(l for l in layout_res.report_lines if l.startswith("FAIL"))

    # 2. Naming audit (retired namespaces)
    naming_passed, naming_violations = check_naming(root)
    naming_lines = []
    if not naming_passed:
        for v in naming_violations:
            msg = f"FAIL {v.file_path}:{v.line}: 폐기된 네임스페이스 감지: {v.match}"
            errors.append(msg)
            naming_lines.append(msg)
    else:
        naming_lines.append("PASS: 폐기된 네임스페이스(char.*, canon.*) 없음")
    sections["naming"] = naming_lines

    build_dir = root / "build"
    if require_build and build_dir.is_dir():
        # 3. Prompt lengths
        length_lines = []
        has_start_sets = (build_dir / "start-sets").is_dir() and any((build_dir / "start-sets").iterdir())
        
        # Check integrated prompts
        for fname in ("integrated-prompt-safe.md", "integrated-prompt-unsafe.md"):
            p = build_dir / fname
            if p.is_file():
                txt = p.read_text(encoding="utf-8")
                res = check_length(fname, txt, MAX_PROMPT, TARGET_PROMPT)
                length_lines.append(res.message)
                if not res.passed:
                    errors.append(res.message)
                elif res.is_warn:
                    warnings.append(res.message)
            else:
                msg = f"FAIL {fname}: 파일이 존재하지 않습니다."
                errors.append(msg)
                length_lines.append(msg)

        # Check root prologue / start-prompt or start-sets
        for fname in ("prologue.md", "start-prompt.md"):
            p = build_dir / fname
            if p.is_file():
                txt = p.read_text(encoding="utf-8")
                res = check_length(fname, txt, MAX_OPENING, TARGET_OPENING)
                length_lines.append(res.message)
                if not res.passed:
                    errors.append(res.message)
                elif res.is_warn:
                    warnings.append(res.message)
            elif not has_start_sets:
                msg = f"FAIL {fname}: 파일이 존재하지 않습니다."
                errors.append(msg)
                length_lines.append(msg)

        if has_start_sets:
            for sdir in sorted((build_dir / "start-sets").iterdir()):
                if sdir.is_dir() and not sdir.name.startswith("."):
                    for s_file in ("prologue.md", "start-prompt.md"):
                        sp = sdir / s_file
                        rel = f"start-sets/{sdir.name}/{s_file}"
                        if sp.is_file():
                            txt = sp.read_text(encoding="utf-8")
                            res = check_length(rel, txt, MAX_OPENING, TARGET_OPENING)
                            length_lines.append(res.message)
                            if not res.passed:
                                errors.append(res.message)
                            elif res.is_warn:
                                warnings.append(res.message)
                        else:
                            msg = f"FAIL {rel}: 파일이 존재하지 않습니다."
                            errors.append(msg)
                            length_lines.append(msg)

        sections["lengths"] = length_lines

        # 4. Safe vs Unsafe prompt parity and security
        safe_p = build_dir / "integrated-prompt-safe.md"
        unsafe_p = build_dir / "integrated-prompt-unsafe.md"
        parity_lines = []
        if safe_p.is_file() and unsafe_p.is_file():
            safe_text = safe_p.read_text(encoding="utf-8")
            unsafe_text = unsafe_p.read_text(encoding="utf-8")

            # Check banned bypass phrases
            for phrase in UNSAFE_BANNED:
                if phrase.casefold() in unsafe_text.casefold():
                    msg = f"FAIL integrated-prompt-unsafe.md: 금지된 탈옥/우회 문구 감지: '{phrase}'"
                    errors.append(msg)
                    parity_lines.append(msg)

            # Check heading drift
            def headings(t: str) -> set[str]:
                found = set()
                for m in re.finditer(r"^#{1,3}\s+(.+?)\s*$", t, re.MULTILINE):
                    h = m.group(1).strip().casefold()
                    if not (h.startswith("safe") or h.startswith("unsafe")):
                        found.add(h)
                return found

            safe_h = headings(safe_text)
            unsafe_h = headings(unsafe_text)
            if safe_h != unsafe_h:
                drift_msg = f"FAIL: SAFE와 UNSAFE 간 섹션 헤딩 불일치: SAFE-only={sorted(safe_h - unsafe_h)}, UNSAFE-only={sorted(unsafe_h - safe_h)}"
                errors.append(drift_msg)
                parity_lines.append(drift_msg)
            else:
                parity_lines.append("PASS: SAFE와 UNSAFE 섹션 헤딩 일치")

            # Symbol audit
            sym_safe = audit_symbols(safe_text)
            sym_unsafe = audit_symbols(unsafe_text)
            if not sym_safe.passed:
                errors.extend(f"[safe] {l}" for l in sym_safe.report_lines if l.startswith("FAIL"))
            if not sym_unsafe.passed:
                errors.extend(f"[unsafe] {l}" for l in sym_unsafe.report_lines if l.startswith("FAIL"))
            parity_lines.extend(f"[safe] {l}" for l in sym_safe.report_lines)
            parity_lines.extend(f"[unsafe] {l}" for l in sym_unsafe.report_lines)
        sections["prompt_parity"] = parity_lines

        # 5. Keyword book audit
        kb_lines = []
        kb_files = sorted(build_dir.glob("keyword-book*.md"))
        generic_keywords = {"인간", "마법", "회사", "신입", "업무", "직원", "동료"}
        for kb_f in kb_files:
            entries, _ = parse_keyword_book(kb_f.read_text(encoding="utf-8"), source=kb_f.name)
            kb_lines.append(f"INFO {kb_f.name}: {len(entries)}개 항목 분석")
            for entry in entries:
                # Check length
                res = check_length(f"{kb_f.name}::{entry.title}", entry.content, MAX_KEYWORD_ENTRY)
                if not res.passed:
                    errors.append(res.message)
                    kb_lines.append(res.message)

                # Check keyword count
                kw_count = len(entry.keywords)
                if kw_count < MIN_KEYWORDS or kw_count > MAX_KEYWORDS:
                    msg = f"FAIL {kb_f.name}::{entry.title}: 키워드 개수는 1~5개여야 합니다 (현재 {kw_count}개: {entry.keywords})"
                    errors.append(msg)
                    kb_lines.append(msg)

                # Check 3-slot collision risk with overly generic keywords
                for kw in entry.keywords:
                    if kw in generic_keywords:
                        w_msg = f"WARN {kb_f.name}::{entry.title}: 지나치게 광범위한 키워드 '{kw}' (3슬롯 상시 점유 및 다른 키워드 탈락 유발 위험)"
                        warnings.append(w_msg)
                        kb_lines.append(w_msg)
        sections["keyword_books"] = kb_lines

        # 6. Images audit
        img_res = audit_images(root)
        sections["images"] = img_res.report_lines
        if not img_res.passed:
            errors.extend(img_res.errors)
        if img_res.warnings:
            warnings.extend(img_res.warnings)

    overall_passed = len(errors) == 0
    return AuditReport(
        passed=overall_passed,
        errors=errors,
        warnings=warnings,
        summary={
            "passed": overall_passed,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        sections=sections,
    )

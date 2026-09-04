"""Audit a compiled prompt for meaning-bearing symbols used without a definition."""

from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass
from ..config import OPERATORS, LAYOUT_GLYPHS

@dataclass
class SymbolAuditResult:
    passed: bool
    missing_operators: list[str]
    missing_emojis: list[str]
    missing_layout: list[str]
    report_lines: list[str]

def is_emoji(char: str) -> bool:
    point = ord(char)
    if point < 0x2190:
        return False
    if 0x1F000 <= point <= 0x1FAFF:
        return True
    return unicodedata.category(char) == "So" and char not in LAYOUT_GLYPHS

def classify_symbol(char: str) -> str | None:
    if char in OPERATORS:
        return "operator"
    if char in LAYOUT_GLYPHS:
        return "layout"
    if is_emoji(char):
        return "emoji"
    return None

def glossed(text: str, symbol: str, kind: str) -> bool:
    escaped = re.escape(symbol)
    if re.search(rf"{escaped}\s*=", text):
        return True
    if any(symbol in span for span in re.findall(r"`([^`\n]{1,40})`", text)):
        return True
    for term, rhs in re.findall(r"([^\s=]{1,8})=([^.\n]{1,60})", text):
        if symbol in rhs and symbol not in term:
            return True
    if kind != "emoji":
        return False
    for line in text.splitlines():
        if symbol not in line:
            continue
        pairs = [m for m in re.finditer(r"(\S)[가-힣]{2,}", line) if is_emoji(m.group(1))]
        if len(pairs) >= 3 and any(m.group(1) == symbol for m in pairs):
            return True
    return False

def audit_symbols(text: str, strict: bool = False) -> SymbolAuditResult:
    # Look for legend section
    legend_match = re.search(r"^#\s*표기\s*$", text, re.MULTILINE)
    legend_text = ""
    rest_text = text
    if legend_match:
        following = re.search(r"^#{1,2}\s+", text[legend_match.end():], re.MULTILINE)
        end_pos = legend_match.end() + (following.start() if following else len(text[legend_match.end():]))
        legend_text = text[legend_match.start():end_pos]
        rest_text = text[:legend_match.start()] + text[end_pos:]

    used = {c: kind for c in set(rest_text) if (kind := classify_symbol(c))}
    missing_ops: list[str] = []
    missing_emojis: list[str] = []
    missing_layout: list[str] = []

    for sym, kind in sorted(used.items()):
        is_def = (sym in legend_text) or glossed(text, sym, kind)
        if not is_def:
            if kind == "operator":
                missing_ops.append(sym)
            elif kind == "emoji":
                missing_emojis.append(sym)
            elif kind == "layout":
                missing_layout.append(sym)

    report_lines: list[str] = []
    passed = len(missing_ops) == 0 and len(missing_emojis) == 0
    if strict and missing_layout:
        passed = False

    if missing_ops:
        report_lines.append(f"FAIL: 미정의 연산자 기호: {', '.join(missing_ops)}")
    if missing_emojis:
        report_lines.append(f"FAIL: 미정의 이모지 기호: {', '.join(missing_emojis)}")
    if missing_layout and strict:
        report_lines.append(f"FAIL (strict): 미정의 레이아웃 글리프: {', '.join(missing_layout)}")
    elif missing_layout:
        report_lines.append(f"INFO: 정의되지 않은 레이아웃 글리프 (장식용 허용): {', '.join(missing_layout)}")

    if passed and not report_lines:
        report_lines.append("PASS: 모든 의미 기호 및 이모지가 정의됨")

    return SymbolAuditResult(
        passed=passed,
        missing_operators=missing_ops,
        missing_emojis=missing_emojis,
        missing_layout=missing_layout,
        report_lines=report_lines,
    )

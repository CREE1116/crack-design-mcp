"""Accurate character and UTF-16 unit counter for Crack prompts and text."""

from __future__ import annotations
from dataclasses import dataclass

@dataclass
class LengthCheckResult:
    label: str
    measured: int
    limit: int
    target: int
    codepoints: int
    utf16_units: int
    passed: bool
    is_warn: bool
    message: str

def count_chars(text: str) -> tuple[int, int, int]:
    """Returns (measured, codepoints, utf16_units) where measured is max(codepoints, utf16_units)."""
    codepoints = len(text)
    utf16_units = len(text.encode("utf-16-le")) // 2
    return max(codepoints, utf16_units), codepoints, utf16_units

def check_length(label: str, text: str, limit: int, target: int = 0) -> LengthCheckResult:
    if target <= 0:
        target = limit
    measured, codepoints, utf16 = count_chars(text)
    passed = measured <= limit
    is_warn = passed and measured > target
    
    if not passed:
        msg = f"FAIL {label}: {measured}/{limit} chars ({- (limit - measured)} over; codepoints={codepoints}, utf16={utf16})"
    elif is_warn:
        msg = f"WARN {label}: {measured}/{limit} chars ({limit - measured} remaining; target {target}; codepoints={codepoints}, utf16={utf16})"
    else:
        msg = f"PASS {label}: {measured}/{limit} chars ({limit - measured} remaining; codepoints={codepoints}, utf16={utf16})"
        
    return LengthCheckResult(
        label=label,
        measured=measured,
        limit=limit,
        target=target,
        codepoints=codepoints,
        utf16_units=utf16,
        passed=passed,
        is_warn=is_warn,
        message=msg,
    )

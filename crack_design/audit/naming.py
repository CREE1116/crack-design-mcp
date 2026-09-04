"""Audit story and build files for retired dotted namespaces (e.g. char.*, canon.*)."""

from __future__ import annotations
import re
from pathlib import Path
from dataclasses import dataclass

FORBIDDEN_NAMESPACES = re.compile(r"\b(?:char|canon)\.[A-Za-z0-9_-]+", re.IGNORECASE)

@dataclass
class NamingViolation:
    file_path: str
    line: int
    match: str

def check_naming(root: Path) -> tuple[bool, list[NamingViolation]]:
    violations: list[NamingViolation] = []
    
    scan_files: list[Path] = []
    for name in ("story.md", "characters.md"):
        p = root / name
        if p.is_file():
            scan_files.append(p)
            
    build_dir = root / "build"
    if build_dir.is_dir():
        scan_files.extend(p for p in build_dir.rglob("*.md") if p.is_file())
        
    for p in scan_files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in FORBIDDEN_NAMESPACES.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            violations.append(NamingViolation(file_path=str(p.relative_to(root)), line=line_no, match=m.group(0)))
            
    passed = len(violations) == 0
    return passed, violations

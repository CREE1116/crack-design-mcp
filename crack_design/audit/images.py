"""Audit derived image prompts and asset mappings against characters.md."""

from __future__ import annotations
import json
import re
from pathlib import Path
from dataclasses import dataclass

@dataclass
class ImageAuditResult:
    passed: bool
    warnings: list[str]
    errors: list[str]
    report_lines: list[str]

def audit_images(project_dir: Path) -> ImageAuditResult:
    chars_path = project_dir / "characters.md"
    config_path = project_dir / "build" / "assets" / "prompts.json"
    
    if not config_path.exists():
        return ImageAuditResult(
            passed=True,
            warnings=[],
            errors=[],
            report_lines=["SKIP: 이미지 프롬프트 명세(build/assets/prompts.json) 없음"]
        )
    if not chars_path.exists():
        return ImageAuditResult(
            passed=False,
            warnings=[],
            errors=["characters.md 파일이 없습니다."],
            report_lines=["FAIL: characters.md 파일이 없습니다."]
        )

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        return ImageAuditResult(
            passed=False,
            warnings=[],
            errors=[f"prompts.json JSON 파싱 오류: {e}"],
            report_lines=[f"FAIL: prompts.json JSON 파싱 오류: {e}"]
        )

    headings = re.findall(r"^##\s+(.+)$", chars_path.read_text(encoding="utf-8"), re.MULTILINE)
    roster_names: set[str] = set()
    for h in headings:
        name_match = re.match(r"^([가-힣a-zA-Z0-9\s]+?)(?:\s*[「『·\(0-9]|$)", h.strip())
        if name_match:
            n = name_match.group(1).strip()
            if n and n != "user":
                roster_names.add(n)

    people: dict = {}
    if isinstance(cfg, list):
        for item in cfg:
            if isinstance(item, dict):
                n = item.get("name", "").strip()
                people[n] = item
    elif isinstance(cfg, dict):
        people = cfg.get("characters", {})

    warnings: list[str] = []
    errors: list[str] = []
    report_lines: list[str] = []

    # Check for missing characters in prompts.json
    for name in sorted(roster_names):
        found = False
        for p_key, p_val in people.items():
            if name in p_key or (isinstance(p_val, dict) and name in p_val.get("ko", "")):
                found = True
                break
        if not found:
            warnings.append(f"등장인물 '{name}'에 대한 이미지 프롬프트 정의가 누락되었습니다.")

    passed = len(errors) == 0
    if passed and not warnings:
        report_lines.append("PASS: 이미지 프롬프트 및 등장인물 매핑 정상")
    else:
        report_lines.extend(f"ERROR: {e}" for e in errors)
        report_lines.extend(f"WARN: {w}" for w in warnings)

    return ImageAuditResult(
        passed=passed,
        warnings=warnings,
        errors=errors,
        report_lines=report_lines,
    )

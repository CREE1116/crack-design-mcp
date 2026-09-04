"""Audit project and build directory structure."""

from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
import unicodedata
from ..config import CORE_EXPECTED, VALID_KB_SETS, ALLOWED_BUILD_DIRS

SOURCES = {"story.md", "characters.md"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_ROOT_PREFIXES = tuple(unicodedata.normalize('NFC', p) for p in (
    "build", "final", "image", "deploy", "output", "assets", "썸네일", "여캐", "무제"
))

@dataclass
class LayoutAuditResult:
    passed: bool
    missing_sources: list[str]
    unexpected_root_files: list[str]
    missing_core_artifacts: list[str]
    unexpected_build_files: list[str]
    kb_files: list[str]
    kb_set_valid: bool
    report_lines: list[str]

def audit_layout(project_dir: Path, require_build: bool = True) -> LayoutAuditResult:
    report_lines: list[str] = []
    if not project_dir.is_dir():
        return LayoutAuditResult(
            passed=False,
            missing_sources=[],
            unexpected_root_files=[],
            missing_core_artifacts=[],
            unexpected_build_files=[],
            kb_files=[],
            kb_set_valid=False,
            report_lines=[f"FAIL: 프로젝트 디렉터리를 찾을 수 없습니다: {project_dir}"]
        )

    entries = [e for e in project_dir.iterdir() if not e.name.startswith(".")]
    entry_names = {e.name for e in entries}

    missing_sources = sorted(s for s in SOURCES if not (project_dir / s).is_file())
    unexpected_root_files = sorted(
        e.name for e in entries
        if e.name not in SOURCES
        and not any(unicodedata.normalize('NFC', e.name).startswith(p) for p in ALLOWED_ROOT_PREFIXES)
        and e.suffix.lower() not in IMAGE_EXTS
        and not (e.name.endswith(".py") or e.name.endswith(".sh") or e.name.endswith(".json"))
    )

    build_dir = project_dir / "build"
    missing_core: list[str] = []
    unexpected_build: list[str] = []
    kb_files: list[str] = []
    kb_valid = True

    passed = True
    if missing_sources:
        passed = False
        report_lines.append(f"FAIL: 원본 파일 누락: {', '.join(missing_sources)}")

    if unexpected_root_files:
        # Non-fatal warning or failure depending on strictness
        report_lines.append(f"WARN: 예기치 않은 루트 파일/디렉터리: {', '.join(unexpected_root_files)}")

    if require_build:
        if not build_dir.is_dir():
            passed = False
            report_lines.append(f"FAIL: build 디렉터리가 없습니다: {build_dir}")
        else:
            build_entries = [e for e in build_dir.iterdir() if not e.name.startswith(".")]
            build_file_names = {e.name for e in build_entries if e.is_file()}
            has_start_sets = (build_dir / "start-sets").is_dir() and any((build_dir / "start-sets").iterdir())
            expected_core = (CORE_EXPECTED - {"prologue.md", "start-prompt.md"}) if has_start_sets else CORE_EXPECTED
            missing_core = sorted(expected_core - build_file_names)
            kb_files = sorted(n for n in build_file_names if n.startswith("keyword-book") and n.endswith(".md"))
            kb_valid = any(set(kb_files) == v_set for v_set in VALID_KB_SETS)

            all_known = CORE_EXPECTED | set(kb_files)
            unexpected_build = sorted(build_file_names - all_known)

            unexpected_build_dirs = sorted(
                e.name for e in build_entries
                if not e.is_file() and e.name not in ALLOWED_BUILD_DIRS
            )

            if missing_core:
                passed = False
                report_lines.append(f"FAIL: 필수 빌드 산출물 누락: {', '.join(missing_core)}")
            if not kb_valid:
                passed = False
                report_lines.append(f"FAIL: 비표준 키워드북 구성: {kb_files} (유효 세트 중 하나여야 함)")
            if unexpected_build:
                passed = False
                report_lines.append(f"FAIL: 알 수 없는 빌드 산출물: {', '.join(unexpected_build)}")
            if unexpected_build_dirs:
                passed = False
                report_lines.append(f"FAIL: 알 수 없는 빌드 하위 디렉터리: {', '.join(unexpected_build_dirs)}")

    if passed:
        report_lines.append("PASS: 프로젝트 및 빌드 산출물 레이아웃 검증 성공")

    return LayoutAuditResult(
        passed=passed,
        missing_sources=missing_sources,
        unexpected_root_files=unexpected_root_files,
        missing_core_artifacts=missing_core,
        unexpected_build_files=unexpected_build,
        kb_files=kb_files,
        kb_set_valid=kb_valid,
        report_lines=report_lines,
    )

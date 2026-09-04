"""Playwright automation module for Crack story chat editor.

Dedicated to injecting story assets and performing DRAFT SAVE ONLY.
Never clicks '발행' (Publish) or final release buttons.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any
from ..config import sync_tool_path

DEFAULT_PROFILE_DIR = Path.home() / ".crack" / "profile"
DEFAULT_LOGIN_URL = "https://crack.wrtn.ai"
SCREENSHOT_DIR = Path.home() / ".crack-emu" / "draft_screenshots"


def sync_project_to_draft(
    payload: dict[str, Any],
    target_url: str = DEFAULT_LOGIN_URL,
    profile_dir: Path | str | None = None,
    headless: bool = True,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """Automate Crack web editor via Playwright: inject form fields and click DRAFT SAVE only.

    Parameters:
    - payload: dictionary containing title, short_summary, main_prompt, prologue,
               opening_situation, keywords, shortcuts, etc. (inspect_sync_payload format)
    - target_url: Crack story create or edit page URL
    - profile_dir: persistent browser context directory holding cookies/login
    - headless: True for background execution, False to show browser
    - timeout_sec: timeout in seconds
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Playwright is not installed. Install with: uv run --with playwright python ... or pip install playwright && playwright install chromium",
            "playwright_missing": True,
        }

    p_dir = Path(profile_dir or DEFAULT_PROFILE_DIR).expanduser().resolve()
    p_dir.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    title = payload.get("title") or "스토리"
    print(f"🚀 [Playwright Draft Sync] 대상: '{title}' (URL: {target_url})", flush=True)

    # Dynamically import the injection functions from crack_sync if available
    sync_tool_path = sync_tool_path()
    sync_mod = None
    if sync_tool_path.exists():
        try:
            spec = importlib.util.spec_from_file_location("crack_sync_mod", str(sync_tool_path))
            if spec and spec.loader:
                sync_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(sync_mod)
        except Exception as e:
            print(f"⚠️ crack_sync module load error: {e}", flush=True)

    result: dict[str, Any] = {
        "success": False,
        "title": title,
        "target_url": target_url,
        "headless": headless,
        "draft_clicked": False,
        "screenshot_path": "",
        "logs": [],
    }

    def log(msg: str):
        print(f"   [DraftSync] {msg}", flush=True)
        result["logs"].append(msg)

    timestamp = int(time.time())
    shot_path = SCREENSHOT_DIR / f"draft_{re_clean(title)}_{timestamp}.png"

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(p_dir),
                headless=headless,
                slow_mo=50,
            )
            page = context.pages[0] if context.pages else context.new_page()

            log(f"브라우저 로딩 중: {target_url}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            except Exception as e:
                log(f"페이지 로드 경고: {e}")

            time.sleep(2)

            # If crack_sync module is loaded and has injection logic, use it
            if sync_mod and hasattr(sync_mod, "load_project_artifacts"):
                # Wrap payload in ProjectArtifacts compatible structure
                try:
                    # Check if at create page or navigate to it
                    if hasattr(sync_mod, "navigate_to_create_story") and "projects/" not in target_url:
                        sync_mod.navigate_to_create_story(page)
                        time.sleep(1.5)
                except Exception as e:
                    log(f"네비게이션 보조 경고: {e}")

            # Look for story editor form inputs:
            # 1. Title
            title_input = page.locator("input[placeholder*='제목'], input[aria-label*='제목']")
            if title_input.count() > 0:
                try:
                    title_input.first.fill(title)
                    log("제목 입력 완료")
                except Exception as e:
                    log(f"제목 입력 실패: {e}")

            # 2. Short summary / description
            summary = payload.get("short_summary") or payload.get("premise") or ""
            if summary:
                desc_input = page.locator("textarea[placeholder*='소개'], textarea[placeholder*='설명'], input[placeholder*='소개']")
                if desc_input.count() > 0:
                    try:
                        desc_input.first.fill(summary[:100])
                        log("한 줄 소개 입력 완료")
                    except Exception as e:
                        log(f"한 줄 소개 입력 실패: {e}")

            # 3. Main Prompt tab / input
            main_prompt = payload.get("main_prompt") or ""
            if main_prompt:
                # Click prompt tab if exists
                prompt_tab = page.locator("button:visible:has-text('프롬프트'), div[role='tab']:visible:has-text('프롬프트')")
                if prompt_tab.count() > 0:
                    try:
                        prompt_tab.first.click()
                        time.sleep(1)
                    except Exception:
                        pass
                mp_area = page.locator("textarea[placeholder*='시스템'], textarea[placeholder*='메인'], textarea[placeholder*='프롬프트']")
                if mp_area.count() > 0:
                    try:
                        mp_area.first.fill(main_prompt)
                        log("메인 프롬프트 입력 완료")
                    except Exception as e:
                        log(f"메인 프롬프트 입력 실패: {e}")

            # 4. Search for [임시저장] (DRAFT SAVE) button ONLY
            # NEVER click 발행 / 출시 / 제출
            time.sleep(1)
            draft_btn = page.locator(
                "button:visible:has-text('임시저장'), "
                "div[role='button']:visible:has-text('임시저장'), "
                "button:visible:has-text('임시 저장'), "
                "div[role='button']:visible:has-text('임시 저장')"
            )

            if draft_btn.count() > 0:
                draft_btn.first.click()
                result["draft_clicked"] = True
                log("✅ [임시저장] 버튼 클릭 완료! (최종 발행은 진행하지 않음)")
                time.sleep(2)
            else:
                log("⚠️ '임시저장' 버튼을 화면에서 직접 찾지 못했습니다. (저장 버튼이 별도 아이콘이거나 자동 저장 상태일 수 있음)")

            # Capture verification screenshot
            page.screenshot(path=str(shot_path), full_page=False)
            result["screenshot_path"] = str(shot_path)
            result["success"] = True
            log(f"인증 스크린샷 저장 완료: {shot_path}")

            context.close()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log(f"❌ 오류 발생: {exc}")

    return result


def re_clean(s: str) -> str:
    import re
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', s).strip('_')[:30]

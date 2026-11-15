"""Headless UI walkthrough (dev tool): onboarding -> chat -> projects -> challenges -> profile ->
settings -> system -> about, light/dark, mobile, outage dialog. Captures screenshots and any
JavaScript console errors.

    pip install playwright   (uses the system Microsoft Edge / Chrome - no browser download)
    python -m tests.ui_smoke http://127.0.0.1:8020 [mock_url]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"
MOCK = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9102"
OUT = Path(__file__).resolve().parent.parent / ".runtime" / "ui"
OUT.mkdir(parents=True, exist_ok=True)
SAMPLE = "def average(xs):\n    total = 0\n    for i in range(1, len(xs)):\n        total += xs[i]\n    return total / len(xs)\n"
errors: list[str] = []


def shot(page: Page, name: str) -> None:
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
    print("screenshot", name, flush=True)


def watch(page: Page) -> None:
    # "423 (Locked)" is the expected answer while another person is using the app (one person at a time)
    page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type == "error" and "status of 423" not in m.text else None)
    page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))


def expire_lock() -> None:
    """Simulate the active person's tab going away: their heartbeat is 10 minutes old (test DB only)."""
    import asyncio
    import os
    from datetime import datetime, timedelta, timezone

    import asyncpg

    async def run() -> None:
        url = (f"postgresql://{os.environ.get('CTE_DB_USER', 'cte')}:{os.environ.get('CTE_DB_PASSWORD', 'cte_local_dev')}"
               f"@127.0.0.1:{os.environ.get('CTE_DB_PORT', '5440')}/cte_test")
        conn = await asyncpg.connect(url)
        try:
            stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            await conn.execute("UPDATE app_state SET value = jsonb_set(value, '{heartbeat}', to_jsonb($1::text)) WHERE key = 'active_user'", stamp)
        finally:
            await conn.close()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:  # Playwright's sync API already owns this thread's event loop
        pool.submit(lambda: asyncio.run(run())).result()


def launch(p):
    for channel in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=channel, headless=True) if channel else p.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit("No Chromium-based browser available for Playwright")


def main() -> int:
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(viewport={"width": 1440, "height": 920})
        page = ctx.new_page()
        watch(page)
        response = page.goto(BASE + "/")
        page.wait_for_selector("#ob-body")
        shot(page, "01_welcome")
        # CSP nonce: the module script carries this response's nonce (browsers hide it from getAttribute)
        csp = (response.headers if response else {}).get("content-security-policy", "")
        nonce = page.evaluate("document.querySelector('script[type=module]').nonce")
        if not nonce or f"'nonce-{nonce}'" not in csp:
            errors.append(f"[csp] module script nonce {nonce!r} not in the CSP header")
        if page.evaluate("document.querySelector('script[type=module]').getAttribute('nonce')"):
            errors.append("[csp] nonce readable through getAttribute - browser nonce hiding not active")
        page.check("#ob-accept")
        page.click("#ob-next")
        page.fill("#f-user", "asha_dev")
        page.fill("#f-pass", "asha-strong-pass")
        page.fill("#f-pass2", "asha-strong-pass")
        page.fill("#f-name", "Asha")
        page.fill("#f-age", "20")
        page.select_option("#f-gender", "female")
        page.select_option("#f-exp", "1-2 years")
        page.click("#ob-next")
        page.fill("#f-strengths", "I break problems into small steps")
        page.fill("#f-weaknesses", "I rush past edge cases")
        page.fill("#f-opportunities", "Hackathons, open source")
        page.fill("#f-threats", "Relying on AI code without understanding it")
        shot(page, "02_swot")
        page.click("#ob-next")
        page.fill("#f-bio", "Second-year CS student who builds Discord bots and small Flask APIs.")
        page.click("#ob-next")
        page.fill("#f-pname", "Grade averager")
        page.fill("#f-pdesc", "Averages a list of grades")
        page.fill("#f-paste", SAMPLE)
        page.eval_on_selector("#f-level", "(el) => { el.value = 3; el.dispatchEvent(new Event('input')); }")
        shot(page, "03_projects_step")
        page.click("#ob-next")
        shot(page, "04_avatar_step")
        page.locator("#f-faces button").nth(1).click()
        page.click("#ob-next")
        shot(page, "05_review")
        page.wait_for_timeout(2600)
        page.click("#ob-next")
        page.wait_for_selector("#rk", timeout=30000)
        page.wait_for_timeout(3500)
        shot(page, "06_created_jobs")

        page.goto(BASE + "/#/chat")
        page.wait_for_selector("#c-log .bubble-ai", timeout=60000)
        shot(page, "07_chat_opening")
        page.fill("#c-input", "I think the main risk is an empty list: average() would divide by zero, so I'd guard it because callers can't know.")
        page.click("#c-send")
        # (a locator wait - wait_for_function("...") would need eval, which the app's CSP rightly forbids)
        page.locator("#c-log .bubble-ai").nth(1).wait_for(timeout=60000)
        page.wait_for_timeout(1500)
        shot(page, "08_chat_reply")
        # choose the scrutiny level yourself (chat level card -> Change)
        page.click("[data-change-level]")
        page.wait_for_selector("#lv-range")
        page.eval_on_selector("#lv-range", "(el) => { el.value = 5; el.dispatchEvent(new Event('input')); }")
        shot(page, "08b_change_level_dialog")
        page.click("text=Apply level")
        page.wait_for_selector("text=Scrutiny raised", timeout=15000)
        page.wait_for_timeout(800)
        shot(page, "08c_level_changed")

        page.goto(BASE + "/#/projects")
        page.wait_for_selector("[data-pid]", timeout=30000)
        for _ in range(60):
            if page.locator("text=Qwen coder review").count():
                break
            page.wait_for_timeout(1000)
            page.goto(BASE + "/#/projects")
        shot(page, "09_projects")
        if page.locator("[data-guess]").count():
            page.fill("[data-guess]", "What if the list is empty - divide by zero?\nThe loop range starts at 1 and skips the first element")
            page.locator("[data-hunt]").first.click()
            page.wait_for_timeout(5000)
            shot(page, "10_bughunt")

        page.goto(BASE + "/#/challenges")
        page.wait_for_selector("[data-gen]")
        page.click('[data-gen="bug_hunt"]')
        page.wait_for_selector("[data-answer]", timeout=60000)
        shot(page, "11_challenge_open")
        page.fill("[data-answer]", "The loop skips the first score because range starts at 1; empty list divides by zero; best is None on the first comparison.")
        page.locator("[data-submit]").first.click()
        page.wait_for_selector("text=Evaluation", timeout=60000)
        page.wait_for_timeout(800)
        shot(page, "12_challenge_evaluated")
        page.locator("[data-reveal]").first.click()
        page.wait_for_selector("text=Solution (revealed)", timeout=30000)
        shot(page, "13_challenge_revealed")

        page.goto(BASE + "/#/profile")
        page.wait_for_selector("text=Thinking skills", timeout=30000)
        shot(page, "14_profile")
        page.locator("text=Recent evidence").scroll_into_view_if_needed()
        shot(page, "14b_profile_evidence")
        page.click("#pf-refresh")  # re-analysis: the button stays busy until the job is done, then the page reloads
        page.wait_for_selector("#pf-refresh[aria-busy=true]", timeout=5000)
        if page.locator("#pf-refresh").is_enabled():
            errors.append("[profile] refresh button still clickable while a re-analysis runs")
        page.wait_for_selector("text=Profile re-analysed", timeout=60000)
        page.wait_for_selector("#pf-refresh:not([disabled])", timeout=15000)
        page.goto(BASE + "/#/settings")
        page.wait_for_selector("#s-fallback")
        shot(page, "15_settings")
        page.goto(BASE + "/#/system")
        page.wait_for_selector("text=Parallel worker queue", timeout=30000)
        shot(page, "16_system")
        page.goto(BASE + "/#/about")
        page.wait_for_selector("text=MIT License")
        shot(page, "17_about")
        page.click("#theme-toggle")
        page.goto(BASE + "/#/profile")
        page.wait_for_selector("text=Thinking skills")
        shot(page, "18_profile_light")
        page.goto(BASE + "/#/chat")
        page.wait_for_selector("#c-log .bubble-ai")
        shot(page, "19_chat_light")
        page.click("#theme-toggle")

        mobile = browser.new_context(viewport={"width": 390, "height": 844}, storage_state=ctx.storage_state())
        mpage = mobile.new_page()
        watch(mpage)
        mpage.goto(BASE + "/#/chat")
        mpage.wait_for_selector("#c-log .bubble-ai", timeout=30000)
        shot(mpage, "20_mobile_chat")
        overflow = mpage.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        if overflow:
            errors.append("[layout] horizontal overflow on a 390px-wide phone")

        httpx.post(f"{MOCK}/__mock/down", params={"model": "mock-coder"})
        try:
            page.wait_for_selector("text=AI server stopped working", timeout=20000)
            shot(page, "21_outage_dialog")
        finally:
            httpx.post(f"{MOCK}/__mock/up")
        page.wait_for_selector("text=AI servers are back online", timeout=25000)
        shot(page, "22_recovered")

        # ---- one person at a time: a second device while Asha is using the app
        other = browser.new_context(viewport={"width": 1280, "height": 860})
        opage = other.new_page()
        watch(opage)
        opage.goto(BASE + "/")
        opage.wait_for_selector("text=is using the app right now", timeout=20000)
        shot(opage, "23_lock_signin_screen")
        opage.goto(BASE + "/#/welcome")
        opage.wait_for_selector("text=You can create a profile when they finish", timeout=20000)
        shot(opage, "24_lock_create_profile")

        # Asha closes her laptop lid (heartbeat stops) -> Bob signs up on his device -> Asha's screen locks
        expire_lock()
        token = other.request.get(BASE + "/api/csrf").json()["token"]  # this "browser's" CSRF token
        res = other.request.post(BASE + "/api/onboarding", headers={"X-CSRF-Token": token}, multipart={
            "username": "bob_codes", "password": "bob-strong-pass", "name": "Bob", "age": "22", "accepted_disclaimer": "true",
            "form_elapsed_ms": "9000", "bio": "I write small games in Python and like puzzles."})
        if res.status != 200:
            errors.append(f"[lock] Bob could not sign up after Asha's lock expired: {res.status} {res.text()[:200]}")
        page.wait_for_selector("#lock-overlay >> text=is using the app right now", timeout=20000)
        shot(page, "25_lock_overlay")
        other.request.post(BASE + "/api/auth/logout", headers={"X-CSRF-Token": token})
        page.wait_for_selector("#lock-overlay >> text=The app is free again", timeout=20000)
        shot(page, "26_lock_free_again")
        page.click("#lock-overlay [data-lock-continue]")
        page.wait_for_selector("#c-log .bubble-ai", timeout=30000)
        if page.locator("#lock-overlay").count():
            errors.append("[lock] overlay still visible after Continue")
        shot(page, "27_continue_after_lock")

        # ---- delete account (Settings > Delete account): username + password confirmation
        page.goto(BASE + "/#/settings")
        page.wait_for_selector("#s-delete")
        page.locator("#s-delete").scroll_into_view_if_needed()
        shot(page, "28_settings_delete_account")
        page.click("#s-delete")
        page.wait_for_selector("#del-confirm")
        page.fill("#del-confirm", "asha_dev")
        page.fill("#cf-pass", "asha-strong-pass")
        shot(page, "29_delete_dialog")
        page.click("text=Delete account forever")
        page.wait_for_selector("text=was deleted from this computer", timeout=20000)
        shot(page, "30_account_deleted")
        if page.locator("text=@asha_dev").count():
            errors.append("[delete] the deleted profile is still shown on the sign-in screen")
        browser.close()
    print("\nJS errors:" if errors else "\nNo JS errors.")
    for e in errors:
        print("  ", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

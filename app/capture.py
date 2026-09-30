"""Interactive session capture via Playwright.

Usage:
    python -m app.capture ubereats [--auto] [--headless]
    python -m app.capture demaecan [--auto] [--account=NAME] [--headless]

Opens a real browser (--headless for servers without a display). With --auto
it fills in the stored credentials (data/credentials.json, encrypted). If
Uber Eats asks for an SMS code, enter it in the browser. The tool saves the
session cookies (encrypted) once the portal dashboard is loaded, then closes.
"""
import asyncio
import sys
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app import config
from app import credentials as creds
from app import db

MAX_REQUEST_LOG = 1500
MAX_BODY = 200_000
DONE_IDLE_SECONDS = 60
TIMEOUT_SECONDS = 420
LOGIN_PATH_HINTS = ("login", "auth", "sso", "signin", "accounts.", "otp", "verify", "/pin")


def platform_host(cfg: dict) -> str:
    return urlparse(cfg["login_url"]).netloc


def is_login_page(url: str) -> bool:
    return any(h in url.lower() for h in LOGIN_PATH_HINTS)


async def fill_quietly(page, selectors: list[str], value: str, timeout_ms: int = 15000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout_ms)
            await el.fill(value)
            print(f"  filled {sel}")
            return True
        except Exception:
            continue
    return False


async def click_quietly(page, selectors: list[str], timeout_ms: int = 8000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout_ms)
            await el.click()
            print(f"  clicked {sel}")
            return True
        except Exception:
            continue
    return False


async def page_has_text(page, *texts, timeout_ms: int = 3000) -> bool:
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def wait_for_url(page, fragments: list[str], timeout_ms: int = 30000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if any(f in page.url for f in fragments):
            return True
        await asyncio.sleep(0.5)
    return False


async def auto_login(page, platform: str, creds_key: str = "") -> str:
    """Automated login with stored credentials. Returns a note or ''."""
    c = creds.get_credentials(creds_key or platform)
    if not c or not c.get("username") or not c.get("password"):
        return "no stored credentials"
    print(f"Auto-login for {platform} ...")
    if platform == "ubereats":
        return await auto_login_ubereats(page, c)
    return await auto_login_demaecan(page, c)


async def auto_login_ubereats(page, c: dict) -> str:
    # SSO page (accounts.uber.com): email -> continue -> password -> log in.
    await wait_for_url(page, ["accounts.uber.com", "auth.uber.com"], timeout_ms=30000)
    if not await fill_quietly(page, ['input[name="email"]', 'input[type="email"]'], c["username"]):
        print("  ! could not find Uber email field")
    await click_quietly(page, ['button[type="submit"]', 'button:has-text("Next")', 'button:has-text("Continue")'])
    if not await fill_quietly(page, ['input[name="password"]', 'input[type="password"]'], c["password"]):
        print("  ! could not find Uber password field")
    await click_quietly(page, ['button[type="submit"]', 'button:has-text("Log in")', 'button:has-text("Login")'])

    # SMS/OTP step: hand over to the user if it appears.
    if await page_has_text(page, "verification code", "verification code sent", "6-digit code", "enter the code", timeout_ms=15000):
        print("  SMS verification required -> enter the code in the browser window now.")
        return "sms-otp"

    # Merchant PIN step (merchants.ubereats.com/manager/pin).
    await wait_for_url(page, ["/manager/pin"], timeout_ms=45000)
    if "/manager/pin" in page.url:
        print("  filling manager PIN")
        pin = str(c.get("pin") or "")
        if not pin:
            print("  ! no PIN stored")
            return ""
        await fill_quietly(page, ['input[type="password"]', 'input[maxlength="4"]', 'input[type="tel"]'], pin)
        clicked = await click_quietly(
            page,
            ['button:has-text("Submit")', 'button[type="submit"]', 'button:has-text("Verify")', 'button:has-text("Continue")'],
        )
        if not clicked:
            await page.keyboard.press("Enter")
            print("  pressed Enter to verify PIN")
        # Wait until we leave the PIN page.
        await wait_for_url(page, ["/manager/home/", "/manager/orders"], timeout_ms=30000)
    return ""


async def auto_login_demaecan(page, c: dict) -> str:
    # Demae login page offers two tabs; pick the email one.
    await click_quietly(page, ['button:has-text("メールアドレス")', 'a:has-text("メールアドレス")', '[role="tab"]:has-text("メール")'])
    if not await fill_quietly(page, ['input[type="email"]', 'input[name="email"]', 'input[type="text"]'], c["username"]):
        print("  ! could not find Demae email field")
    if not await fill_quietly(page, ['input[type="password"]'], c["password"]):
        print("  ! could not find Demae password field")
    await click_quietly(page, ['button[type="submit"]', 'button:has-text("ログイン")', 'button:has-text("ログインする")'])
    return ""


async def capture(platform: str, auto: bool = False, account: str = "", headless: bool = False) -> None:
    cfg = config.PLATFORMS[platform]
    host = platform_host(cfg)
    creds_key = f"{platform}-{account}" if account else platform
    requests_log = []
    seen_api_traffic = False
    last_api_seen_at = None
    started_at = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, channel=None)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        async def on_response(resp):
            nonlocal seen_api_traffic, last_api_seen_at
            url = resp.url
            content_type = resp.headers.get("content-type", "")
            same_host = host in url
            if not same_host:
                return
            if "json" not in content_type and "text" not in content_type:
                return
            record = {
                "ts": time.time(),
                "url": url,
                "method": resp.request.method,
                "status": resp.status,
                "content_type": content_type,
                "request_body": resp.request.post_data[:4000] if resp.request.post_data else None,
            }
            try:
                body = await resp.text()
            except Exception:
                body = None
            if body:
                record["response_body"] = body[:MAX_BODY]
            requests_log.append(record)
            if len(requests_log) > MAX_REQUEST_LOG:
                requests_log.pop(0)
            if resp.status == 200 and "json" in content_type:
                print(f"  [api] {resp.request.method} {url.split('?')[0]} -> {resp.status}")
                if not is_login_page(url):
                    seen_api_traffic = True
                    last_api_seen_at = time.time()

        page.on("response", on_response)

        print(f"\nOpening browser for {cfg['label']}...")
        print(f"URL: {cfg['login_url']}")
        if auto:
            print("Auto-login enabled (stored credentials).\n")
        else:
            print("Log in manually in the browser window. If prompted for an SMS code, enter it.\n")
        print("(Automatically closes once your dashboard has loaded. Max 7 minutes.)")

        await page.goto(cfg["login_url"], wait_until="domcontentloaded")
        if auto:
            note = await auto_login(page, platform, creds_key)
            if note == "sms-otp":
                print("Waiting for SMS code entry...")
            elif note:
                print(f"Auto-login note: {note}")

        print("Waiting for login + dashboard load...\n")

        while True:
            current_url = page.url
            logged_in_page = not is_login_page(current_url)
            if seen_api_traffic and logged_in_page and last_api_seen_at and time.time() - last_api_seen_at >= DONE_IDLE_SECONDS:
                print("Dashboard API traffic detected — capture complete.")
                break
            if time.time() - started_at > TIMEOUT_SECONDS:
                print("Timed out. Did you finish logging in?")
                break
            await asyncio.sleep(1)

        cookies = await context.cookies()
        await browser.close()

    if not seen_api_traffic:
        print("WARNING: no merchant API traffic was captured. Login may not have completed.")
    db.save_session(creds_key, cookies)
    out = config.CAPTURES_DIR / f"{creds_key}_requests.json"
    config.save_json(out, requests_log)
    print(f"Saved session for {creds_key} ({len(cookies)} cookies)")
    print(f"Saved {len(requests_log)} API requests to {out}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    auto = "--auto" in sys.argv
    headless = "--headless" in sys.argv
    account = ""
    for a in sys.argv:
        if a.startswith("--account="):
            account = a.split("=", 1)[1]
    if len(args) != 1 or args[0] not in config.PLATFORMS:
        print("Usage: python -m app.capture [ubereats|demaecan] [--auto] [--account=NAME] [--headless]")
        sys.exit(1)
    db.init_db()
    asyncio.run(capture(args[0], auto=auto, account=account, headless=headless))

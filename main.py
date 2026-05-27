"""
TikTok DM Worker — FastAPI + Playwright
Endpoints: /health, /inbox, /inbox/mark-seen, /dm/send, /thread/reply
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import asyncio
import os
import logging
import json
import random
import time
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TikTok DM Worker")

TT_USERNAME = os.getenv("TT_USERNAME", "")
TT_PASSWORD = os.getenv("TT_PASSWORD", "")
COOKIES_FILE = "/tmp/tt_session.json"

_seen_messages: dict[str, set] = {}
_init_lock = asyncio.Lock()

# Global Playwright state
_pw   = None
_browser = None
_ctx  = None
_pg   = None

# ---------------------------------------------------------------------------
# Browser / session helpers
# ---------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver',   { get: () => undefined });
Object.defineProperty(navigator, 'plugins',      { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages',    { get: () => ['fr-FR','fr','en-US','en'] });
window.chrome = { runtime: {} };
"""

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Mobile Safari/537.36 TikTok/30.0.0"
)


async def _new_context(browser):
    ctx = await browser.new_context(
        user_agent=MOBILE_UA,
        viewport={"width": 412, "height": 915},
        locale="fr-FR",
        timezone_id="Europe/Paris",
        has_touch=True,
    )
    await ctx.add_init_script(STEALTH_JS)
    return ctx


async def _save_cookies(ctx):
    cookies = await ctx.cookies()
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f)


async def _load_cookies(ctx):
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)
            await ctx.add_cookies(cookies)
            logger.info("Cookies TikTok chargés depuis disque")
        except Exception as e:
            logger.warning(f"Impossible de charger les cookies: {e}")


async def _type_slow(page, selector: str, text: str):
    await page.click(selector, timeout=10000)
    await asyncio.sleep(random.uniform(0.3, 0.6))
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.04, 0.12))


async def _do_login(page, ctx):
    logger.info("Connexion TikTok (email/mot de passe)…")
    await page.goto(
        "https://www.tiktok.com/login/phone-or-email/email",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    await asyncio.sleep(random.uniform(2, 4))

    await _type_slow(page, 'input[name="username"]', TT_USERNAME)
    await asyncio.sleep(random.uniform(0.4, 0.8))

    await _type_slow(page, 'input[type="password"]', TT_PASSWORD)
    await asyncio.sleep(random.uniform(0.5, 1.0))

    btn = await page.query_selector(
        'button[data-e2e="login-button"], button[type="submit"]'
    )
    if btn:
        await btn.click()
    await asyncio.sleep(6)

    await _save_cookies(ctx)
    logger.info("Connexion TikTok réussie")


async def _is_logged_in(page) -> bool:
    try:
        result = await page.evaluate(
            "() => document.cookie.includes('sid_guard') || document.cookie.includes('sessionid')"
        )
        return bool(result)
    except Exception:
        return False


async def get_page():
    """Retourne une page Playwright connectée à TikTok (initialise si besoin)."""
    global _pw, _browser, _ctx, _pg

    async with _init_lock:
        if _pg and not _pg.is_closed():
            try:
                await _pg.evaluate("1")
                if await _is_logged_in(_pg):
                    return _pg
            except Exception:
                pass

        from playwright.async_api import async_playwright

        if _pw is None:
            _pw = await async_playwright().start()

        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--window-size=412,915",
            ],
        )

        _ctx = await _new_context(_browser)
        await _load_cookies(_ctx)
        _pg = await _ctx.new_page()

        await _pg.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if not await _is_logged_in(_pg):
            await _do_login(_pg, _ctx)

        return _pg


# ---------------------------------------------------------------------------
# TikTok internal API helpers
# ---------------------------------------------------------------------------

async def _tiktok_api(page, path: str, params: dict = {}) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://www.tiktok.com{path}?{qs}" if qs else f"https://www.tiktok.com{path}"
    result = await page.evaluate(
        """async (url) => {
            const r = await fetch(url, {
                credentials: 'include',
                headers: {
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': 'https://www.tiktok.com/messages'
                }
            });
            return await r.json();
        }""",
        url,
    )
    return result


async def _send_dm_api(page, user_id: str, text: str) -> dict:
    result = await page.evaluate(
        """async ([userId, text]) => {
            const r = await fetch('https://www.tiktok.com/api/v2/conversation/msg/', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': 'https://www.tiktok.com/messages'
                },
                body: `receiver_id=${userId}&content=${encodeURIComponent(JSON.stringify({text}))}&type=1`
            });
            return await r.json();
        }""",
        [user_id, text],
    )
    return result


async def _get_user_id(page, username: str) -> Optional[str]:
    try:
        result = await page.evaluate(
            """async (username) => {
                const r = await fetch(`https://www.tiktok.com/@${username}`, {
                    credentials: 'include',
                    headers: { 'Accept': 'text/html' }
                });
                const html = await r.text();
                const m = html.match(/"uniqueId":"([^"]+)","id":"(\\d+)"/);
                return m ? m[2] : null;
            }""",
            username,
        )
        return result
    except Exception as e:
        logger.warning(f"Impossible de récupérer l'id de @{username}: {e}")
        return None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SendDMRequest(BaseModel):
    username: str
    text: str


class ReplyRequest(BaseModel):
    thread_id: str
    text: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "platform": "tiktok",
        "account": TT_USERNAME,
        "seen_threads": len(_seen_messages),
    }


@app.get("/inbox")
async def get_inbox(limit: int = 20):
    try:
        page = await get_page()
        await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        data = await _tiktok_api(
            page,
            "/api/v2/conversation/inbox/",
            {"count": limit, "cursor": "0"},
        )

        conversations = data.get("data", {}).get("conversation_list", [])
        new_messages = []

        for conv in conversations:
            conv_id = str(conv.get("conversation_id", ""))
            if not conv_id:
                continue

            if conv_id not in _seen_messages:
                _seen_messages[conv_id] = set()

            thread_data = await _tiktok_api(
                page,
                "/api/v2/conversation/msg/",
                {"conversation_id": conv_id, "count": "10", "cursor": "0"},
            )

            messages = thread_data.get("data", {}).get("message_list", [])

            for msg in messages:
                msg_id = str(msg.get("message_id", ""))
                sender_id = str(msg.get("sender_id", ""))

                if not msg_id or msg_id in _seen_messages[conv_id]:
                    continue

                content = msg.get("content", "{}")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        content = {}
                text = content.get("text", "")

                if not text:
                    _seen_messages[conv_id].add(msg_id)
                    continue

                my_id = str(data.get("data", {}).get("self_id", ""))
                if sender_id == my_id:
                    _seen_messages[conv_id].add(msg_id)
                    continue

                sender_username = ""
                for u in conv.get("users", []):
                    if str(u.get("uid", "")) == sender_id:
                        sender_username = u.get("unique_id", "") or u.get("nickname", "")
                        break

                new_messages.append({
                    "thread_id": conv_id,
                    "message_id": msg_id,
                    "sender_id": sender_id,
                    "sender_username": sender_username,
                    "text": text,
                    "timestamp": str(msg.get("create_time", "")),
                    "platform": "tiktok",
                })

        return {"messages": new_messages, "count": len(new_messages)}

    except Exception as e:
        logger.error(f"Erreur inbox TikTok: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/inbox/mark-seen")
async def mark_seen(thread_id: str, message_id: str):
    if thread_id not in _seen_messages:
        _seen_messages[thread_id] = set()
    _seen_messages[thread_id].add(message_id)
    return {"marked": True}


@app.post("/dm/send")
async def send_dm(req: SendDMRequest):
    try:
        page = await get_page()
        await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        user_id = await _get_user_id(page, req.username)
        if not user_id:
            raise HTTPException(status_code=404, detail=f"Utilisateur @{req.username} introuvable")

        result = await _send_dm_api(page, user_id, req.text)
        logger.info(f"DM envoyé à @{req.username}")
        return {"success": True, "recipient": req.username, "response": result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur dm/send: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/thread/reply")
async def reply_to_thread(req: ReplyRequest):
    try:
        page = await get_page()
        await page.goto("https://www.tiktok.com/messages", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        result = await page.evaluate(
            """async ([convId, text]) => {
                const r = await fetch('https://www.tiktok.com/api/v2/conversation/msg/', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Referer': 'https://www.tiktok.com/messages'
                    },
                    body: `conversation_id=${convId}&content=${encodeURIComponent(JSON.stringify({text}))}&type=1`
                });
                return await r.json();
            }""",
            [req.thread_id, req.text],
        )

        logger.info(f"Réponse envoyée dans thread {req.thread_id}")
        return {"success": True, "thread_id": req.thread_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erreur thread/reply: {e}")
        raise HTTPException(status_code=500, detail=str(e))


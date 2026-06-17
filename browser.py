# ===================================================
# browser.py v3 — استراتيجية مزدوجة
# curl_cffi للصفحات البسيطة + rebrowser للصفحات الثقيلة
# ===================================================

import asyncio
import random
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}


# ═══════════════════════════════════════════════════════════════
# طبقة 1: curl_cffi — أسرع 10x، بدون متصفح كامل
# يتجاوز Cloudflare عبر TLS fingerprint حقيقي
# ═══════════════════════════════════════════════════════════════

async def fetch_with_curl(url: str, timeout: int = 20) -> str:
    """
    يجلب HTML الصفحة بسرعة عبر curl_cffi.
    يتجاوز Cloudflare بدون فتح متصفح كامل.
    """
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome124") as session:
        r = await session.get(
            url,
            timeout=timeout,
            headers={
                "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT":             "1",
            }
        )
        r.raise_for_status()
        return r.text


# ═══════════════════════════════════════════════════════════════
# طبقة 2: rebrowser — Chromium بدون fingerprint leaks
# ═══════════════════════════════════════════════════════════════

async def _smart_cf_wait(page, max_wait: int = 15):
    title = (await page.title()).lower()
    if "just a moment" not in title and "checking" not in title:
        return
    logger.info("[CF] challenge — انتظار ذكي...")
    for _ in range(max_wait * 2):
        await asyncio.sleep(0.5)
        title = (await page.title()).lower()
        if "just a moment" not in title and "checking" not in title:
            logger.info("[CF] ✅ تم التجاوز")
            await asyncio.sleep(0.5)
            return
    logger.warning("[CF] ⚠️ لم يُتجاوز")


async def _block_heavy(route, request):
    if request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


@asynccontextmanager
async def get_browser_page(url: str = None, block_resources: bool = True):
    """
    يستخدم rebrowser-playwright (Chromium بدون fingerprint leaks).
    متوافق 100% مع wtr_lab.py و scraper.py الحاليين.
    """
    try:
        from rebrowser_playwright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright

    playwright_obj = None
    browser = None
    context = None
    page    = None

    try:
        playwright_obj = await async_playwright().start()
        browser = await playwright_obj.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
            ],
        )
        context = await browser.new_context(
            locale=random.choice(["en-US", "en-GB"]),
            timezone_id=random.choice(["America/New_York", "Europe/London", "Europe/Paris"]),
            extra_http_headers={
                "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT":             "1",
            },
            bypass_csp=True,
        )
        page = await context.new_page()

        if block_resources:
            await page.route("**/*", _block_heavy)

        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=28000)
                await _smart_cf_wait(page)
            except Exception as e:
                logger.warning(f"[BROWSER] goto: {e}")

        yield page

    finally:
        for obj in [page, context, browser]:
            if obj:
                try: await obj.close()
                except: pass
        if playwright_obj:
            try: await playwright_obj.stop()
            except: pass


# ═══════════════════════════════════════════════════════════════
# دوال مساعدة
# ═══════════════════════════════════════════════════════════════

async def human_scroll(page, times: int = 3, delay: float = 1.5):
    effective = max(delay * 0.25, 0.25)
    for _ in range(times):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(effective + random.uniform(0.1, 0.2))


async def human_delay(min_ms: int = 500, max_ms: int = 2000):
    await asyncio.sleep(random.randint(min_ms // 4, max_ms // 4) / 1000)


async def wait_for_content(page, selector: str, timeout: int = 15000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:
        return False

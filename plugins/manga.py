# plugins/manga_scraper.py
"""
plugins/manga_scraper.py
endpoint: POST /manga/extract-chapter
يدعم Cloudflare عبر استراتيجية متدرجة:
  1) curl_cffi  (محاكاة بصمة TLS لمتصفح حقيقي - الأسرع)
  2) cloudscraper (حل تحدي Cloudflare JS البسيط)
  3) Playwright + stealth (متصفح كامل - يُستخدم فقط عند فشل الطرق الأخف)
"""

import re
import asyncio
import logging
import random
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

logger = logging.getLogger("manga_scraper")

DESCRIPTION = "كشط واستخراج روابط صور فصول المانجا والمانهوا المترجمة للعربية، مع دعم Cloudflare (استراتيجية متدرجة)."

DOCKERFILE_DEPS = [
    "libgconf-2-4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libgdk-pixbuf2.0-0",
    "libgtk-3-0",
    "libgbm-dev",
    "libnss3",
    "libxss1",
    "libasound2",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

LATEST_CHAPTER_FLAG = "%%"

SLUG_OVERRIDES = {
    "one-piece": "pieceone",
    "one piece": "pieceone",
}

BASE_DOMAIN = "https://lek-manga.net"


def _normalize_slug(manga_name: str) -> str:
    key = manga_name.lower().strip()
    return SLUG_OVERRIDES.get(key, key)


def _is_chapter_link(href: str, chapter_number) -> bool:
    href_clean = href.split('?')[0]
    pattern = re.compile(
        rf"/(?:chapter-?)?0*{re.escape(str(chapter_number))}(?:/|$)",
        re.IGNORECASE
    )
    return bool(pattern.search(href_clean))


def _find_chapter_link(html_content: str, chapter_number) -> str | None:
    soup = BeautifulSoup(html_content, "lxml")
    selectors = [
        "li.wp-manga-chapter a",
        ".version-chap li a",
        "ul.main.version-chap a",
        ".chapter-list a"
    ]
    for selector in selectors:
        links = soup.select(selector)
        if links:
            for a in links:
                href = a.get("href", "").strip()
                if href and _is_chapter_link(href, chapter_number):
                    return href
    all_links = soup.find_all("a", href=True)
    for a in all_links:
        href = a["href"].strip()
        if "/manga/" in href and _is_chapter_link(href, chapter_number):
            return href
    return None


def _find_latest_chapter_link(html_content: str, manga_main_url: str) -> str | None:
    soup = BeautifulSoup(html_content, "lxml")
    base = manga_main_url.rstrip("/")
    selectors = [
        "li.wp-manga-chapter a",
        ".version-chap li a",
        "ul.main.version-chap a",
        ".chapter-list a"
    ]
    for selector in selectors:
        links = soup.select(selector)
        if links:
            for a in links:
                href = a.get("href", "").strip().rstrip("/")
                if not href or "/manga/" not in href:
                    continue
                if href == base:
                    continue
                return href + "/"
    all_links = soup.find_all("a", href=True)
    for a in all_links:
        href = a["href"].strip().rstrip("/")
        if not href or "/manga/" not in href:
            continue
        if href == base:
            continue
        return href + "/"
    return None


def _extract_images_from_html(html_content: str) -> list:
    """يستخرج روابط صور الفصل من نص HTML خام (دون متصفح)."""
    soup = BeautifulSoup(html_content, "lxml")
    container = (
        soup.find(class_="wp-manga-section") or
        soup.find(class_="reading-content") or
        soup.find(class_="page-break")
    )
    images = container.find_all("img") if container else soup.find_all("img")
    valid = []
    for img in images:
        url = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if not url:
            continue
        url = url.strip()
        if any(bad in url.lower() for bad in ["emoji", "logo", "avatar", "banner", "icon"]):
            continue
        if "uploads" in url or "wp-content" in url:
            if any(ext in url.lower() for ext in [".jpg", ".png", ".webp", ".jpeg"]):
                valid.append(url)
    return list(dict.fromkeys(valid))


# ============================================================
#   المستوى 1: curl_cffi — محاكاة بصمة TLS لمتصفح حقيقي
# ============================================================

def _fetch_with_curl_cffi(url: str) -> str | None:
    if not HAS_CURL_CFFI:
        return None
    try:
        resp = cffi_requests.get(
            url,
            impersonate="chrome120",
            timeout=30,
            headers={
                "Accept-Language": "ar,en;q=0.9",
                "User-Agent": USER_AGENT,
            },
        )
        if resp.status_code == 200 and "cf-browser-verification" not in resp.text.lower() \
                and "checking your browser" not in resp.text.lower():
            return resp.text
        print(f"[manga][curl_cffi] استجابة مشبوهة status={resp.status_code} لـ {url}", flush=True)
    except Exception as e:
        print(f"[manga][curl_cffi] فشل: {e}", flush=True)
    return None


# ============================================================
#   المستوى 2: cloudscraper — حل تحدي Cloudflare JS البسيط
# ============================================================

_cloudscraper_session = None


def _get_cloudscraper_session():
    global _cloudscraper_session
    if _cloudscraper_session is None and HAS_CLOUDSCRAPER:
        _cloudscraper_session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return _cloudscraper_session


def _fetch_with_cloudscraper(url: str) -> str | None:
    if not HAS_CLOUDSCRAPER:
        return None
    try:
        scraper = _get_cloudscraper_session()
        resp = scraper.get(url, timeout=30, headers={"Accept-Language": "ar,en;q=0.9"})
        if resp.status_code == 200 and "checking your browser" not in resp.text.lower():
            return resp.text
        print(f"[manga][cloudscraper] استجابة مشبوهة status={resp.status_code} لـ {url}", flush=True)
    except Exception as e:
        print(f"[manga][cloudscraper] فشل: {e}", flush=True)
    return None


def _fetch_html_lightweight(url: str) -> str | None:
    """يحاول curl_cffi ثم cloudscraper. يرجع None إذا فشل كلاهما."""
    html = _fetch_with_curl_cffi(url)
    if html:
        return html
    html = _fetch_with_cloudscraper(url)
    if html:
        return html
    return None


# ============================================================
#   المستوى 3: Playwright + stealth — متصفح كامل (الأبطأ)
# ============================================================

def _wait_for_page_ready(page, timeout=45000):
    """تنتظر حتى تختفي شاشة Cloudflare ويظهر محتوى الموقع الفعلي."""
    try:
        page.wait_for_selector("#cf-challenge-widget", state="detached", timeout=timeout)
    except Exception:
        pass

    # وقت إضافي لإتمام تنفيذ تحدي JS قبل الفحص عن المحتوى الفعلي
    page.wait_for_timeout(random.randint(2000, 4000))

    try:
        page.wait_for_selector(".site-header, .main-navigation, .profile-manga", timeout=timeout)
    except Exception:
        page.wait_for_timeout(6000)
        page.reload()
        page.wait_for_selector(".site-header, .main-navigation, .profile-manga", timeout=timeout)


def _new_stealth_context(p):
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    try:
        browser = p.chromium.launch(headless=True, channel="chrome", args=browser_args)
    except Exception:
        # في حال عدم توفر قناة chrome الحقيقية على السيرفر، نعود لـ chromium الافتراضي
        browser = p.chromium.launch(headless=True, args=browser_args)

    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="ar-EG",
        timezone_id="Africa/Cairo",
        extra_http_headers={
            "Accept-Language": "ar,en;q=0.9",
            "DNT": "1",
        }
    )
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ar', 'en']});
        window.chrome = { runtime: {} };
    """)
    return browser, context


def _new_stealth_page(context):
    page = context.new_page()
    if HAS_STEALTH:
        try:
            stealth_sync(page)
        except Exception as e:
            print(f"[manga][stealth] تعذر تطبيق stealth: {e}", flush=True)
    return page


def _fetch_html_with_playwright(context, url: str) -> str:
    page = _new_stealth_page(context)
    try:
        page.goto(url, wait_until="networkidle", timeout=90000)
        _wait_for_page_ready(page)
        return page.content()
    finally:
        page.close()


def _extract_images_via_playwright(context, chapter_url: str) -> list:
    page = _new_stealth_page(context)
    try:
        page.goto(chapter_url, wait_until="networkidle", timeout=90000)
        _wait_for_page_ready(page)
        try:
            page.wait_for_selector("img[data-src], img[data-lazy-src], img[src]", timeout=15000)
        except Exception:
            page.wait_for_timeout(3000)
        html = page.content()
        return _extract_images_from_html(html)
    finally:
        page.close()


def _search_manga_main_url_lightweight(query: str) -> str | None:
    search_url = f"{BASE_DOMAIN}/?s={quote(query)}&post_type=wp-manga"
    html = _fetch_html_lightweight(search_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    candidates = (
        soup.select("div.c-tabs-item__content .post-title a") or
        soup.select(".search-wrap .post-title a") or
        soup.select("a[href*='/manga/']")
    )
    for a in candidates:
        href = (a.get("href") or "").strip()
        if "/manga/" in href and href != search_url:
            return href.rstrip("/") + "/"
    return None


def _search_manga_main_url_playwright(context, query: str) -> str | None:
    search_url = f"{BASE_DOMAIN}/?s={quote(query)}&post_type=wp-manga"
    html = _fetch_html_with_playwright(context, search_url)
    soup = BeautifulSoup(html, "lxml")
    candidates = (
        soup.select("div.c-tabs-item__content .post-title a") or
        soup.select(".search-wrap .post-title a") or
        soup.select("a[href*='/manga/']")
    )
    for a in candidates:
        href = (a.get("href") or "").strip()
        if "/manga/" in href and href != search_url:
            return href.rstrip("/") + "/"
    return None


# ============================================================
#   المنطق الرئيسي: يبدأ خفيف، ويتصاعد فقط عند الحاجة
# ============================================================

def _scrape_manga_chapter(manga_name: str, chapter_number):
    is_latest = str(chapter_number).strip() == LATEST_CHAPTER_FLAG
    effective_slug = _normalize_slug(manga_name)
    print(f"[manga] بدء: '{manga_name}' → slug '{effective_slug}'", flush=True)

    # ---------- المرحلة A: محاولة خفيفة بالكامل (curl_cffi / cloudscraper) ----------
    if not is_latest:
        direct_url = f"{BASE_DOMAIN}/manga/{effective_slug}/{chapter_number}/"
        print(f"[manga][light] محاولة مباشرة: {direct_url}", flush=True)
        html = _fetch_html_lightweight(direct_url)
        if html:
            images = _extract_images_from_html(html)
            if images:
                return {
                    "ok": True, "error": None, "images": images,
                    "chapter_url": direct_url, "detected_chapter": str(chapter_number),
                }

    if effective_slug != manga_name.lower():
        main_url = f"{BASE_DOMAIN}/manga/{effective_slug}/"
    else:
        search_query = manga_name.replace("-", " ").replace("_", " ").strip()
        resolved = _search_manga_main_url_lightweight(search_query)
        main_url = resolved if resolved else f"{BASE_DOMAIN}/manga/{effective_slug}/"

    main_html = _fetch_html_lightweight(main_url)
    if main_html:
        chapter_url = (
            _find_latest_chapter_link(main_html, main_url) if is_latest
            else _find_chapter_link(main_html, chapter_number)
        )
        if chapter_url:
            html = _fetch_html_lightweight(chapter_url)
            if html:
                images = _extract_images_from_html(html)
                if images:
                    detected = chapter_url.rstrip("/").split("/")[-1]
                    print(f"[manga][light] ✅ نجح بدون متصفح: {chapter_url}", flush=True)
                    return {
                        "ok": True, "error": None, "images": images,
                        "chapter_url": chapter_url, "detected_chapter": detected,
                    }

    # ---------- المرحلة B: المتصفح الكامل (Playwright + stealth) ----------
    print("[manga] الطرق الخفيفة فشلت، الانتقال لـ Playwright + stealth", flush=True)
    with sync_playwright() as p:
        browser, context = _new_stealth_context(p)
        try:
            if not is_latest:
                direct_url = f"{BASE_DOMAIN}/manga/{effective_slug}/{chapter_number}/"
                try:
                    images = _extract_images_via_playwright(context, direct_url)
                    if images:
                        return {
                            "ok": True, "error": None, "images": images,
                            "chapter_url": direct_url, "detected_chapter": str(chapter_number),
                        }
                except Exception as e:
                    print(f"[manga][pw] فشل المباشرة: {e}", flush=True)

            if effective_slug != manga_name.lower():
                main_url = f"{BASE_DOMAIN}/manga/{effective_slug}/"
            else:
                search_query = manga_name.replace("-", " ").replace("_", " ").strip()
                try:
                    resolved = _search_manga_main_url_playwright(context, search_query)
                    main_url = resolved if resolved else f"{BASE_DOMAIN}/manga/{effective_slug}/"
                except Exception as e:
                    print(f"[manga][pw] فشل البحث: {e}", flush=True)
                    main_url = f"{BASE_DOMAIN}/manga/{effective_slug}/"

            try:
                html = _fetch_html_with_playwright(context, main_url)
            except Exception as e:
                return {"ok": False, "error": f"تعذر الوصول: {e}", "images": [], "chapter_url": None, "detected_chapter": None}

            chapter_url = (
                _find_latest_chapter_link(html, main_url) if is_latest
                else _find_chapter_link(html, chapter_number)
            )

            if not chapter_url:
                fallback = f"{BASE_DOMAIN}/manga/{effective_slug}/{chapter_number}/"
                print(f"[manga][pw] محاولة تخمينية: {fallback}", flush=True)
                try:
                    images = _extract_images_via_playwright(context, fallback)
                    if images:
                        chapter_url = fallback
                except Exception:
                    pass

            if not chapter_url:
                return {
                    "ok": False,
                    "error": f"لم يُعثر على الفصل {'الأخير' if is_latest else chapter_number}",
                    "images": [], "chapter_url": None, "detected_chapter": None,
                }

            detected = chapter_url.rstrip("/").split("/")[-1]
            print(f"[manga][pw] ✅ الفصل: {chapter_url} (مكتشف: {detected})", flush=True)

            try:
                images = _extract_images_via_playwright(context, chapter_url)
            except Exception as e:
                print(f"[manga][pw] فشل استخراج الصور: {e}", flush=True)
                images = []

            if not images:
                return {
                    "ok": False, "error": "فشل استخراج الصور",
                    "images": [], "chapter_url": chapter_url, "detected_chapter": detected,
                }

            return {
                "ok": True, "error": None, "images": images,
                "chapter_url": chapter_url, "detected_chapter": detected,
            }
        finally:
            browser.close()


def register(app):
    @app.post("/manga/extract-chapter")
    async def extract_manga_chapter(request: Request):
        try:
            body = await request.json()
            manga_name = body.get("manga_name")
            chapter_number = body.get("chapter_number")
            print(f"[manga] طلب: {body}", flush=True)

            if not manga_name or chapter_number is None:
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "message": "أرسل manga_name و chapter_number"}
                )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _scrape_manga_chapter, manga_name, chapter_number)

            if not result["ok"]:
                status = 404 if "لم يُعثر" in (result["error"] or "") else 500
                return JSONResponse(status_code=status, content={"status": "error", "message": result["error"]})

            return JSONResponse({
                "status": "ok",
                "manga_name": manga_name,
                "chapter_number": result.get("detected_chapter") or chapter_number,
                "total_pages": len(result["images"]),
                "images": result["images"],
            })

        except Exception as e:
            print(f"[manga] خطأ: {e}", flush=True)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# plugins/manga_scraper.py
"""
plugins/manga_scraper.py
endpoint: POST /manga/extract-chapter
يدعم Cloudflare عبر استراتيجية متدرجة:
  1) curl_cffi  (محاكاة بصمة TLS لمتصفح حقيقي - الأسرع)
  2) cloudscraper (حل تحدي Cloudflare JS البسيط)
  3) Playwright + stealth (متصفح كامل - يُستخدم فقط عند فشل الطرق الأخف)

يدعم الموقعين:
  - lek-manga.net (بنية Madara)
  - mangatek.com (بنية حديثة مع /reader/)
"""

import re
import asyncio
import logging
import random
from urllib.parse import quote, urlparse
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

# ============================================================
#  🔧 إعدادات المواقع (SITE_CONFIGS)
# ============================================================
SITE_CONFIGS = {
    "lek-manga.net": {
        "base_url": "https://lek-manga.net",
        "chapter_list_selectors": [
            "li.wp-manga-chapter a",
            ".version-chap li a",
            "ul.main.version-chap a",
            ".chapter-list a"
        ],
        "image_container_selectors": [
            ".wp-manga-section",
            ".reading-content",
            ".page-break"
        ],
        "image_attributes": ["data-src", "data-lazy-src", "src"],
        "search_url_template": "https://lek-manga.net/?s={query}&post_type=wp-manga",
        "search_result_selectors": [
            "div.c-tabs-item__content .post-title a",
            ".search-wrap .post-title a",
            "a[href*='/manga/']"
        ],
        "slug_overrides": {
            "one-piece": "pieceone",
            "one piece": "pieceone",
        },
        "chapter_path_pattern": r"/manga/([^/]+)/(\d+)/?",
        "chapter_url_pattern": re.compile(r"/(?:chapter-?)?0*(\d+)(?:/|$)"),
        "manga_main_path": "/manga/{slug}/",
        "chapter_url_template": "/manga/{slug}/{chapter}/",
        "image_ignore_patterns": ["emoji", "logo", "avatar", "banner", "icon"],
        "image_required_patterns": ["uploads", "wp-content"],
        "image_extensions": [".jpg", ".png", ".webp", ".jpeg"],
    },
    "mangatek.com": {
        "base_url": "https://mangatek.com",
        "chapter_list_selectors": [
            "a[href*='/reader/']",  # جميع روابط القراءة
        ],
        "image_container_selectors": [
            "main.flex-grow",   # الأكثر تحديداً (مطابقة واحدة فقط) - نجربه أولاً
            ".chapter-images",
            ".flex-grow",       # احتياطي، قد يطابق عناصر متعددة
            "main",
        ],
        # mangatek يحمّل الصور أحياناً عبر data-src (lazy load) قبل ما الـ JS يحطها في src
        "image_attributes": ["src", "data-src", "data-lazy-src"],
        "search_url_template": None,  # لا يوجد محرك بحث واضح
        "search_result_selectors": [],
        "slug_overrides": {},
        "chapter_path_pattern": r"/reader/([^/]+)/(\d+)/?",
        "chapter_url_pattern": re.compile(r"/reader/[^/]+/(\d+)/?$"),
        "manga_main_path": "/reader/{slug}",
        "chapter_url_template": "/reader/{slug}/{chapter}",
        "image_ignore_patterns": ["decorations", "emoji", "logo", "avatar", "banner", "icon"],
        # روابط صور الفصل الحقيقية شكلها:
        # https://api.mangatek.com/api/chapters/stream/<id>/<id>?v=1  (بدون امتداد ملف!)
        "image_required_patterns": ["chapters/stream"],
        "image_extensions": [],  # متعمد تركها فاضية، الروابط مفيهاش امتداد أصلاً
    }
}

# ترتيب المحاولة: الأكثر استخداماً أولاً
SITE_ORDER = ["mangatek.com", "lek-manga.net"]


def _get_site_config(url_or_domain: str) -> dict | None:
    """ترجع إعدادات الموقع بناءً على الرابط أو النطاق."""
    domain = urlparse(url_or_domain).netloc if "://" in url_or_domain else url_or_domain
    for site_domain, config in SITE_CONFIGS.items():
        if site_domain in domain:
            return config
    return None


def _normalize_slug(manga_name: str, site_config: dict) -> str:
    """تطبيق استثناءات الـ Slug حسب الموقع."""
    key = manga_name.lower().strip()
    overrides = site_config.get("slug_overrides", {})
    return overrides.get(key, key)


def _is_chapter_link(href: str, chapter_number, site_config: dict) -> bool:
    """تتحقق من أن الرابط يشير إلى الفصل المطلوب حسب نمط الموقع."""
    href_clean = href.split('?')[0]
    pattern = site_config.get("chapter_url_pattern")
    if pattern:
        match = pattern.search(href_clean)
        if match:
            # نأخذ آخر مجموعة أرقام
            nums = re.findall(r'\d+', href_clean)
            if nums and int(nums[-1]) == int(chapter_number):
                return True
    # fallback
    return bool(re.search(rf"/(?:chapter-?)?0*{re.escape(str(chapter_number))}(?:/|$)", href_clean, re.IGNORECASE))


def _find_chapter_link(html_content: str, chapter_number, site_config: dict) -> str | None:
    """يبحث عن رابط الفصل حسب إعدادات الموقع."""
    soup = BeautifulSoup(html_content, "lxml")
    selectors = site_config.get("chapter_list_selectors", [])
    for selector in selectors:
        links = soup.select(selector)
        for a in links:
            href = a.get("href", "").strip()
            if href and _is_chapter_link(href, chapter_number, site_config):
                return href
    # fallback: البحث في كل الروابط التي تحتوي على /manga/ أو /reader/
    all_links = soup.find_all("a", href=True)
    for a in all_links:
        href = a["href"].strip()
        if ("/manga/" in href or "/reader/" in href) and _is_chapter_link(href, chapter_number, site_config):
            return href
    return None


def _find_latest_chapter_link(html_content: str, manga_main_url: str, site_config: dict) -> str | None:
    """يبحث عن أحدث فصل (الأول في القائمة)."""
    soup = BeautifulSoup(html_content, "lxml")
    base = manga_main_url.rstrip("/")
    selectors = site_config.get("chapter_list_selectors", [])
    for selector in selectors:
        links = soup.select(selector)
        for a in links:
            href = a.get("href", "").strip().rstrip("/")
            if not href:
                continue
            if ("/manga/" in href or "/reader/" in href) and href != base:
                return href + "/"
    # fallback
    all_links = soup.find_all("a", href=True)
    for a in all_links:
        href = a["href"].strip().rstrip("/")
        if not href:
            continue
        if ("/manga/" in href or "/reader/" in href) and href != base:
            return href + "/"
    return None


def _extract_images_from_html(html_content: str, site_config: dict) -> list:
    """يستخرج روابط صور الفصل من نص HTML حسب إعدادات الموقع."""
    soup = BeautifulSoup(html_content, "lxml")
    container_selectors = site_config.get("image_container_selectors", [])
    container = None
    for selector in container_selectors:
        container = soup.select_one(selector)
        if container:
            break
    if not container:
        container = soup

    images = container.find_all("img")
    valid = []
    attrs = site_config.get("image_attributes", ["src"])
    ignore_patterns = site_config.get("image_ignore_patterns", [])
    required_patterns = site_config.get("image_required_patterns", [])
    extensions = site_config.get("image_extensions", [])

    for img in images:
        url = None
        for attr in attrs:
            url = img.get(attr)
            if url:
                break
        if not url:
            continue
        url = url.strip()

        # تجاهل الصور غير المرغوب فيها
        if any(bad in url.lower() for bad in ignore_patterns):
            continue

        # إذا كان هناك أنماط مطلوبة، تحقق منها، ومطابقتها كافية وحدها للقبول
        # (مفيد لمواقع زي mangatek اللي روابط صورها API بدون امتداد ملف، مثل:
        #  https://api.mangatek.com/api/chapters/stream/<id>/<id>?v=1)
        if required_patterns:
            if any(req in url.lower() for req in required_patterns):
                valid.append(url)
                continue
            else:
                continue

        # التحقق من امتدادات الصور (إذا كانت موجودة)
        if extensions:
            if any(ext in url.lower() for ext in extensions):
                valid.append(url)
        else:
            # إذا لم تكن هناك امتدادات محددة، نقبل أي رابط يحتوي على image أو uploads
            if "image" in url.lower() or "uploads" in url.lower():
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

_CF_CHALLENGE_MARKERS = [
    "just a moment",          # عنوان صفحة تحدي Cloudflare الحديثة
    "checking your browser",
    "enable javascript and cookies to continue",
    "cf-turnstile",
    "challenge-platform",
    "cf-chl-",
    "verify you are human",
]


def _looks_like_cf_challenge(page) -> bool:
    """تتحقق هل الصفحة الحالية لسه شاشة تحدي Cloudflare ولا لأ، بفحص العنوان والمحتوى فعلياً."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if any(marker in title for marker in _CF_CHALLENGE_MARKERS):
        return True
    try:
        html_snippet = page.content()[:5000].lower()
    except Exception:
        html_snippet = ""
    return any(marker in html_snippet for marker in _CF_CHALLENGE_MARKERS)


def _wait_for_page_ready(page, timeout=45000):
    """تنتظر حتى تختفي شاشة تحدي Cloudflare (Turnstile/JS challenge) ويظهر محتوى الموقع الفعلي.

    ملاحظة: السيليكتور القديم #cf-challenge-widget مش موجود غالباً في تحديات
    Cloudflare الحديثة (Turnstile)، فلو اعتمدنا عليه بس، wait_for_selector(state="detached")
    بيرجع فوراً (لأن العنصر أصلاً مش موجود) من غير ما ينتظر التحدي الحقيقي يخلص.
    بدل كده، بنفحص فعلياً عنوان/محتوى الصفحة ونعيد المحاولة لحد ما التحدي يختفي أو الوقت يخلص.
    """
    deadline_ms = timeout
    poll_interval_ms = 1500
    elapsed_ms = 0

    # أول استقرار بسيط للصفحة
    page.wait_for_timeout(random.randint(1500, 2500))

    while elapsed_ms < deadline_ms:
        if not _looks_like_cf_challenge(page):
            break
        page.wait_for_timeout(poll_interval_ms)
        elapsed_ms += poll_interval_ms
    else:
        # خلص الوقت ولسه شكله تحدي: نجرب reload واحد أخير
        try:
            page.reload(wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

    try:
        page.wait_for_selector("body", timeout=15000)
    except Exception:
        page.wait_for_timeout(3000)


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


def _extract_images_via_playwright(context, chapter_url: str, site_config: dict) -> list:
    page = _new_stealth_page(context)
    try:
        page.goto(chapter_url, wait_until="networkidle", timeout=90000)
        _wait_for_page_ready(page)

        still_challenge = _looks_like_cf_challenge(page)
        try:
            page_title = page.title()
        except Exception:
            page_title = "?"
        if still_challenge:
            print(f"[manga][pw][تشخيص] لسه شكله تحدي Cloudflare بعد الانتظار! العنوان: '{page_title}' — {chapter_url}", flush=True)

        # انتظار ظهور الصور (نبني selector صحيح لكل سمة محتملة: img[src], img[data-src], ...)
        attrs = site_config.get("image_attributes", ["src"])
        selector = ", ".join(f"img[{attr}]" for attr in attrs)
        try:
            page.wait_for_selector(selector, timeout=15000)
        except Exception:
            page.wait_for_timeout(3000)
        html = page.content()
        total_imgs = html.lower().count("<img")
        result = _extract_images_from_html(html, site_config)
        print(f"[manga][pw][تشخيص] '{page_title}' — إجمالي <img> بالصفحة: {total_imgs} | بعد الفلترة: {len(result)} — {chapter_url}", flush=True)
        return result
    finally:
        page.close()


# ============================================================
#   دوال البحث الخاصة بكل موقع (خفيفة وثقيلة)
# ============================================================

def _search_manga_main_url_lightweight(query: str, site_config: dict) -> str | None:
    search_template = site_config.get("search_url_template")
    if not search_template:
        return None
    search_url = search_template.format(query=quote(query))
    html = _fetch_html_lightweight(search_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    selectors = site_config.get("search_result_selectors", [])
    for selector in selectors:
        links = soup.select(selector)
        for a in links:
            href = (a.get("href") or "").strip()
            if ("/manga/" in href or "/reader/" in href) and href != search_url:
                return href.rstrip("/") + "/"
    return None


def _search_manga_main_url_playwright(context, query: str, site_config: dict) -> str | None:
    search_template = site_config.get("search_url_template")
    if not search_template:
        return None
    search_url = search_template.format(query=quote(query))
    html = _fetch_html_with_playwright(context, search_url)
    soup = BeautifulSoup(html, "lxml")
    selectors = site_config.get("search_result_selectors", [])
    for selector in selectors:
        links = soup.select(selector)
        for a in links:
            href = (a.get("href") or "").strip()
            if ("/manga/" in href or "/reader/" in href) and href != search_url:
                return href.rstrip("/") + "/"
    return None


# ============================================================
#   المنطق الرئيسي: يحاول كل موقع بالتسلسل
# ============================================================

def _scrape_manga_chapter(manga_name: str, chapter_number):
    is_latest = str(chapter_number).strip() == LATEST_CHAPTER_FLAG
    print(f"[manga] بدء: '{manga_name}', الفصل: '{chapter_number}'", flush=True)

    # نمر على كل موقع في الترتيب المحدد
    for domain in SITE_ORDER:
        site_config = SITE_CONFIGS.get(domain)
        if not site_config:
            continue

        effective_slug = _normalize_slug(manga_name, site_config)
        base_url = site_config["base_url"]

        # ---------- المرحلة A: محاولة خفيفة (curl_cffi / cloudscraper) ----------
        print(f"[manga][light] محاولة {domain}...", flush=True)

        if not is_latest:
            chapter_path = site_config["chapter_url_template"].format(slug=effective_slug, chapter=chapter_number)
            direct_url = base_url + chapter_path
            print(f"[manga][light] مباشرة: {direct_url}", flush=True)
            html = _fetch_html_lightweight(direct_url)
            if html:
                images = _extract_images_from_html(html, site_config)
                if images:
                    return {
                        "ok": True, "error": None, "images": images,
                        "chapter_url": direct_url, "detected_chapter": str(chapter_number),
                        "site": domain
                    }

        # الحصول على الرابط الرئيسي للمانجا
        if effective_slug != manga_name.lower() and site_config.get("slug_overrides"):
            main_path = site_config["manga_main_path"].format(slug=effective_slug)
            main_url = base_url + main_path
        else:
            search_query = manga_name.replace("-", " ").replace("_", " ").strip()
            resolved = _search_manga_main_url_lightweight(search_query, site_config)
            if resolved:
                main_url = resolved
            else:
                main_path = site_config["manga_main_path"].format(slug=effective_slug)
                main_url = base_url + main_path

        print(f"[manga][light] رئيسية: {main_url}", flush=True)
        main_html = _fetch_html_lightweight(main_url)
        if main_html:
            chapter_url = (
                _find_latest_chapter_link(main_html, main_url, site_config) if is_latest
                else _find_chapter_link(main_html, chapter_number, site_config)
            )
            if chapter_url:
                # تأكد من أن الرابط مطلق
                if chapter_url.startswith("/"):
                    chapter_url = base_url + chapter_url
                elif not chapter_url.startswith("http"):
                    chapter_url = base_url + "/" + chapter_url.lstrip("/")

                html = _fetch_html_lightweight(chapter_url)
                if html:
                    images = _extract_images_from_html(html, site_config)
                    if images:
                        detected = chapter_url.rstrip("/").split("/")[-1]
                        print(f"[manga][light] ✅ نجح بدون متصفح على {domain}: {chapter_url}", flush=True)
                        return {
                            "ok": True, "error": None, "images": images,
                            "chapter_url": chapter_url, "detected_chapter": detected,
                            "site": domain
                        }

        # ---------- المرحلة B: المتصفح الكامل (Playwright + stealth) ----------
        print(f"[manga][pw] الطرق الخفيفة فشلت لـ {domain}، الانتقال لـ Playwright", flush=True)
        with sync_playwright() as p:
            browser, context = _new_stealth_context(p)
            try:
                tried_direct_url = None
                if not is_latest:
                    chapter_path = site_config["chapter_url_template"].format(slug=effective_slug, chapter=chapter_number)
                    direct_url = base_url + chapter_path
                    tried_direct_url = direct_url
                    try:
                        images = _extract_images_via_playwright(context, direct_url, site_config)
                        if images:
                            return {
                                "ok": True, "error": None, "images": images,
                                "chapter_url": direct_url, "detected_chapter": str(chapter_number),
                                "site": domain
                            }
                    except Exception as e:
                        print(f"[manga][pw] فشل المباشرة لـ {domain}: {e}", flush=True)

                if effective_slug != manga_name.lower() and site_config.get("slug_overrides"):
                    main_path = site_config["manga_main_path"].format(slug=effective_slug)
                    main_url = base_url + main_path
                else:
                    search_query = manga_name.replace("-", " ").replace("_", " ").strip()
                    try:
                        resolved = _search_manga_main_url_playwright(context, search_query, site_config)
                        main_url = resolved if resolved else base_url + site_config["manga_main_path"].format(slug=effective_slug)
                    except Exception as e:
                        print(f"[manga][pw] فشل البحث لـ {domain}: {e}", flush=True)
                        main_url = base_url + site_config["manga_main_path"].format(slug=effective_slug)

                try:
                    html = _fetch_html_with_playwright(context, main_url)
                except Exception as e:
                    print(f"[manga][pw] فشل فتح الرئيسية لـ {domain}: {e}", flush=True)
                    continue

                chapter_url = (
                    _find_latest_chapter_link(html, main_url, site_config) if is_latest
                    else _find_chapter_link(html, chapter_number, site_config)
                )

                if not chapter_url:
                    # محاولة تخمينية
                    fallback_path = site_config["chapter_url_template"].format(slug=effective_slug, chapter=chapter_number)
                    fallback_url = base_url + fallback_path
                    if fallback_url == tried_direct_url:
                        print(f"[manga][pw] تخطي المحاولة التخمينية، نفس الرابط جُرّب بالفعل: {fallback_url}", flush=True)
                    else:
                        print(f"[manga][pw] محاولة تخمينية: {fallback_url}", flush=True)
                        try:
                            images = _extract_images_via_playwright(context, fallback_url, site_config)
                            if images:
                                chapter_url = fallback_url
                        except Exception:
                            pass

                if not chapter_url:
                    continue

                if chapter_url.startswith("/"):
                    chapter_url = base_url + chapter_url
                elif not chapter_url.startswith("http"):
                    chapter_url = base_url + "/" + chapter_url.lstrip("/")

                detected = chapter_url.rstrip("/").split("/")[-1]
                print(f"[manga][pw] ✅ الفصل على {domain}: {chapter_url} (مكتشف: {detected})", flush=True)

                try:
                    images = _extract_images_via_playwright(context, chapter_url, site_config)
                except Exception as e:
                    print(f"[manga][pw] فشل استخراج الصور: {e}", flush=True)
                    images = []

                if images:
                    return {
                        "ok": True, "error": None, "images": images,
                        "chapter_url": chapter_url, "detected_chapter": detected,
                        "site": domain
                    }

            finally:
                browser.close()

    # إذا وصلنا إلى هنا، فشلت جميع المواقع
    return {
        "ok": False,
        "error": f"لم يتم العثور على الفصل {'الأخير' if is_latest else chapter_number} في أي موقع.",
        "images": [], "chapter_url": None, "detected_chapter": None,
        "site": None
    }


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
                status = 404 if "لم يتم العثور" in (result["error"] or "") else 500
                return JSONResponse(status_code=status, content={"status": "error", "message": result["error"]})

            return JSONResponse({
                "status": "ok",
                "manga_name": manga_name,
                "chapter_number": result.get("detected_chapter") or chapter_number,
                "total_pages": len(result["images"]),
                "images": result["images"],
                "site": result.get("site", "unknown")
            })

        except Exception as e:
            print(f"[manga] خطأ: {e}", flush=True)
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
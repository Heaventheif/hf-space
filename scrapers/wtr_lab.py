# scrapers/wtr_lab.py  v3 — سريع: curl_cffi أولاً + BeautifulSoup
# fallback للمتصفح فقط إذا فشل curl_cffi (Cloudflare challenge فعلي)

import asyncio
import logging
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from browser import fetch_with_curl, get_browser_page, human_delay, human_scroll

logger = logging.getLogger(__name__)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ═══════════════════════════════════════════════════════════════
# قائمة شاملة: إنجليزي + عربي — أي فقرة تحمل هذه الكلمات تُحذف
# ═══════════════════════════════════════════════════════════════
_PAYWALL_WORDS = [
    "wtr-lab", "cloudflare", "just a moment", "enable javascript",
    "next chapter", "prev chapter", "table of contents",
    "report chapter", "read more at", "translator:", "editor:",
    "ai translation requires", "guests can preview",
    "register for free", "sign up to read", "login to continue",
    "ad blocker detected", "disable adblock", "please disable",
    "subscribe to read", "unlock chapter", "premium chapter",
    "disable your ad", "ad-blocker", "adblock",
    "تتطلب الترجمة بالذكاء",
    "يمكن للضيوف معاينة",
    "تم اكتشاف مانع",
    "مانع الإعلانات",
    "أداة حظر الإعلانات",
    "يرجى تعطيل",
    "قم بالتسجيل",
    "ترجمة الويب من google",
    "لمواصلة الاستمتاع بالمحتوى",
    "إعلان به مشكلة",
    "يمكنك تعطيل الإعلانات",
    "دعم موقعنا",
    "لدعم موقعنا",
    "الاستمتاع بالمحتوى المجاني",
    "لمواصلة استخدام الترجمة",
]

# عناصر تُحذف قبل استخراج الفقرات (CSS selectors)
_REMOVE_SELECTORS = [
    "script", "style", "ins", "noscript", "button",
    ".ads", ".ad", "[class*='advert']", "[class*='sponsor']",
    ".navigation", ".chapter-nav", "[class*='nav']",
    ".comment", ".footer", ".btn",
    "[class*='paywall']", "[class*='login']", "[class*='register']",
    "[class*='adblock']", "[class*='popup']", "[role='dialog']",
    "[class*='unlock']", "[class*='subscribe']",
    ".modal", ".modal-backdrop", ".overlay", "[class*='blur']",
]

_CONTENT_SELECTORS = [
    ".chapter-content", ".serie-content", "#chapter-content",
    "[class*='chapter-content']", "[class*='content-text']",
    ".reader-content", "article .content",
]


def _is_cloudflare(html: str) -> bool:
    head = html[:3000].lower()
    return "just a moment" in head or "cloudflare" in head or "cf-browser-verification" in head


def _extract_paragraphs(soup: BeautifulSoup):
    """يستخرج الفقرات النظيفة من HTML — مشترك بين curl و browser."""
    for sel in _REMOVE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    container = None
    for sel in _CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            container = el
            break

    if container is None:
        # أكبر <div> نصياً كـ fallback
        best, best_len = None, 0
        for div in soup.find_all("div"):
            if div.find(["nav", "header", "footer", "script"]):
                continue
            t = div.get_text(strip=True)
            if len(t) > best_len:
                best, best_len = div, len(t)
        if best_len > 500:
            container = best

    if container is None:
        return [], "", ""

    chapter_title = ""
    for sel in [".chapter-title", "h1", ".serie-header h1", "[class*='chapter-title']"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            chapter_title = el.get_text(strip=True)
            break

    novel_title = ""
    for sel in [".novel-title", ".serie-title", "[class*='novel-name']"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            novel_title = el.get_text(strip=True)
            break

    paragraphs = []
    p_tags = container.find_all("p")
    if len(p_tags) > 2:
        for p in p_tags:
            t = p.get_text(strip=True)
            if len(t) > 10:
                paragraphs.append(t)
    else:
        for line in container.get_text("\n").split("\n"):
            t = line.strip()
            if len(t) > 10:
                paragraphs.append(t)

    clean = [p for p in paragraphs if not any(w.lower() in p.lower() for w in _PAYWALL_WORDS)]
    return clean, chapter_title, novel_title


# ═══════════════════════════════════════════════════════════════
# البحث عن الرواية
# ═══════════════════════════════════════════════════════════════
async def search_novel(novel_name: str) -> dict:
    search_url = f"https://wtr-lab.com/en/novel-finder?text={novel_name.replace(' ', '+')}"

    html = None
    try:
        html = await fetch_with_curl(search_url, timeout=15)
        if _is_cloudflare(html):
            html = None
    except Exception as e:
        logger.warning(f"[WTR/search] curl فشل: {e}")
        html = None

    if html is None:
        # fallback للمتصفح فقط عند فشل curl/Cloudflare
        async with get_browser_page(search_url, block_resources=False) as page:
            await human_delay(2000, 3000)
            await page.wait_for_load_state("domcontentloaded", timeout=20000)
            title = await page.title()
            if "just a moment" in title.lower() or "cloudflare" in title.lower():
                await asyncio.sleep(6)
            await human_scroll(page, times=2, delay=0.6)
            html = await page.content()

    soup = BeautifulSoup(html, "lxml")

    results = []
    seen = set()
    import re
    for a in soup.select('a[href*="/novel/"]'):
        href = a.get("href", "")
        m = re.search(r"/novel/(\d+)/([\w-]+)", href)
        if not m:
            continue
        nid, slug = m.group(1), m.group(2)
        if nid in seen:
            continue
        seen.add(nid)
        title_el = a.select_one("h2,h3,.title,.novel-title,.serie-title")
        title_tx = (title_el.get_text(strip=True) if title_el else "") or a.get_text(strip=True).split("\n")[0]
        if not title_tx:
            continue
        results.append({"id": nid, "slug": slug, "title": title_tx, "href": href})

    if not results:
        raise ValueError(f"لم أجد نتائج لـ '{novel_name}'")

    scored = sorted(results, key=lambda r: _similarity(novel_name, r["title"]), reverse=True)
    best = scored[0]
    logger.info(f"[WTR/search] best: {best['title']} id={best['id']} slug={best['slug']}")

    return {
        "id": best["id"],
        "slug": best["slug"],
        "title": best["title"] or novel_name,
        "novel_url": f"https://wtr-lab.com/en/novel/{best['id']}/{best['slug']}",
    }


# ═══════════════════════════════════════════════════════════════
# جلب الفصل
# ═══════════════════════════════════════════════════════════════
async def scrape_chapter(novel_id: str, novel_slug: str, chapter_num: int) -> dict:
    candidate_urls = [
        f"https://wtr-lab.com/en/novel/{novel_id}/{novel_slug}/chapter-{chapter_num}",
        f"https://wtr-lab.com/en/serie-en/{novel_id}-{novel_slug}/chapter-{chapter_num}",
        f"https://wtr-lab.com/en/serie/{novel_id}-{novel_slug}/chapter-{chapter_num}",
    ]
    last_error = None
    for url in candidate_urls:
        try:
            result = await _fetch_chapter_page(url, chapter_num)
            if result and result.get("paragraphs"):
                logger.info(f"[WTR/chapter] ✅ {url} ({result['paragraph_count']} فقرة)")
                return result
        except Exception as e:
            logger.warning(f"[WTR/chapter] ❌ {url}: {e}")
            last_error = e
    raise ValueError(f"فشل كشط الفصل {chapter_num}: {last_error}")


async def _fetch_chapter_page(url: str, chapter_num: int) -> dict:
    html = None

    # ─── المحاولة 1: curl_cffi (أسرع 10x، بدون متصفح) ─────────
    try:
        html = await fetch_with_curl(url, timeout=15)
        if _is_cloudflare(html):
            logger.info(f"[WTR/chapter] Cloudflare على curl — التبديل للمتصفح")
            html = None
        elif "404" in html[:2000] or "not found" in html[:2000].lower():
            raise ValueError("404")
    except ValueError:
        raise
    except Exception as e:
        logger.warning(f"[WTR/chapter] curl فشل: {e}")
        html = None

    if html is not None:
        soup = BeautifulSoup(html, "lxml")
        paragraphs, chapter_title, novel_title = _extract_paragraphs(soup)
        if len(paragraphs) >= 3:
            return {
                "title": novel_title or "رواية",
                "chapter_title": chapter_title or f"الفصل {chapter_num}",
                "paragraphs": paragraphs,
                "url": url,
                "paragraph_count": len(paragraphs),
            }
        logger.info(f"[WTR/chapter] curl أعطى {len(paragraphs)} فقرة فقط — التبديل للمتصفح")

    # ─── المحاولة 2: المتصفح (fallback فقط) ────────────────────
    async with get_browser_page(url, block_resources=False) as page:
        await human_delay(1500, 2500)
        await page.wait_for_load_state("domcontentloaded", timeout=25000)

        page_title = await page.title()
        if "404" in page_title or "not found" in page_title.lower():
            raise ValueError("404")
        if "just a moment" in page_title.lower() or "cloudflare" in page_title.lower():
            await asyncio.sleep(6)

        try:
            await page.wait_for_selector(
                ".chapter-content, .serie-content, #chapter-content, [class*='chapter-content']",
                timeout=10000
            )
        except Exception:
            pass

        await human_scroll(page, times=2, delay=0.6)
        await asyncio.sleep(0.8)

        page_html = await page.content()

    soup = BeautifulSoup(page_html, "lxml")
    paragraphs, chapter_title, novel_title = _extract_paragraphs(soup)

    if len(paragraphs) < 3:
        raise ValueError(f"محتوى فارغ أو محمي ({len(paragraphs)} فقرة صالحة)")

    return {
        "title": novel_title or "رواية",
        "chapter_title": chapter_title or f"الفصل {chapter_num}",
        "paragraphs": paragraphs,
        "url": url,
        "paragraph_count": len(paragraphs),
    }

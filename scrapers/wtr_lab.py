# scrapers/wtr_lab.py  v4 — curl_cffi فقط (بدون playwright)

import asyncio
import logging
from difflib import SequenceMatcher
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


_PAYWALL_WORDS = [
    "wtr-lab", "cloudflare", "just a moment", "enable javascript",
    "next chapter", "prev chapter", "table of contents",
    "report chapter", "read more at", "translator:", "editor:",
    "ai translation requires", "guests can preview",
    "register for free", "sign up to read", "login to continue",
    "ad blocker detected", "disable adblock", "please disable",
    "subscribe to read", "unlock chapter", "premium chapter",
    "disable your ad", "ad-blocker", "adblock",
    "تتطلب الترجمة بالذكاء", "يمكن للضيوف معاينة",
    "تم اكتشاف مانع", "مانع الإعلانات", "أداة حظر الإعلانات",
    "يرجى تعطيل", "قم بالتسجيل", "ترجمة الويب من google",
    "لمواصلة الاستمتاع بالمحتوى", "إعلان به مشكلة",
    "يمكنك تعطيل الإعلانات", "دعم موقعنا", "لدعم موقعنا",
    "الاستمتاع بالمحتوى المجاني", "لمواصلة استخدام الترجمة",
]

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


async def _fetch_curl(url: str, timeout: int = 20) -> str:
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome124") as session:
        r = await session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text


def _extract_paragraphs(soup: BeautifulSoup):
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


async def search_novel(novel_name: str) -> dict:
    search_url = f"https://wtr-lab.com/en/novel-finder?text={novel_name.replace(' ', '+')}"

    try:
        html = await _fetch_curl(search_url, timeout=15)
        if _is_cloudflare(html):
            raise ValueError("Cloudflare تحظر الطلب — لا يوجد متصفح كـ fallback")
    except Exception as e:
        raise ValueError(f"فشل جلب نتائج البحث: {e}")

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
    return {
        "id": best["id"],
        "slug": best["slug"],
        "title": best["title"] or novel_name,
        "novel_url": f"https://wtr-lab.com/en/novel/{best['id']}/{best['slug']}",
    }


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
                return result
        except Exception as e:
            logger.warning(f"[WTR/chapter] ❌ {url}: {e}")
            last_error = e
    raise ValueError(f"فشل كشط الفصل {chapter_num}: {last_error}")


async def _fetch_chapter_page(url: str, chapter_num: int) -> dict:
    try:
        html = await _fetch_curl(url, timeout=15)
    except Exception as e:
        raise ValueError(f"curl فشل: {e}")

    if _is_cloudflare(html):
        raise ValueError("Cloudflare تحظر الطلب")
    if "404" in html[:2000] or "not found" in html[:2000].lower():
        raise ValueError("404")

    soup = BeautifulSoup(html, "lxml")
    paragraphs, chapter_title, novel_title = _extract_paragraphs(soup)

    if len(paragraphs) < 3:
        raise ValueError(f"محتوى فارغ ({len(paragraphs)} فقرة)")

    return {
        "title": novel_title or "رواية",
        "chapter_title": chapter_title or f"الفصل {chapter_num}",
        "paragraphs": paragraphs,
        "url": url,
        "paragraph_count": len(paragraphs),
    }

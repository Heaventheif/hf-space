# scrapers/wtr_lab.py  v6 — curl only (webplus/web tabs)

import re
import json
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
    "or click here to read with google",
    "sign up for free to continue",
    "ai translation requires registration",
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
    ".webplus-content", ".web-plus-content", "[class*='webplus']",
    ".serie-content", ".chapter-content", "#chapter-content",
    "[class*='chapter-content']", "[class*='content-text']",
    ".reader-content", "article .content",
]

_AI_PAYWALL_MARKERS = [
    "ai translation requires registration",
    "ai translation requires",
    "guests can preview ai translation",
    "sign up for free to continue",
    "or click here to read with google",
]


def _is_cloudflare(html: str) -> bool:
    head = html[:3000].lower()
    return "just a moment" in head or "cloudflare" in head or "cf-browser-verification" in head


def _is_ai_paywall_block(html: str) -> bool:
    head = html.lower()
    return any(marker in head for marker in _AI_PAYWALL_MARKERS)


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


def _extract_next_data(html: str) -> dict | None:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None

    def find_key(obj, *keys):
        if isinstance(obj, dict):
            for k in keys:
                if k in obj:
                    return obj[k]
            for v in obj.values():
                r = find_key(v, *keys)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = find_key(item, *keys)
                if r is not None:
                    return r
        return None

    content_raw   = find_key(data, "content", "chapterContent", "body", "text", "webplus", "paragraphs")
    chapter_title = find_key(data, "chapterTitle", "chapter_title", "chapterName")
    novel_title   = find_key(data, "novelTitle", "novel_title", "novelName", "serieName")

    if not content_raw:
        return None

    if isinstance(content_raw, list):
        paragraphs = [str(p).strip() for p in content_raw if len(str(p).strip()) > 10]
    elif isinstance(content_raw, str):
        soup_inner = BeautifulSoup(content_raw, "lxml")
        for el in soup_inner.select("script,style,ins,.ads"):
            el.decompose()
        p_tags = soup_inner.find_all("p")
        if p_tags:
            paragraphs = [p.get_text(strip=True) for p in p_tags if len(p.get_text(strip=True)) > 10]
        else:
            paragraphs = [l.strip() for l in content_raw.split("\n") if len(l.strip()) > 10]
    else:
        return None

    paragraphs = [p for p in paragraphs if not any(w.lower() in p.lower() for w in _PAYWALL_WORDS)]
    if len(paragraphs) < 3:
        return None

    return {
        "paragraphs":    paragraphs,
        "chapter_title": str(chapter_title) if chapter_title else None,
        "novel_title":   str(novel_title)   if novel_title   else None,
    }


def _parse_html(html: str, chapter_num: int, url: str) -> dict | None:
    next_data = _extract_next_data(html)
    if next_data and len(next_data["paragraphs"]) >= 3:
        return {
            "title":           next_data["novel_title"]   or "Novel",
            "chapter_title":   next_data["chapter_title"] or f"Chapter {chapter_num}",
            "paragraphs":      next_data["paragraphs"],
            "url":             url,
            "paragraph_count": len(next_data["paragraphs"]),
        }

    soup = BeautifulSoup(html, "lxml")
    paragraphs, chapter_title, novel_title = _extract_paragraphs(soup)
    if len(paragraphs) >= 3:
        return {
            "title":           novel_title   or "Novel",
            "chapter_title":   chapter_title or f"Chapter {chapter_num}",
            "paragraphs":      paragraphs,
            "url":             url,
            "paragraph_count": len(paragraphs),
        }
    return None


async def search_novel(novel_name: str) -> dict:
    from urllib.parse import quote
    search_url = f"https://wtr-lab.com/en/novel-finder?text={quote(novel_name)}"

    try:
        html = await _fetch_curl(search_url, timeout=15)
        if _is_cloudflare(html):
            raise ValueError("Cloudflare تحظر الطلب")
    except Exception as e:
        raise ValueError(f"فشل جلب نتائج البحث: {e}")

    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
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
        "id":        best["id"],
        "slug":      best["slug"],
        "title":     best["title"] or novel_name,
        "novel_url": f"https://wtr-lab.com/en/novel/{best['id']}/{best['slug']}",
    }


async def scrape_chapter(novel_id: str, novel_slug: str, chapter_num: int) -> dict:
    base = f"https://wtr-lab.com/en/novel/{novel_id}/{novel_slug}/chapter-{chapter_num}"
    urls = [
        f"{base}?service=webplus",
        f"{base}?service=web",
    ]
    for url in urls:
        try:
            html = await _fetch_curl(url, timeout=20)
            if _is_cloudflare(html) or _is_ai_paywall_block(html):
                logger.warning(f"[WTR/curl] 🔒 {url}: paywall/cloudflare")
                continue
            result = _parse_html(html, chapter_num, url)
            if result:
                logger.info(f"[WTR/curl] ✅ {url} — {result['paragraph_count']} فقرة")
                return result
        except Exception as e:
            logger.warning(f"[WTR/curl] ❌ {url}: {e}")

    raise ValueError(f"wtr-lab: فشل كشط الفصل {chapter_num} — الموقع يحجب الطلبات")

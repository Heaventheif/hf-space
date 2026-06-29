"""
plugins/novel.py
endpoint: POST /novel
يجلب فصول الروايات من مواقع تحتاج JavaScript (Playwright فقط)
المواقع المستهدفة: NovelHi, WtrLab
(المواقع الـ static تعمل في novel.js على Render)
"""

import re
import time
import asyncio
import logging
from typing import Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

logger = logging.getLogger("novel")
DESCRIPTION = "جلب فصول الروايات من مواقع JavaScript"

# ─── Cache ─────────────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 3600

def _cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    if time.time() > item["expires"]:
        del _cache[key]
        return None
    return item["value"]

def _cache_set(key: str, value):
    if len(_cache) >= 300:
        oldest = next(iter(_cache))
        del _cache[oldest]
    _cache[key] = {"value": value, "expires": time.time() + CACHE_TTL}

# ─── User Agents ───────────────────────────────────────────────
import random
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
def rua(): return random.choice(UAS)

# ─── slugify ──────────────────────────────────────────────────
def slugify(name: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")))

def pascal_slug(name: str) -> str:
    """Martial Peak → Martial-Peak"""
    return "-".join(w.capitalize() for w in name.strip().split())

# ─── فلترة النص ───────────────────────────────────────────────
FILTER_WORDS = [
    "advertisement", "report chapter", "next chapter", "prev chapter",
    "table of contents", "access denied", "just a moment", "cloudflare",
    "enable javascript", "read more at",
]
STOLEN = [
    re.compile(r"stol(en|e)\s+(content|chapter)", re.I),
    re.compile(r"if\s+you.re\s+reading\s+this\s+on", re.I),
    re.compile(r"unauthorized\s+(use|reproduction)", re.I),
]

def is_filtered(t: str) -> bool:
    lo = t.lower()
    return any(w in lo for w in FILTER_WORDS) or any(p.search(t) for p in STOLEN)

def clean(t: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"\.{4,}", "...", t.replace("\u00a0", " "))).strip()

# ─── استخراج محتوى ─────────────────────────────────────────────
def extract(html: str, selectors: list[str]) -> Optional[list[str]]:
    soup = BeautifulSoup(html, "lxml")

    # إزالة عناصر التشويش أولاً
    for tag in soup.select("script,style,ins,.ads,noscript,nav,header,footer"):
        tag.decompose()

    container = None
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el and len(el.get_text()) > 200:
                container = el
                break
        except Exception:
            continue

    if container:
        paras = [clean(p.get_text()) for p in container.find_all("p")]
        paras = [p for p in paras if len(p) > 15 and not is_filtered(p)]
        if len(paras) < 3:
            raw = [clean(l) for l in container.get_text(separator="\n").split("\n")]
            paras = [p for p in raw if len(p) > 15 and not is_filtered(p)]
        if len(paras) >= 2:
            return paras

    # Fallback: أكبر div/article فيه نص (إذا فشلت كل الـ selectors)
    candidates = []
    for el in soup.select("div, article, section, main"):
        text = el.get_text()
        if len(text) > 500:
            candidates.append((len(text), el))
    candidates.sort(reverse=True)

    for _, el in candidates[:3]:
        paras = [clean(p.get_text()) for p in el.find_all("p")]
        paras = [p for p in paras if len(p) > 15 and not is_filtered(p)]
        if len(paras) >= 5:
            return paras

    return None

def extract_title(html: str, selectors: list[str]) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = re.split(r"[–\-|]", el.get_text())[0].strip()
            if len(t) > 2:
                return t
    return ""

# ─── Playwright ───────────────────────────────────────────────
async def fetch_js(url: str, wait_sel: str = None, timeout: int = 25000) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
            )
            try:
                page = await browser.new_page(user_agent=rua())
                await page.goto(url, wait_until="networkidle", timeout=timeout)
                if wait_sel:
                    try:
                        await page.wait_for_selector(wait_sel, timeout=10000)
                    except Exception:
                        pass  # قد يكون المحتوى موجوداً بدون انتظار selector معين
                html = await page.content()
                return html if len(html) > 500 else None
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"[Playwright] {url} → {e}")
        return None

# ─── تعريف المواقع (JS فقط) ────────────────────────────────────
# كل هذه المواقع تحتاج Playwright — axios لا يراها
SITES = [
    {
        "name": "NovelHi",
        "build_url": lambda name, ch: f"https://novelhi.com/s/{pascal_slug(name)}/{ch}",
        "wait_sel": ".ReadAjax_content",
        "selectors": [".ReadAjax_content", "#chaptercontent", ".Readarea", "#chapter-content"],
        "title_sel": ["h1", "title"],
    },
    {
        "name": "WtrLab",
        "needs_id": True,  # يحتاج resolve أولاً
        "search_url": lambda name: f"https://wtr-lab.com/en/novel-list?search={name.replace(' ', '+')}",
        "id_pattern": re.compile(r"/en/novel/(\d+)/"),
        "build_url": lambda novel_id, ch: f"https://wtr-lab.com/en/novel/{novel_id}/chapter-{ch}",
        "wait_sel": ".chapter-sentences, .chapter-content, div[class*='chapter']",
        "selectors": [
            ".chapter-sentences",      # WtrLab الحديث
            ".chapter-content",
            "div[class*='sentence']",
            "div[class*='chapter-text']",
            "div[class*='chapter']",
            "article .content",
            "main article",
        ],
        "title_sel": [".novel-title", "h1", "title"],
    },
]

# ─── WtrLab: resolve ID ────────────────────────────────────────
async def resolve_wtrlab_id(novel_name: str) -> Optional[str]:
    key = f"wtrlab:id:{novel_name.lower()}"
    cached = _cache_get(key)
    if cached:
        return cached

    search_url = f"https://wtr-lab.com/en/novel-list?search={novel_name.replace(' ', '+')}"
    html = await fetch_js(search_url, wait_sel="a[href*='/en/novel/']")
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(r"/en/novel/(\d+)/")

    # كلمات الاسم للمطابقة (تجاهل الكلمات القصيرة)
    name_words = [w.lower() for w in novel_name.split() if len(w) > 2]

    best_id = None
    best_score = 0

    for a in soup.select("a[href*='/en/novel/']"):
        href = a.get("href", "")
        m = pat.search(href)
        if not m:
            continue
        # نص الرابط + الـ href معاً للمطابقة
        link_text = (a.get_text() + " " + href).lower()
        score = sum(1 for w in name_words if w in link_text)
        if score > best_score:
            best_score = score
            best_id = m.group(1)
        if score == len(name_words):
            break

    # اقبل فقط إذا طابق نصف الكلمات على الأقل
    if best_id and best_score >= max(1, len(name_words) // 2):
        _cache_set(key, best_id)
        logger.info(f"[WtrLab] ID لـ '{novel_name}' = {best_id} (score={best_score}/{len(name_words)})")
        return best_id

    logger.warning(f"[WtrLab] لم يُعثر على تطابق لـ '{novel_name}' (أفضل score={best_score})")
    return None

# ─── جلب فصل من موقع ──────────────────────────────────────────
async def fetch_from_site(site: dict, novel_name: str, chapter_num: int) -> dict:
    key = f"{site['name']}:{novel_name.lower()}:{chapter_num}"
    cached = _cache_get(key)
    if cached:
        return cached

    if site.get("needs_id"):
        novel_id = await resolve_wtrlab_id(novel_name)
        if not novel_id:
            raise ValueError(f"WtrLab: لم يُعثر على ID الرواية '{novel_name}'")
        url = site["build_url"](novel_id, chapter_num)
    else:
        url = site["build_url"](novel_name, chapter_num)

    html = await fetch_js(url, site.get("wait_sel"))
    if not html:
        raise ValueError(f"{site['name']}: فشل جلب الصفحة")

    paragraphs = extract(html, site["selectors"])
    if not paragraphs:
        raise ValueError(f"{site['name']}: محتوى فارغ أو لم يُكتشف")

    title = extract_title(html, site["title_sel"]) or novel_name
    result = {
        "title": title,
        "chapter": chapter_num,
        "paragraphs": paragraphs,
        "site": site["name"],
        "url": url,
        "word_count": sum(len(p.split()) for p in paragraphs),
    }
    _cache_set(key, result)
    return result

# ─── register ─────────────────────────────────────────────────
def register(app):

    @app.post("/novel")
    async def get_chapter(request: Request):
        """
        Body: { "novel": "martial peak", "chapter": 1, "site": "NovelHi" (اختياري) }
        Response: {
            "title": "...", "chapter": 1,
            "paragraphs": [...], "site": "NovelHi",
            "url": "...", "word_count": 1200
        }
        """
        try:
            body = await request.json()
            novel_name = body.get("novel", "").strip()
            chapter_num = body.get("chapter")
            preferred_site = body.get("site", "").strip()

            if not novel_name:
                return JSONResponse({"error": "novel مطلوب"}, status_code=400)
            if not chapter_num or int(str(chapter_num)) < 1:
                return JSONResponse({"error": "chapter يجب أن يكون رقماً موجباً"}, status_code=400)

            chapter_num = int(chapter_num)

            # ترتيب المواقع: الأفضلية للموقع المطلوب إن وُجد
            sites = SITES[:]
            if preferred_site:
                sites = sorted(sites, key=lambda s: 0 if s["name"].lower() == preferred_site.lower() else 1)

            errors = []
            for site in sites:
                try:
                    result = await fetch_from_site(site, novel_name, chapter_num)
                    return JSONResponse(result)
                except Exception as e:
                    err = f"{site['name']}: {str(e)[:80]}"
                    errors.append(err)
                    logger.warning(f"[Novel] {err}")

            return JSONResponse({
                "error": "فشلت جميع المصادر",
                "details": errors,
            }, status_code=404)

        except Exception as e:
            logger.exception("خطأ غير متوقع في /novel")
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    @app.get("/novel/sites")
    async def list_sites():
        """قائمة المواقع المدعومة"""
        return {"sites": [s["name"] for s in SITES]}

    @app.delete("/novel/cache")
    async def clear_cache():
        count = len(_cache)
        _cache.clear()
        return {"cleared": count}

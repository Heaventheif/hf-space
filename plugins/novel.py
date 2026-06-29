"""
plugins/novel.py
endpoint: POST /novel
يجلب فصول الروايات من freewebnovel.com فقط
مع إعادة محاولة تلقائية، تنويع User-Agents، وآلية Fallback .html
"""

import re
import time
import asyncio
import logging
import httpx
from typing import Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

logger = logging.getLogger("novel")
DESCRIPTION = "جلب فصول الروايات من freewebnovel.com"

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

# ─── User Agents (متنوعة للإنتاج) ──────────────────────────────
import random
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
def rua(): return random.choice(UAS)

HEADERS = lambda: {
    "User-Agent": rua(),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ─── slugify ──────────────────────────────────────────────────
def slugify(name: str) -> str:
    """تحويل اسم الرواية إلى slug متوافق مع freewebnovel.com"""
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")))

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

    # إزالة عناصر التشويش
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

    # Fallback: أكبر div/article فيه نص
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

# ─── طلب HTTP مع إعادة المحاولة التلقائية ────────────────────
async def fetch_page(url: str, timeout: int = 30, retries: int = 2) -> Optional[str]:
    """جلب صفحة مع إعادة المحاولة (بحد أقصى 3 محاولات)"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(retries + 1):
            try:
                resp = await client.get(url, headers=HEADERS())
                if resp.status_code == 200 and len(resp.text) > 500:
                    # فحص سريع للحماية
                    lower = resp.text[:3000].lower()
                    if "just a moment" in lower or "cloudflare" in lower:
                        logger.warning(f"حماية Cloudflare على {url}")
                        break  # لا فائدة من إعادة المحاولة
                    return resp.text
            except httpx.TimeoutException:
                if attempt == retries:
                    logger.warning(f"Timeout بعد {retries} محاولات: {url}")
            except Exception as e:
                if attempt == retries:
                    logger.warning(f"فشل جلب {url} بعد {retries} محاولات: {e}")
            await asyncio.sleep(1)  # انتظر ثانية قبل إعادة المحاولة
        return None

# ─── الموقع الوحيد (مع selectors موسعة) ──────────────────────
SITE = {
    "name": "Freewebnovel",
    # المسار الصحيح بدون /novel/
    "build_url": lambda slug, ch: f"https://freewebnovel.com/{slug}/chapter-{ch}",
    # قائمة selectors موسعة
    "selectors": [
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".content",
        "#reading-content",
        ".reading-content",
        "#article",
        "div#article",
        "div[class*='chapter']",
        "article",
        "main"
    ],
    "title_sel": ["h1", "title"],
}

async def fetch_chapter(novel_name: str, chapter_num: int) -> dict:
    key = f"freewebnovel:{novel_name.lower()}:{chapter_num}"
    cached = _cache_get(key)
    if cached:
        return cached

    slug = slugify(novel_name)
    if not slug:
        raise ValueError("اسم الرواية غير صالح بعد التحويل إلى slug")

    # المحاولة الأولى: الرابط النظيف (بدون .html)
    url = SITE["build_url"](slug, chapter_num)
    html = await fetch_page(url)

    # المحاولة الثانية: إضافة .html (للمواقع القديمة في الموقع)
    if not html:
        logger.info(f"فشل الرابط النظيف، جرب .html: {url}.html")
        url = f"{SITE['build_url'](slug, chapter_num)}.html"
        html = await fetch_page(url)

    if not html:
        raise ValueError(f"فشل جلب الصفحة بكلا النمطين: {SITE['build_url'](slug, chapter_num)}")

    paragraphs = extract(html, SITE["selectors"])
    if not paragraphs:
        raise ValueError("لم يُعثر على محتوى (تحقق من selectors)")

    title = extract_title(html, SITE["title_sel"]) or novel_name

    result = {
        "title": title,
        "chapter": chapter_num,
        "paragraphs": paragraphs,
        "site": SITE["name"],
        "url": url,
        "word_count": sum(len(p.split()) for p in paragraphs),
    }
    _cache_set(key, result)
    return result

# ─── تسجيل الـ endpoints ──────────────────────────────────────
def register(app):

    @app.post("/novel")
    async def get_chapter(request: Request):
        try:
            body = await request.json()
            novel_name = body.get("novel", "").strip()
            chapter_num = body.get("chapter")

            if not novel_name:
                return JSONResponse({"error": "novel مطلوب"}, status_code=400)
            if not chapter_num or int(str(chapter_num)) < 1:
                return JSONResponse({"error": "chapter يجب أن يكون رقماً موجباً"}, status_code=400)
            chapter_num = int(chapter_num)

            result = await fetch_chapter(novel_name, chapter_num)
            return JSONResponse(result)

        except ValueError as e:
            logger.warning(f"خطأ في /novel: {e}")
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception as e:
            logger.exception("خطأ غير متوقع في /novel")
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    @app.get("/novel/sites")
    async def list_sites():
        return {"sites": [SITE["name"]]}

    @app.delete("/novel/cache")
    async def clear_cache():
        count = len(_cache)
        _cache.clear()
        return {"cleared": count}
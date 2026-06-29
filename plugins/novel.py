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
                        pass
                html = await page.content()
                return html if len(html) > 500 else None
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"[Playwright] {url} → {e}")
        return None

# ─── تعريف المواقع (JS فقط) ────────────────────────────────────
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
        "needs_id": True,
        "search_url": lambda name: f"https://wtr-lab.com/en/novel-list?search={name.replace(' ', '+')}",
        "id_pattern": re.compile(r"/en/novel/(\d+)/"),
        "build_url": lambda novel_id, ch: f"https://wtr-lab.com/en/novel/{novel_id}/chapter-{ch}",
        "wait_sel": ".chapter-sentences, .chapter-content, div[class*='chapter']",
        "selectors": [
            ".chapter-sentences",
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

    name_words = [w.lower() for w in novel_name.split() if len(w) > 2]

    best_id = None
    best_score = 0

    for a in soup.select("a[href*='/en/novel/']"):
        href = a.get("href", "")
        m = pat.search(href)
        if not m:
            continue
        link_text = (a.get_text() + " " + href).lower()
        score = sum(1 for w in name_words if w in link_text)
        if score > best_score:
            best_score = score
            best_id = m.group(1)
        if score == len(name_words):
            break

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


# ══════════════════════════════════════════════════════════════════
# ─── Probe / Discovery ────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════

# أنماط URL الشائعة في مواقع الروايات — يُجرَّب كلٌّ منها بالترتيب
# {slug}   = slugified novel name  (martial-peak)
# {pascal} = Pascal-slugified name (Martial-Peak)
# {raw}    = الاسم كما هو بعد استبدال المسافات بـ +
# {ch}     = رقم الفصل
URL_PATTERNS = [
    "{base}/{slug}/chapter-{ch}",
    "{base}/{slug}/chapter_{ch}",
    "{base}/{slug}/{ch}",
    "{base}/novel/{slug}/chapter-{ch}",
    "{base}/novel/{slug}/{ch}",
    "{base}/book/{slug}/chapter-{ch}",
    "{base}/read/{slug}/chapter-{ch}",
    "{base}/s/{pascal}/{ch}",
    "{base}/{slug}-chapter-{ch}",
    "{base}/chapters/{slug}/{ch}",
]

# selectors المرشحة للبحث عنها في أي موقع مجهول
CANDIDATE_SELECTORS = [
    # IDs شائعة
    "#chapter-content", "#chaptercontent", "#content", "#readContent",
    "#chapter-container", "#novel-content", "#reading-content",
    # classes شائعة
    ".chapter-content", ".chapter-text", ".chapter-body",
    ".reading-content", ".read-content", ".novel-content",
    ".content-text", ".text-content", ".ReadAjax_content",
    ".Readarea", ".chapter-sentences", ".entry-content",
    # عناصر دلالية
    "article.chapter", "article .content", "main article",
    "div[class*='chapter']", "div[class*='content']",
    "div[itemprop='articleBody']",
]

# selectors للعنوان
TITLE_CANDIDATES = [
    "h1.chapter-title", "h1.title", ".chapter-title", ".novel-title",
    "h1", "h2.chapter-name", "title",
]


def _probe_selectors(html: str) -> list[dict]:
    """
    يحلل HTML ويُعيد قائمة مرتبة من العناصر المرشحة لاحتواء نص الفصل.
    كل عنصر: { selector, paragraph_count, char_count, sample }
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select("script,style,ins,.ads,noscript,nav,header,footer"):
        tag.decompose()

    results = []
    seen_ids = set()  # تجنب تكرار نفس العنصر عبر selectors مختلفة

    for sel in CANDIDATE_SELECTORS:
        try:
            el = soup.select_one(sel)
            if not el:
                continue
            el_id = id(el)
            if el_id in seen_ids:
                continue
            seen_ids.add(el_id)

            paras = [clean(p.get_text()) for p in el.find_all("p")]
            paras = [p for p in paras if len(p) > 15 and not is_filtered(p)]

            # fallback: أسطر مباشرة
            if len(paras) < 3:
                lines = [clean(l) for l in el.get_text(separator="\n").split("\n")]
                paras = [l for l in lines if len(l) > 15 and not is_filtered(l)]

            char_count = sum(len(p) for p in paras)
            if char_count < 200:
                continue

            results.append({
                "selector": sel,
                "paragraph_count": len(paras),
                "char_count": char_count,
                "sample": paras[0][:120] if paras else "",
            })
        except Exception:
            continue

    # Fallback: أكبر div/article بالنص
    for el in soup.select("div, article, section, main"):
        el_id = id(el)
        if el_id in seen_ids:
            continue
        text = el.get_text()
        if len(text) < 500:
            continue
        paras = [clean(p.get_text()) for p in el.find_all("p")]
        paras = [p for p in paras if len(p) > 15 and not is_filtered(p)]
        if len(paras) < 5:
            continue
        seen_ids.add(el_id)

        # بناء selector تقريبي من الـ class/id
        classes = el.get("class", [])
        el_id_attr = el.get("id", "")
        if el_id_attr:
            approx_sel = f"{el.name}#{el_id_attr}"
        elif classes:
            approx_sel = f"{el.name}.{classes[0]}"
        else:
            approx_sel = el.name

        char_count = sum(len(p) for p in paras)
        results.append({
            "selector": approx_sel,
            "paragraph_count": len(paras),
            "char_count": char_count,
            "sample": paras[0][:120] if paras else "",
            "auto_detected": True,
        })

    # ترتيب: الأكثر نصاً أولاً
    results.sort(key=lambda x: x["char_count"], reverse=True)
    return results[:10]


def _build_probe_urls(base_url: str, novel_name: str, chapter_num: int) -> list[dict]:
    """
    يبني كل أنماط URL المحتملة للرواية/الفصل.
    يُعيد: [{ pattern, url }]
    """
    base = base_url.rstrip("/")
    sl = slugify(novel_name)
    pa = pascal_slug(novel_name)
    raw = novel_name.replace(" ", "+")

    urls = []
    for pat in URL_PATTERNS:
        try:
            url = pat.format(base=base, slug=sl, pascal=pa, raw=raw, ch=chapter_num)
            urls.append({"pattern": pat, "url": url})
        except KeyError:
            continue
    return urls


async def probe_site(
    base_url: str,
    novel_name: str,
    chapter_num: int,
    try_all: bool = False,
) -> dict:
    """
    يستكشف موقعاً مجهولاً:
    1. يجرب أنماط URL حتى يجد صفحة تحتوي محتوى
    2. يحلل الـ HTML ويكشف أفضل selectors
    3. يُعيد تقريراً كاملاً + "وصفة" جاهزة للاستخدام في SITES

    try_all=True → يجرب كل الأنماط حتى لو وجد واحداً ناجحاً (للمقارنة)
    """
    cache_key = f"probe:{base_url}:{novel_name.lower()}:{chapter_num}"
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "from_cache": True}

    url_candidates = _build_probe_urls(base_url, novel_name, chapter_num)
    attempts = []
    best_result = None

    for candidate in url_candidates:
        url = candidate["url"]
        logger.info(f"[Probe] جرب → {url}")
        html = await fetch_js(url, timeout=20000)

        attempt = {
            "pattern": candidate["pattern"],
            "url": url,
            "ok": False,
            "http_size": len(html) if html else 0,
        }

        if not html:
            attempt["reason"] = "فشل الجلب أو صفحة فارغة"
            attempts.append(attempt)
            if not try_all:
                continue
        else:
            sel_results = _probe_selectors(html)
            title = extract_title(html, TITLE_CANDIDATES)

            if sel_results and sel_results[0]["paragraph_count"] >= 3:
                attempt["ok"] = True
                attempt["title"] = title
                attempt["selectors"] = sel_results
                attempts.append(attempt)

                if best_result is None:
                    best_result = attempt

                if not try_all:
                    break
            else:
                attempt["reason"] = "لم يُعثر على محتوى كافٍ"
                attempt["selectors_found"] = sel_results
                attempts.append(attempt)

    # ─── بناء "الوصفة" الجاهزة ───────────────────────────────
    recipe = None
    if best_result:
        top_sel = best_result["selectors"][0]["selector"]
        pattern_used = best_result["pattern"]
        recipe = {
            "name": novel_name,
            "base_url": base_url,
            "url_pattern": pattern_used,
            "example_url": best_result["url"],
            "build_url_template": pattern_used
                .replace("{base}", base_url.rstrip("/"))
                .replace("{slug}", "{novel_slug}")
                .replace("{pascal}", "{novel_pascal}")
                .replace("{ch}", "{chapter}"),
            "best_selector": top_sel,
            "all_selectors": [s["selector"] for s in best_result["selectors"]],
            "title_selectors": TITLE_CANDIDATES[:4],
            "note": (
                "أضف هذه الوصفة إلى SITES في novel.py — "
                "build_url: lambda name, ch: pattern.format(slug=slugify(name), ch=ch)"
            ),
        }

    report = {
        "base_url": base_url,
        "novel": novel_name,
        "chapter": chapter_num,
        "patterns_tried": len(attempts),
        "success": best_result is not None,
        "best_url": best_result["url"] if best_result else None,
        "best_pattern": best_result["pattern"] if best_result else None,
        "best_selectors": best_result["selectors"] if best_result else [],
        "recipe": recipe,
        "attempts": attempts,
    }

    if best_result:
        _cache_set(cache_key, report)

    return report


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

    # ──────────────────────────────────────────────────────────────
    @app.post("/novel/probe")
    async def probe_novel_site(request: Request):
        """
        يستكشف موقع رواية مجهول ويكشف:
        - أي نمط URL يعمل للوصول لفصل معين
        - أي CSS selectors تحتوي على نص الفصل
        - "وصفة" جاهزة للإضافة إلى SITES

        Body:
        {
            "base_url": "https://example-novel-site.com",   ← مطلوب
            "novel":    "martial peak",                      ← مطلوب
            "chapter":  1,                                   ← اختياري (افتراضي: 1)
            "try_all":  false                                ← اختياري: جرب كل الأنماط
        }

        Response:
        {
            "success": true,
            "best_url": "https://...",
            "best_pattern": "{base}/{slug}/chapter-{ch}",
            "best_selectors": [
                { "selector": ".chapter-content", "paragraph_count": 42, "char_count": 8500, "sample": "..." },
                ...
            ],
            "recipe": {
                "name": "...",
                "base_url": "...",
                "url_pattern": "...",
                "example_url": "...",
                "build_url_template": "https://site.com/{novel_slug}/chapter-{chapter}",
                "best_selector": ".chapter-content",
                "all_selectors": [...],
                "title_selectors": [...],
                "note": "أضف هذه الوصفة إلى SITES في novel.py"
            },
            "patterns_tried": 10,
            "attempts": [
                { "pattern": "...", "url": "...", "ok": true/false, "reason": "...", "selectors": [...] },
                ...
            ],
            "from_cache": false
        }
        """
        try:
            body = await request.json()
            base_url = body.get("base_url", "").strip().rstrip("/")
            novel_name = body.get("novel", "").strip()
            chapter_num = int(body.get("chapter", 1))
            try_all = bool(body.get("try_all", False))

            if not base_url:
                return JSONResponse({"error": "base_url مطلوب (مثال: https://novelsite.com)"}, status_code=400)
            if not novel_name:
                return JSONResponse({"error": "novel مطلوب"}, status_code=400)
            if chapter_num < 1:
                return JSONResponse({"error": "chapter يجب أن يكون رقماً موجباً"}, status_code=400)

            # تحقق بسيط من الـ URL
            if not base_url.startswith("http"):
                base_url = "https://" + base_url

            report = await probe_site(base_url, novel_name, chapter_num, try_all=try_all)
            status = 200 if report["success"] else 404
            return JSONResponse(report, status_code=status)

        except Exception as e:
            logger.exception("خطأ في /novel/probe")
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    # ──────────────────────────────────────────────────────────────
    @app.get("/novel/probe/patterns")
    async def list_url_patterns():
        """
        يُعيد كل أنماط URL التي يجربها probe مع شرح المتغيرات.
        مفيد لفهم ما سيتم اختباره قبل استدعاء probe.
        """
        return {
            "variables": {
                "{base}":   "رابط الموقع الأساسي (مثال: https://novelsite.com)",
                "{slug}":   "اسم الرواية مُحوَّل لـ slug (مثال: martial-peak)",
                "{pascal}": "اسم الرواية بـ Pascal-slug (مثال: Martial-Peak)",
                "{raw}":    "الاسم كما هو مع + بدل المسافات (مثال: martial+peak)",
                "{ch}":     "رقم الفصل (مثال: 1)",
            },
            "patterns": URL_PATTERNS,
            "total": len(URL_PATTERNS),
        }

    # ──────────────────────────────────────────────────────────────
    @app.get("/novel/sites")
    async def list_sites():
        """قائمة المواقع المدعومة"""
        return {"sites": [s["name"] for s in SITES]}

    @app.delete("/novel/cache")
    async def clear_cache():
        count = len(_cache)
        _cache.clear()
        return {"cleared": count}

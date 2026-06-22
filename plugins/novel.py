"""
plugins/novel.py — v5
روايات: wtr-lab → fanmtl → novelbin → lightnovelworld → lnmtl
"""
import time
import traceback
import logging
from urllib.parse import quote
from bs4 import BeautifulSoup
from fastapi import Query
from fastapi.responses import JSONResponse
from scrapers.wtr_lab import search_novel, scrape_chapter

logger = logging.getLogger(__name__)

DESCRIPTION     = "روايات: wtr-lab + fanmtl + novelbin + lnw + lnmtl"
DOCKERFILE_DEPS = []


async def _fetch_curl(url: str, timeout: int = 20) -> str:
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome124") as session:
        r = await session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text


# ═══════════════════════════════════════════════════
# FanMTL — HTML مباشر بدون JS ✅
# رابط الفصل: /novel/<slug>_<N>.html
# ═══════════════════════════════════════════════════

async def _fanmtl_search(query: str) -> dict | None:
    try:
        url  = f"https://www.fanmtl.com/search.html?keyword={quote(query)}"
        html = await _fetch_curl(url, timeout=15)
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select('a[href*="/novel/"]'):
            href = a.get("href", "")
            if "_" in href.split("/novel/")[-1]:  # روابط فصول — تجنب
                continue
            slug = href.rstrip("/").replace(".html", "").split("/novel/")[-1]
            if not slug or slug == "novel":
                continue
            title = a.get("title") or a.get_text(strip=True).split("\n")[0]
            if title and len(title) > 2:
                return {"title": title, "slug": slug}
        return None
    except Exception as e:
        logger.warning(f"[fanmtl/search] {e}")
        return None


async def _fanmtl_chapter(slug: str, chapter: int) -> dict | None:
    try:
        url  = f"https://www.fanmtl.com/novel/{slug}_{chapter}.html"
        html = await _fetch_curl(url, timeout=20)
        if "404" in html[:500]:
            return None
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("script,style,ins,.ads,nav,header,footer,.chapter-nav,.navigation"):
            el.decompose()
        content = soup.select_one(
            "#chapter-content, .chapter-content, .read-content, "
            ".content, article, .novel-content, main"
        )
        if not content:
            best, best_len = None, 0
            for div in soup.find_all("div"):
                t = div.get_text(strip=True)
                if len(t) > best_len and not div.find(["nav", "header", "footer"]):
                    best, best_len = div, len(t)
            if best_len > 300:
                content = best

        if not content:
            return None

        paras = [p.get_text(" ", strip=True) for p in content.find_all("p") if len(p.get_text(strip=True)) > 20]
        if len(paras) < 3:
            paras = [l.strip() for l in content.get_text("\n").split("\n") if len(l.strip()) > 20]
        if len(paras) < 3:
            return None

        title_el = soup.select_one("h1, h2, .chapter-title")
        return {
            "title":           "Novel",
            "chapter_title":   title_el.get_text(strip=True) if title_el else f"Chapter {chapter}",
            "paragraphs":      paras,
            "url":             url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[fanmtl/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# LNMTL
# ═══════════════════════════════════════════════════

async def _lnmtl_chapter(slug: str, chapter: int) -> dict | None:
    try:
        url  = f"https://lnmtl.com/chapter/{slug}/chapter-{chapter}"
        html = await _fetch_curl(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("script,style,ins,.ads,.navigation"):
            el.decompose()
        content = soup.select_one(".chapter-body, .translated, #chapter-content")
        if not content:
            return None
        paras = [p.get_text(" ", strip=True) for p in content.find_all("p") if len(p.get_text(strip=True)) > 20]
        if len(paras) < 3:
            return None
        title_el = soup.select_one("h1, .chapter-title")
        return {
            "title":           "Novel",
            "chapter_title":   title_el.get_text(strip=True) if title_el else f"Chapter {chapter}",
            "paragraphs":      paras,
            "url":             url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[lnmtl/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# Novelbin
# ═══════════════════════════════════════════════════

async def _novelbin_search(query: str) -> dict | None:
    try:
        html = await _fetch_curl(f"https://novelbin.com/search?keyword={quote(query)}", timeout=15)
        soup = BeautifulSoup(html, "lxml")
        item = soup.select_one(".list-novel .row-item, .novel-item")
        if not item:
            return None
        a = item.select_one("a[href*='/novel/']")
        if not a:
            return None
        slug = a["href"].split("/novel/")[-1].rstrip("/")
        return {"title": a.get_text(strip=True), "slug": slug}
    except Exception as e:
        logger.warning(f"[novelbin/search] {e}")
        return None


async def _novelbin_chapter(slug: str, chapter: int) -> dict | None:
    try:
        url  = f"https://novelbin.com/b/{slug}/chapter-{chapter}"
        html = await _fetch_curl(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("script,style,ins,.ads,#ads,.chapter-nav,.navigation"):
            el.decompose()
        content = soup.select_one("#chr-content, #chapter-content, .chr-content, .chapter__content")
        if not content:
            return None
        paras = [p.get_text(" ", strip=True) for p in content.find_all("p") if len(p.get_text(strip=True)) > 20]
        if len(paras) < 3:
            return None
        title_el = soup.select_one(".chr-title, .chapter-title, h2, h1")
        return {
            "title":           "Novel",
            "chapter_title":   title_el.get_text(strip=True) if title_el else f"Chapter {chapter}",
            "paragraphs":      paras,
            "url":             url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[novelbin/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# LightNovelWorld
# ═══════════════════════════════════════════════════

async def _lnw_search(query: str) -> dict | None:
    try:
        html = await _fetch_curl(f"https://www.lightnovelworld.co/search?title={quote(query)}", timeout=15)
        soup = BeautifulSoup(html, "lxml")
        item = soup.select_one(".novel-item a[href*='/novel/']")
        if not item:
            return None
        slug = item["href"].split("/novel/")[-1].rstrip("/")
        return {"title": item.get_text(strip=True), "slug": slug}
    except Exception as e:
        logger.warning(f"[lnw/search] {e}")
        return None


async def _lnw_chapter(slug: str, chapter: int) -> dict | None:
    try:
        url  = f"https://www.lightnovelworld.co/novel/{slug}/chapter-{chapter}"
        html = await _fetch_curl(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("script,style,ins,.ads,.chapter-nav"):
            el.decompose()
        content = soup.select_one("#chapter-container, .chapter-content, .text-left")
        if not content:
            return None
        paras = [p.get_text(" ", strip=True) for p in content.find_all("p") if len(p.get_text(strip=True)) > 20]
        if len(paras) < 3:
            return None
        title_el = soup.select_one(".chapter-title, h2")
        return {
            "title":           "Novel",
            "chapter_title":   title_el.get_text(strip=True) if title_el else f"Chapter {chapter}",
            "paragraphs":      paras,
            "url":             url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[lnw/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# دالة مساعدة: جلب slug lnmtl من اسم الرواية
# ═══════════════════════════════════════════════════

async def _lnmtl_search(query: str) -> dict | None:
    try:
        url  = f"https://lnmtl.com/novel?filter%5Bq%5D={quote(query)}"
        html = await _fetch_curl(url, timeout=15)
        soup = BeautifulSoup(html, "lxml")
        a = soup.select_one('.novel-item a[href*="/novel/"], .thumbnail a[href*="/novel/"]')
        if not a:
            return None
        slug = a["href"].rstrip("/").split("/novel/")[-1]
        return {"title": a.get_text(strip=True), "slug": slug}
    except Exception as e:
        logger.warning(f"[lnmtl/search] {e}")
        return None


# ═══════════════════════════════════════════════════
# الجلب الموحد: wtr-lab → fanmtl → novelbin → lnw → lnmtl
# ═══════════════════════════════════════════════════

async def fetch_with_fallback(name: str, chapter: int) -> dict:
    errors = []

    # 1. wtr-lab
    try:
        info = await search_novel(name)
        ch   = await scrape_chapter(info["id"], info["slug"], chapter)
        return {
            "source": "wtr-lab",
            "novel":  {"title": info["title"], "url": info["novel_url"]},
            "chapter": ch,
        }
    except Exception as e:
        errors.append(f"wtr-lab: {e}")
        logger.warning(f"[novel/fallback] wtr-lab فشل: {e}")

    # 2. fanmtl — HTML مباشر، موثوق
    try:
        fm = await _fanmtl_search(name)
        if fm:
            ch = await _fanmtl_chapter(fm["slug"], chapter)
            if ch:
                return {
                    "source": "fanmtl",
                    "novel":  {"title": fm["title"], "url": f"https://www.fanmtl.com/novel/{fm['slug']}.html"},
                    "chapter": ch,
                }
        errors.append("fanmtl: لم يُجد نتائج أو فصلاً")
    except Exception as e:
        errors.append(f"fanmtl: {e}")

    # 3. novelbin
    try:
        nb = await _novelbin_search(name)
        if nb:
            ch = await _novelbin_chapter(nb["slug"], chapter)
            if ch:
                return {
                    "source": "novelbin",
                    "novel":  {"title": nb["title"], "url": f"https://novelbin.com/b/{nb['slug']}"},
                    "chapter": ch,
                }
        errors.append("novelbin: لم يُجد نتائج أو فصلاً")
    except Exception as e:
        errors.append(f"novelbin: {e}")

    # 4. lightnovelworld
    try:
        lnw = await _lnw_search(name)
        if lnw:
            ch = await _lnw_chapter(lnw["slug"], chapter)
            if ch:
                return {
                    "source": "lightnovelworld",
                    "novel":  {"title": lnw["title"], "url": f"https://www.lightnovelworld.co/novel/{lnw['slug']}"},
                    "chapter": ch,
                }
        errors.append("lightnovelworld: لم يُجد نتائج أو فصلاً")
    except Exception as e:
        errors.append(f"lightnovelworld: {e}")

    # 5. lnmtl
    try:
        lm = await _lnmtl_search(name)
        if lm:
            ch = await _lnmtl_chapter(lm["slug"], chapter)
            if ch:
                return {
                    "source": "lnmtl",
                    "novel":  {"title": lm["title"], "url": f"https://lnmtl.com/novel/{lm['slug']}"},
                    "chapter": ch,
                }
        errors.append("lnmtl: لم يُجد نتائج أو فصلاً")
    except Exception as e:
        errors.append(f"lnmtl: {e}")

    raise ValueError("كل المصادر فشلت:\n" + "\n".join(errors))


# ═══════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════

def register(app):

    @app.get("/novel/search")
    async def novel_search(q: str = Query(...)):
        try:
            return JSONResponse({"success": True, "data": await search_novel(q)})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.get("/novel/chapter")
    async def novel_chapter(id: str = Query(...), slug: str = Query(...), chapter: int = Query(...)):
        start = time.time()
        try:
            result = await scrape_chapter(id, slug, chapter)
            return JSONResponse({"success": True, "elapsed_seconds": round(time.time()-start, 2), "data": result})
        except Exception as e:
            return JSONResponse({"success": False, "elapsed_seconds": round(time.time()-start, 2), "error": str(e)}, status_code=500)

    @app.get("/novel/fetch")
    async def novel_fetch(name: str = Query(...), chapter: int = Query(...)):
        start = time.time()
        try:
            print(f"[NOVEL/fetch] 🔍 {name} فصل {chapter}")
            result = await fetch_with_fallback(name, chapter)
            ch = result["chapter"]
            print(f"[NOVEL/fetch] ✅ {result['source']} — {ch['paragraph_count']} فقرة")
            return JSONResponse({
                "success":         True,
                "source":          result["source"],
                "elapsed_seconds": round(time.time()-start, 2),
                "novel":           result["novel"],
                "chapter": {
                    "number":          chapter,
                    "title":           ch.get("chapter_title", f"Chapter {chapter}"),
                    "paragraphs":      ch["paragraphs"],
                    "paragraph_count": ch["paragraph_count"],
                    "url":             ch.get("url", ""),
                },
            })
        except Exception as e:
            err = traceback.format_exc()
            print(f"[NOVEL/fetch] ❌\n{err}")
            return JSONResponse({
                "success": False,
                "elapsed_seconds": round(time.time()-start, 2),
                "error": str(e),
            }, status_code=500)

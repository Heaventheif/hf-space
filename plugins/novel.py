"""
plugins/novel.py — v3
روايات: wtr-lab + novelbin + lightnovelworld كـ fallback
"""
import time
import asyncio
import traceback
import logging
from urllib.parse import quote
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from fastapi import Query
from fastapi.responses import JSONResponse
from scrapers.wtr_lab import search_novel, scrape_chapter

logger = logging.getLogger(__name__)

DESCRIPTION     = "روايات: wtr-lab + novelbin + lightnovelworld"
DOCKERFILE_DEPS = []


async def _fetch_curl(url: str, timeout: int = 20) -> str:
    """جلب الصفحة عبر curl_cffi مع تقليد متصفح حقيقي — يتجاوز حظر WAF/Cloudflare
    الذي كان يرفض طلبات httpx العادية بـ 403."""
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome124") as session:
        r = await session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text


# ═══════════════════════════════════════════════════
# Novelbin Scraper (fallback 1)
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
        title = a.get_text(strip=True)
        slug  = a["href"].split("/novel/")[-1].rstrip("/")
        return {"title": title, "slug": slug, "source": "novelbin"}
    except Exception as e:
        logger.warning(f"[novelbin/search] {e}")
        return None


async def _novelbin_chapter(slug: str, chapter: int) -> dict | None:
    try:
        url  = f"https://novelbin.com/b/{slug}/chapter-{chapter}"
        html = await _fetch_curl(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")
        # إزالة العناصر غير المرغوبة
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
            "title": "رواية",
            "chapter_title": title_el.get_text(strip=True) if title_el else f"الفصل {chapter}",
            "paragraphs": paras,
            "url": url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[novelbin/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# LightNovelWorld Scraper (fallback 2)
# ═══════════════════════════════════════════════════

async def _lnw_search(query: str) -> dict | None:
    try:
        html = await _fetch_curl(f"https://www.lightnovelworld.co/search?title={quote(query)}", timeout=15)
        soup = BeautifulSoup(html, "lxml")
        item = soup.select_one(".novel-item a[href*='/novel/']")
        if not item:
            return None
        slug = item["href"].split("/novel/")[-1].rstrip("/")
        return {"title": item.get_text(strip=True), "slug": slug, "source": "lnw"}
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
            "title": "رواية",
            "chapter_title": title_el.get_text(strip=True) if title_el else f"الفصل {chapter}",
            "paragraphs": paras,
            "url": url,
            "paragraph_count": len(paras),
        }
    except Exception as e:
        logger.warning(f"[lnw/chapter] {e}")
        return None


# ═══════════════════════════════════════════════════
# الجلب الموحد مع fallbacks
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

    # 2. novelbin
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

    # 3. lightnovelworld
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

    raise ValueError(f"كل المصادر فشلت:\n" + "\n".join(errors))


# ═══════════════════════════════════════════════════
# Register Routes
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
            return JSONResponse({"success": True, "elapsed_seconds": round(time.time()-start,2), "data": result})
        except Exception as e:
            return JSONResponse({"success": False, "elapsed_seconds": round(time.time()-start,2), "error": str(e)}, status_code=500)

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
                    "title":           ch.get("chapter_title", f"الفصل {chapter}"),
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

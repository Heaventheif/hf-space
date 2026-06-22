
# scrapers/wtr_lab.py  v5 — webplus + __NEXT_DATA__ extraction

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
    "تتطلب الترجمة بالذكاء", "يمكن للضيوف معاينة",
    "تم اكتشاف مانع", "مانع الإعلانات", "أداة حظر الإعلانات",
    "يرجى تعطيل", "قم بالتسجيل", "ترجمة الويب من google",
    "لمواصلة الاستمتاع بالمحتوى", "إعلان به مشكلة",
    "يمكنك تعطيل الإعلانات", "دعم موقعنا", "لدعم موقعنا",
    "الاستمتاع بالمحتوى المجاني", "لمواصلة استخدام الترجمة",
    # Web+ specific
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

# Web+ selectors أولاً ثم العامة
_CONTENT_SELECTORS = [
    ".webplus-content", ".web-plus-content", "[class*='webplus']",
    ".serie-content", ".chapter-content", "#chapter-content",
    "[class*='chapter-content']", "[class*='content-text']",
    ".reader-content", "article .content",
]


def _is_cloudflare(html: str) -> bool:
    head = html[:3000].lower()
    return "just a moment" in head or "cloudflare" in head or "cf-browser-verification" in head


_AI_PAYWALL_MARKERS = [
    "ai translation requires registration",
    "ai translation requires",
    "guests can preview ai translation",
    "sign up for free to continue",
    "or click here to read with google",
    "تتطلب الترجمة بالذكاء الاصطناعي",
    "يمكن للضيوف معاينة",
]


def _is_ai_paywall_block(html: str) -> bool:
    """يكتشف تحديداً حالة كون المحتوى المُستخرج هو كتلة "AI Translation Requires
    Registration" (تبويب AI الافتراضي المحظور للزوار) بدل محتوى الفصل الحقيقي.
    هذا يختلف عن Cloudflare/404: الصفحة تُحمَّل بنجاح لكنها تعرض تبويب AI
    بدل Web+ المطلوب، رغم تمرير service=webplus في الرابط."""
    head = html.lower()
    return any(marker in head for marker in _AI_PAYWALL_MARKERS)


class _AiPaywallError(Exception):
    """يُرفع عندما تكون الصفحة المُجلَبة هي كتلة AI المحظورة بدل المحتوى الحقيقي،
    لتمييزها عن أخطاء الشبكة/404/Cloudflare العادية في scrape_chapter."""
    pass


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
    """استخراج المحتوى من __NEXT_DATA__ JSON — لا يحتاج JS rendering"""
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

    content_raw   = find_key(data, "content", "chapterContent", "body", "text", "webplus")
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


# ─── جلب slug الفصل من MtlNovel ──────────────────────────────
async def getMtlNovelChapterUrl(novelSlug, chapterNum):
    pass  # kept for compat


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
    # ملاحظة مهمة (مؤكدة من فحص يدوي للموقع):
    #   /chapter-N                  → تبويب AI (الافتراضي — يطلب تسجيل دخول للزوار)
    #   /chapter-N?service=webplus  → تبويب Web+  (يعمل بدون تسجيل)
    #   /chapter-N?service=web      → تبويب Web   (يعمل بدون تسجيل)
    # لذلك الرابط بدون أي service= هو فعلياً تبويب AI المحظور، وليس fallback آمناً.
    # الترتيب أدناه يُجرّب webplus ثم web أولاً (كلاهما يعملان فعلياً)، ولا يلجأ
    # للرابط العادي (AI) أو لمخطط serie-en القديم إلا كملاذ أخير عند فشل الاثنين
    # الأوليين لسبب آخر (شبكة/404/Cloudflare) — وليس بسبب قفل AI.
    candidate_urls = [
        f"https://wtr-lab.com/en/novel/{novel_id}/{novel_slug}/chapter-{chapter_num}?service=webplus",
        f"https://wtr-lab.com/en/novel/{novel_id}/{novel_slug}/chapter-{chapter_num}?service=web",
        f"https://wtr-lab.com/en/serie-en/{novel_id}-{novel_slug}/chapter-{chapter_num}?service=webplus",
        f"https://wtr-lab.com/en/serie-en/{novel_id}-{novel_slug}/chapter-{chapter_num}?service=web",
        # الروابط بدون service= (AI) تُبقى كآخر ملاذ فقط — أي نجاح هنا يجب أن يُراجع
        # لاحتمال كونه محتوى AI وليس Web+/Web الحقيقي.
        f"https://wtr-lab.com/en/novel/{novel_id}/{novel_slug}/chapter-{chapter_num}",
        f"https://wtr-lab.com/en/serie-en/{novel_id}-{novel_slug}/chapter-{chapter_num}",
    ]
    last_error = None
    ai_block_seen = False
    for url in candidate_urls:
        try:
            result = await _fetch_chapter_page(url, chapter_num)
            if result and result.get("paragraphs"):
                return result
        except _AiPaywallError as e:
            ai_block_seen = True
            logger.warning(f"[WTR/chapter] 🔒 {url}: تبويب AI محظور — تجربة رابط/تبويب آخر")
            last_error = e
        except Exception as e:
            logger.warning(f"[WTR/chapter] ❌ {url}: {e}")
            last_error = e

    if ai_block_seen:
        raise ValueError(
            f"فشل كشط الفصل {chapter_num}: كل المحاولات أعادت تبويب AI المحظور "
            f"للزوار (يتطلب تسجيل دخول) — {last_error}"
        )
    raise ValueError(f"فشل كشط الفصل {chapter_num}: {last_error}")


async def _fetch_chapter_page(url: str, chapter_num: int) -> dict:
    # رابط بدون أي service= هو تبويب AI (مؤكد بالفحص اليدوي) — أي محتوى يُستخرج
    # منه غير موثوق به كنص رواية حقيقي حتى لو تجاوز فلاتر القفل النصية، لأن AI قد
    # يعيد ترجمته الخاصة (وليس Web/Web+) أو نصاً جزئياً بسبب حد المعاينة للزوار.
    is_ai_tab = "service=" not in url

    try:
        html = await _fetch_curl(url, timeout=20)
    except Exception as e:
        raise ValueError(f"curl فشل: {e}")

    if _is_cloudflare(html):
        raise ValueError("Cloudflare تحظر الطلب")
    if "404" in html[:2000] or "not found" in html[:2000].lower():
        raise ValueError("404")
    if _is_ai_paywall_block(html):
        # الصفحة حُمِّلت بنجاح، لكنها تعرض تبويب AI المحظور للزوار غير المسجلين
        # (راجع: "AI Translation Requires Registration") بدل تبويب Web/Web+
        # المطلوب. هذا ليس فشل شبكة أو حظر Cloudflare — لذلك يُرفع استثناء
        # مخصص يسمح لـ scrape_chapter بتمييزه وتجربة رابط/تبويب آخر مباشرة.
        raise _AiPaywallError("الصفحة أعادت تبويب AI المحظور (يتطلب تسجيل دخول) بدل المحتوى المطلوب")
    if is_ai_tab:
        # احتياط إضافي: حتى لو لم يُكتشف نص القفل صريحاً (مثلاً ضمن أول 10 فصول
        # المسموح للزوار معاينتها)، الرابط نفسه يستهدف تبويب AI لا Web+/Web —
        # لذلك لا نعتمد عليه كمصدر أساسي ونرفع نفس الاستثناء لتفضيل بقية الروابط.
        raise _AiPaywallError("الرابط يستهدف تبويب AI (بدون service=) — يُتجاهل لصالح webplus/web")

    # ── محاولة 1: __NEXT_DATA__ JSON (لا يحتاج JS) ─────────────
    next_data = _extract_next_data(html)
    if next_data and len(next_data["paragraphs"]) >= 3:
        logger.info(f"[WTR/chapter] ✅ __NEXT_DATA__ — {len(next_data['paragraphs'])} فقرة — {url}")
        return {
            "title":           next_data["novel_title"]   or "رواية",
            "chapter_title":   next_data["chapter_title"] or f"الفصل {chapter_num}",
            "paragraphs":      next_data["paragraphs"],
            "url":             url,
            "paragraph_count": len(next_data["paragraphs"]),
        }

    # ── محاولة 2: HTML parsing مباشر ───────────────────────────
    soup = BeautifulSoup(html, "lxml")
    paragraphs, chapter_title, novel_title = _extract_paragraphs(soup)

    if len(paragraphs) < 3:
        raise ValueError(f"محتوى فارغ ({len(paragraphs)} فقرة) — الموقع يحتاج JS rendering")

    return {
        "title":           novel_title   or "رواية",
        "chapter_title":   chapter_title or f"الفصل {chapter_num}",
        "paragraphs":      paragraphs,
        "url":             url,
        "paragraph_count": len(paragraphs),
    }

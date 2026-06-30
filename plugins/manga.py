# plugins/manga_scraper.py
"""
plugins/manga_scraper.py
endpoint: POST /manga/extract-chapter
الوصف: كشط واستخراج روابط صور فصول المانجا والمانهوا المترجمة للعربية، مع دعم
البحث عن رقم فصل محدد أو عن آخر فصل منشور تلقائياً (chapter_number = "%%").
"""

import re
import asyncio
import logging
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger("manga_scraper")

# تعريف إلزامي يقرأه الـ loader ويعرضه في قائمة الـ plugins عند طلب GET /
DESCRIPTION = "كشط واستخراج روابط صور فصول المانجا والمانهوا المترجمة للعربية، بحث برقم فصل أو عن آخر فصل (%%)"

# حزم النظام (apt) المطلوبة لتشغيل متصفح Chromium الخاص بـ Playwright داخل Docker بدون مشاكل
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
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# الرمز الخاص الذي يطلب البحث عن آخر فصل منشور بدل رقم فصل محدد
LATEST_CHAPTER_FLAG = "%%"


def _find_chapter_link(html_content: str, chapter_number) -> str | None:
    """يبحث في HTML صفحة المانجا الرئيسية عن رابط فصل بعينه عبر regex مرن."""
    soup = BeautifulSoup(html_content, "lxml")
    all_links = soup.find_all("a", href=True)

    # نمط بحث مرن يطابق الرقم سواء كان: /3/ أو /03/ أو /chapter-3 أو /3-
    # (?!\d) بعد "-" يمنع مطابقة فصل كامل (521) بالغلط مع فصل نصفي تابع له (521-5)
    pattern = re.compile(rf"(/|[-_])(chapter[-_])?0*{chapter_number}(?:[/_\s]|-(?!\d)|$)")

    for link in all_links:
        href = link["href"].strip()
        if "/manga/" in href and pattern.search(href.lower()):
            return href
    return None


def _find_latest_chapter_link(html_content: str, manga_main_url: str) -> str | None:
    """
    يبحث عن رابط آخر فصل منشور داخل صفحة المانجا الرئيسية.
    قوالب Madara تعرض قائمة الفصول بترتيب الأحدث أولاً، لذلك نأخذ أول رابط
    فصل صالح (مع استثناء رابط صفحة المانجا نفسها وأي روابط لمواقع/مانجات أخرى).
    """
    soup = BeautifulSoup(html_content, "lxml")
    base = manga_main_url.rstrip("/")

    # محددات شائعة لقائمة الفصول في قوالب Madara، مع fallback عام لأي رابط /manga/
    candidates = (
        soup.select("li.wp-manga-chapter a")
        or soup.select(".version-chap li a")
        or soup.select("ul.main.version-chap a")
        or soup.select("a[href*='/manga/']")
    )

    for a in candidates:
        href = (a.get("href") or "").strip().rstrip("/")
        if not href or "/manga/" not in href:
            continue
        if href == base:
            continue  # نفس رابط صفحة المانجا نفسها، تجاهله
        if href.startswith(base + "/"):
            return href + "/"
    return None


def _search_manga_main_url(page, query: str) -> str | None:
    """
    يبحث عن المانجا عبر محرك بحث الموقع (نمط Madara/WordPress المعياري:
    /?s={query}&post_type=wp-manga) ويرجع رابط صفحة المانجا الأولى المطابقة.
    يُستخدم كـ fallback عندما يكون الـ slug المُدخَل من المستخدم لا يطابق
    الـ slug الفعلي على الموقع (مثال: 'one-piece' بينما الموقع يستخدم 'pieceone').
    """
    search_url = f"https://lek-manga.net/?s={quote(query)}&post_type=wp-manga"
    page.goto(search_url, wait_until="networkidle", timeout=60000)
    html_content = page.content()
    soup = BeautifulSoup(html_content, "lxml")

    # محددات شائعة لنتائج بحث قوالب Madara، مع fallback عام لأي رابط /manga/
    candidates = (
        soup.select("div.c-tabs-item__content .post-title a")
        or soup.select(".search-wrap .post-title a")
        or soup.select("a[href*='/manga/']")
    )

    for a in candidates:
        href = (a.get("href") or "").strip()
        if "/manga/" in href:
            return href.rstrip("/") + "/"
    return None


def _scrape_manga_chapter(manga_name: str, chapter_number):
    """
    دالة متزامنة (sync) بالكامل تنفّذ كل عمليات Playwright/BeautifulSoup.
    تُستدعى دائماً عبر run_in_executor من الراوت الـ async لتفادي حظر event loop.
    لو chapter_number == "%%" يبحث عن آخر فصل منشور بدل رقم محدد.
    ترجع dict موحَّد: {"ok": bool, "error": str|None, "images": list,
                        "chapter_url": str|None, "detected_chapter": str|None}
    """
    is_latest = str(chapter_number).strip() == LATEST_CHAPTER_FLAG

    # بناء رابط المانجا الرئيسي المباشر (تخمين أولي بافتراض أن manga_name = slug صحيح)
    direct_main_url = f"https://lek-manga.net/manga/{manga_name}/"
    logger.info(f"[manga] محاولة مباشرة: {direct_main_url}")
    print(f"[manga_scraper] 🔗 محاولة مباشرة: {direct_main_url}", flush=True)

    def _locate_chapter(html_content: str, main_url: str):
        if is_latest:
            return _find_latest_chapter_link(html_content, main_url)
        return _find_chapter_link(html_content, chapter_number)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            target_chapter_url = None

            # 1. محاولة مباشرة بافتراض أن manga_name هو الـ slug الصحيح
            try:
                page.goto(direct_main_url, wait_until="networkidle", timeout=60000)
                html_content = page.content()
                target_chapter_url = _locate_chapter(html_content, direct_main_url)
            except Exception:
                target_chapter_url = None

            # 2. لو فشلت المحاولة المباشرة (slug غير مطابق)، نلجأ للبحث التلقائي
            #    عبر محرك بحث الموقع لإيجاد رابط المانجا الصحيح
            if not target_chapter_url:
                search_query = manga_name.replace("-", " ").replace("_", " ")
                print(f"[manga_scraper] 🔍 المحاولة المباشرة فشلت، البحث عن: '{search_query}'", flush=True)
                try:
                    resolved_main_url = _search_manga_main_url(page, search_query)
                    print(f"[manga_scraper] 🔗 رابط محلول من البحث: {resolved_main_url}", flush=True)
                except Exception as e:
                    print(f"[manga_scraper] ❌ فشل البحث التلقائي: {e}", flush=True)
                    resolved_main_url = None

                if resolved_main_url and resolved_main_url != direct_main_url:
                    try:
                        page.goto(resolved_main_url, wait_until="networkidle", timeout=60000)
                        html_content = page.content()
                        target_chapter_url = _locate_chapter(html_content, resolved_main_url)
                    except Exception:
                        target_chapter_url = None

            if not target_chapter_url:
                if is_latest:
                    error_msg = (
                        f"تعذّر العثور على آخر فصل لـ '{manga_name}'، "
                        f"حتى بعد محاولة البحث التلقائي عن اسم المانجا على الموقع."
                    )
                else:
                    error_msg = (
                        f"لم يتم العثور على الفصل رقم {chapter_number} لـ '{manga_name}'، "
                        f"حتى بعد محاولة البحث التلقائي عن اسم المانجا على الموقع. "
                        f"تأكد من صحة الاسم ورقم الفصل."
                    )
                return {
                    "ok": False,
                    "error": error_msg,
                    "images": [],
                    "chapter_url": None,
                    "detected_chapter": None,
                }

            # استخراج رقم/تسمية الفصل المكتشف فعلياً من الرابط (مفيد خاصة في وضع %%)
            detected_chapter = target_chapter_url.rstrip("/").split("/")[-1]
            print(f"[manga_scraper] ✅ رابط الفصل المكتشف: {target_chapter_url} (detected={detected_chapter})", flush=True)

            # 3. كشط الصور من رابط الفصل الذي عثرنا عليه (نفس المتصفح، صفحة جديدة)
            chapter_page = context.new_page()
            chapter_page.goto(target_chapter_url, wait_until="networkidle", timeout=60000)
            chapter_page.wait_for_timeout(4000)  # وقت أمان لضمان توليد الـ JavaScript للصور

            chapter_html = chapter_page.content()
            soup = BeautifulSoup(chapter_html, "lxml")

            # استهداف حاويات قالب Madara الشهير للمانجا
            image_container = (
                soup.find(class_="wp-manga-section")
                or soup.find(class_="reading-content")
                or soup.find(class_="page-break")
            )

            images = image_container.find_all("img") if image_container else soup.find_all("img")

            valid_image_urls = []
            for img in images:
                url = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if not url:
                    continue
                clean_url = url.strip()

                # تصفية الأيقونات والإعلانات وصور التقييم
                if any(bad_word in clean_url.lower() for bad_word in ["emoji", "logo", "avatar", "banner", "icon"]):
                    continue

                # التحقق من الامتدادات ومجلدات الرفع للشابتر
                if "uploads" in clean_url or "wp-content" in clean_url:
                    if any(ext in clean_url.lower() for ext in [".jpg", ".png", ".webp", ".jpeg"]):
                        valid_image_urls.append(clean_url)

            # إزالة التكرار مع الحفاظ على ترتيب الصفحات
            valid_image_urls = list(dict.fromkeys(valid_image_urls))

            if not valid_image_urls:
                return {
                    "ok": False,
                    "error": "فشل استخراج الصور من داخل صفحة الفصل.",
                    "images": [],
                    "chapter_url": target_chapter_url,
                    "detected_chapter": detected_chapter,
                }

            return {
                "ok": True,
                "error": None,
                "images": valid_image_urls,
                "chapter_url": target_chapter_url,
                "detected_chapter": detected_chapter,
            }
        finally:
            browser.close()


def register(app):
    """
    الدالة الإلزامية لتسجيل الـ Route الجديد على تطبيق FastAPI الرئيسي تلقائياً.
    سيمر هذا الـ Endpoint تلقائياً عبر الـ middleware للتحقق من X-Internal-Token.
    """

    @app.post("/manga/extract-chapter")
    async def extract_manga_chapter(request: Request):
        try:
            body = await request.json()
            manga_name = body.get("manga_name")
            chapter_number = body.get("chapter_number")

            # طباعة الطلب الخام القادم من Render فور استقباله (قبل أي تحقق/معالجة)
            # لتشخيص أي مشكلة في أسماء الحقول أو القيم المُرسلة من البوت
            print(f"[manga_scraper] 📥 طلب وارد: body={body}", flush=True)
            logger.info(f"[manga] طلب وارد: manga_name={manga_name!r}, chapter_number={chapter_number!r}")

            if not manga_name or chapter_number is None:
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "message": "يجب إرسال manga_name و chapter_number بشكل صحيح."},
                )

            # تشغيل كود Playwright (متزامن/blocking) في thread منفصل
            # حتى لا يحظر event loop الخاص بـ FastAPI أثناء التصفح
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _scrape_manga_chapter, manga_name, chapter_number)

            if not result["ok"]:
                status_code = 404 if ("لم يتم العثور" in (result["error"] or "") or "تعذّر العثور" in (result["error"] or "")) else 500
                return JSONResponse(
                    status_code=status_code,
                    content={"status": "error", "message": result["error"]},
                )

            # نرجّع رقم/تسمية الفصل المكتشف فعلياً (مهم خصوصاً في وضع %% لأن "%%"
            # نفسها لا تعني شيئاً للمستخدم، والبوت يحتاج يعرض الرقم الحقيقي)
            return JSONResponse(
                {
                    "status": "ok",
                    "manga_name": manga_name,
                    "chapter_number": result.get("detected_chapter") or chapter_number,
                    "total_pages": len(result["images"]),
                    "images": result["images"],
                }
            )

        except Exception as e:
            print(f"[manga_scraper] ❌ خطأ غير متوقع: {e}", flush=True)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": f"حدث خطأ غير متوقع أثناء معالجة الطلب: {str(e)}"},
            )

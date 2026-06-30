# plugins/manga.py
"""
plugins/manga.py
endpoint: POST /manga
يجلب روابط صور فصل مانجا من موقع 3asq.org (يدعم Cloudflare عبر cloudscraper)
ويُعيدها كقائمة JSON ليرسلها البوت على Render داخل مجموعة فيسبوك.
"""

import time
import cloudscraper
from bs4 import BeautifulSoup
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "جلب روابط صور فصل مانجا من 3asq.org (manga_name + chapter_num)"

# pip deps إضافية لهذا الـ plugin فقط -> ضع نفس الأسطر في plugins/requirements/manga.txt
# cloudscraper
# beautifulsoup4

BASE_URL = "https://3asq.org"


def _get_chapter_images(manga_name: str, chapter_num: str):
    """
    يرجع (image_urls, error_message)
    image_urls: قائمة روابط الصور بالترتيب
    error_message: نص الخطأ إن فشل شيء، وإلا None
    """
    chapter_url = f"{BASE_URL}/{manga_name}/{chapter_num}/"
    scraper = cloudscraper.create_scraper()

    try:
        response = scraper.get(chapter_url, timeout=30)
    except Exception as e:
        return None, f"فشل الاتصال بالموقع: {e}"

    if response.status_code != 200:
        return None, f"فشل الاتصال بالموقع (كود الخطأ: {response.status_code})"

    soup = BeautifulSoup(response.text, "html.parser")

    image_containers = soup.find_all("div", class_="page-break")
    if not image_containers:
        image_containers = soup.find_all("div", class_="wp-manga-chapter-img")

    if not image_containers:
        return None, "لم يتم العثور على صور في هذا الفصل، تأكد من صحة اسم المانجا أو رقم الفصل."

    image_urls = []
    for container in image_containers:
        img_tag = container.find("img")
        if not img_tag:
            continue
        img_url = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src")
        if img_url:
            image_urls.append(img_url.strip())

    if not image_urls:
        return None, "تم العثور على حاويات صور لكن بدون روابط صالحة."

    return image_urls, None


def register(app):
    """
    دالة إلزامية: تسجّل /manga على تطبيق FastAPI.
    يمر هذا المسار تلقائياً عبر middleware التحقق من X-Internal-Token
    (نفس آلية بقية الـ endpoints الموثقة في README).
    """

    @app.post("/manga")
    async def manga_endpoint(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        manga_name = (body.get("manga_name") or "").strip()
        chapter_num = str(body.get("chapter_num") or "").strip()

        if not manga_name or not chapter_num:
            return JSONResponse(
                {"status": "error", "message": "manga_name و chapter_num مطلوبان"},
                status_code=400,
            )

        image_urls, error = _get_chapter_images(manga_name, chapter_num)

        if error:
            return JSONResponse({"status": "error", "message": error}, status_code=404)

        return JSONResponse(
            {
                "status": "ok",
                "manga_name": manga_name,
                "chapter_num": chapter_num,
                "pages": len(image_urls),
                "images": image_urls,
            }
        )

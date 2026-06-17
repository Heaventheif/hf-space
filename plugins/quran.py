"""
plugins/quran.py
endpoint: POST /quran
"""
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "جلب الآيات القرآنية مع التفسير الميسر"


def register(app):

    @app.post("/quran")
    async def quran(request: Request):
        """
        Body: { "surah": 2, "ayah": 255 }
        Response: { "text": "...", "tafsir": "...", "meta": {...} }
        """
        try:
            body   = await request.json()
            surah  = int(body.get("surah", 0))
            ayah   = int(body.get("ayah", 0))

            if not (1 <= surah <= 114):
                return JSONResponse({"error": "رقم السورة يجب أن يكون بين 1 و114"}, status_code=400)
            if ayah < 1:
                return JSONResponse({"error": "رقم الآية يجب أن يكون أكبر من 0"}, status_code=400)

            url = f"https://api.alquran.cloud/v1/ayah/{surah}:{ayah}/editions/quran-uthmani,ar.muyassar"

            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
                if r.status_code == 404:
                    return JSONResponse({"error": f"الآية {surah}:{ayah} غير موجودة"}, status_code=404)
                r.raise_for_status()
                data = r.json()

            editions = data.get("data", [])
            if len(editions) < 2:
                return JSONResponse({"error": "بيانات غير مكتملة"}, status_code=502)

            ayah_data   = editions[0]
            tafsir_data = editions[1]
            surah_info  = ayah_data.get("surah", {})

            return JSONResponse({
                "text":   ayah_data.get("text", ""),
                "tafsir": tafsir_data.get("text", ""),
                "meta": {
                    "surah_name":    surah_info.get("name", ""),
                    "surah_english": surah_info.get("englishName", ""),
                    "revelation":    surah_info.get("revelationType", ""),
                    "juz":           ayah_data.get("juz", ""),
                    "page":          ayah_data.get("page", ""),
                    "surah":         surah,
                    "ayah":          ayah,
                }
            })

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

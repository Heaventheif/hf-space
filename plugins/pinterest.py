"""
plugins/pinterest.py
endpoint: POST /pinterest
"""
import os
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "البحث عن صور من Pinterest"

# ─── Shared HTTP client (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

FERDEV_KEY = os.environ.get("FERDEV_API_KEY", "")


def register(app):

    @app.post("/pinterest")
    async def pinterest(request: Request):
        """
        Body: { "query": "nature wallpaper", "limit": 5 }
        Response: { "images": ["url1", "url2", ...] }
        """
        try:
            body  = await request.json()
            query = body.get("query", "").strip()
            limit = min(int(body.get("limit", 5)), 10)

            if not query:
                return JSONResponse({"error": "query مطلوب"}, status_code=400)

            key = FERDEV_KEY or "FREE"

            r = await _http.get(
                "https://api.ferdev.my.id/search/pinterest",
                params={"query": query, "apikey": key},
                headers={"User-Agent": "SunkenBot/2.0"},
            )
            r.raise_for_status()
            data = r.json()

            results = data.get("result", [])
            if not results:
                return JSONResponse({"error": "لم تُوجد نتائج", "images": []}, status_code=404)

            images = []
            for item in results[:limit]:
                url = item.get("url") or item.get("image") or (item if isinstance(item, str) else None)
                if url:
                    images.append(url)

            return JSONResponse({"images": images, "query": query})

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

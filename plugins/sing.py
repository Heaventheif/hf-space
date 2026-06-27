"""
plugins/sing.py
endpoints: POST /sing/search  |  POST /sing/download
"""
import os, random, httpx, base64
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "البحث والتحميل من SoundCloud"

# ─── Shared HTTP clients (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=20,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)
_http_dl = httpx.AsyncClient(
    timeout=90,
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=3, max_connections=5),
)

FERDEV_KEYS = [k for k in [
    os.environ.get("FERDEV_API_KEY"),
    os.environ.get("FERDEV_API_KEY2"),
    os.environ.get("FERDEV_API_KEY3"),
] if k]

MAX_BYTES = 25 * 1024 * 1024  # 25MB


def _key():
    return random.choice(FERDEV_KEYS) if FERDEV_KEYS else "FREE"


def register(app):

    @app.post("/sing/search")
    async def sing_search(request: Request):
        """
        Body: { "query": "shape of you" }
        Response: { "results": [{"title":"...","url":"..."}, ...] }
        """
        try:
            body  = await request.json()
            query = body.get("query", "").strip()
            if not query:
                return JSONResponse({"error": "query مطلوب"}, status_code=400)

            r = await _http.get(
                "https://api.ferdev.my.id/search/soundcloud",
                params={"query": query, "apikey": _key()},
            )
                r.raise_for_status()
                data = r.json()

            items = data.get("result", [])
            results = []
            for track in items[:7]:
                title = track.get("title", f"أغنية {len(results)+1}")
                url   = track.get("url") or track.get("permalink_url") or track.get("link")
                if url:
                    results.append({"title": title, "url": url})

            if not results:
                return JSONResponse({"error": "لم تُوجد نتائج"}, status_code=404)

            return JSONResponse({"results": results})

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)


    @app.post("/sing/download")
    async def sing_download(request: Request):
        """
        Body: { "url": "https://soundcloud.com/..." }
        Response: { "audio_b64": "...", "title": "...", "size": 123456 }
        """
        try:
            body  = await request.json()
            url   = body.get("url", "").strip()
            title = body.get("title", "أغنية")
            if not url:
                return JSONResponse({"error": "url مطلوب"}, status_code=400)

            dl = await _http.get(
                "https://api.ferdev.my.id/downloader/soundcloud",
                params={"link": url, "apikey": _key()},
            )
            dl.raise_for_status()
            dl_data = dl.json()

            download_url = (
                dl_data.get("result", {}).get("downloadUrl")
                or dl_data.get("result", {}).get("url")
                or dl_data.get("result", {}).get("download_url")
            )
            if not download_url:
                return JSONResponse({"error": "لم يُرجع الـ API رابط تحميل"}, status_code=502)

            audio = await _http_dl.get(
                download_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            audio.raise_for_status()
            raw = audio.content

            if not raw:
                return JSONResponse({"error": "الملف فارغ"}, status_code=502)
            if len(raw) > MAX_BYTES:
                return JSONResponse({"error": "الملف أكبر من 25MB"}, status_code=413)

            return JSONResponse({
                "audio_b64": base64.b64encode(raw).decode(),
                "title":     title,
                "size":      len(raw),
            })

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

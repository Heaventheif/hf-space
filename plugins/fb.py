"""
plugins/fb.py
endpoint: POST /fb
"""
import httpx, base64, re
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "تحميل فيديوهات فيسبوك"

FDOWN    = "https://facebook-video-download-api.onrender.com"
MAX_BYTES = 25 * 1024 * 1024


async def _get_video_url(fb_url: str, quality: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{FDOWN}/download",
            json={"url": fb_url, "quality": quality},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    return {
        "video_url": data.get("download_url") or (data.get("available_formats") or [{}])[0].get("url"),
        "title":     data.get("video_info", {}).get("title", "فيديو فيسبوك"),
    }


def register(app):

    @app.post("/fb")
    async def fb_download(request: Request):
        """
        Body: { "url": "https://facebook.com/...", "quality": "worst" | "720p" }
        Response:
          مع ملف:    { "video_b64": "...", "title": "...", "size": N }
          برابط:     { "video_url": "...", "title": "..." }
        """
        try:
            body    = await request.json()
            fb_url  = body.get("url", "").strip()
            quality = body.get("quality", "worst")

            if not fb_url:
                return JSONResponse({"error": "url مطلوب"}, status_code=400)

            # جرب الجودة المطلوبة ثم worst كـ fallback
            qualities = [quality, "worst"] if quality != "worst" else ["worst"]
            result    = None
            for q in qualities:
                try:
                    r = await _get_video_url(fb_url, q)
                    if r["video_url"]:
                        result = r
                        break
                except Exception:
                    continue

            if not result or not result["video_url"]:
                return JSONResponse({"error": "لم يُعثر على الفيديو"}, status_code=404)

            video_url = result["video_url"]
            title     = result["title"]

            # حاول تحميل الفيديو
            async with httpx.AsyncClient(timeout=120) as client:
                dl = await client.get(video_url, follow_redirects=True)
                dl.raise_for_status()
                content = dl.content

            if not content:
                return JSONResponse({"error": "الملف فارغ"}, status_code=502)

            if len(content) > MAX_BYTES:
                # حاول بجودة أقل إذا لم نكن عليها
                if quality != "worst":
                    try:
                        r2 = await _get_video_url(fb_url, "worst")
                        if r2["video_url"]:
                            async with httpx.AsyncClient(timeout=120) as client:
                                dl2 = await client.get(r2["video_url"], follow_redirects=True)
                                content2 = dl2.content
                            if content2 and len(content2) <= MAX_BYTES:
                                return JSONResponse({
                                    "video_b64": base64.b64encode(content2).decode(),
                                    "title":     title,
                                    "size":      len(content2),
                                })
                    except Exception:
                        pass
                return JSONResponse({"error": "الفيديو أكبر من 25MB"}, status_code=413)

            return JSONResponse({
                "video_b64": base64.b64encode(content).decode(),
                "title":     title,
                "size":      len(content),
            })

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

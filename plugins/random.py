"""
plugins/random.py
endpoint: POST /random
"""
import os, random, httpx, base64
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "فيديو عشوائي من Tumblr"

TUMBLR_KEY = os.environ.get("TUMBLR_API_KEY", "")
MAX_BYTES  = 25 * 1024 * 1024

VIDEO_BLOGS = [
    "videohall", "gifak-net", "sizvideos", "pleatedjeans",
    "tastefullyoffensive", "humortrain", "best-of-tumblr-daily",
    "videogifs", "funnyordie", "motionaddicts",
    "catasters", "kittens", "there-is-always-hope",
    "awesome-picz", "thefrogman",
]


def register(app):

    @app.post("/random")
    async def random_video(request: Request):
        """
        Response:
          نجاح مع ملف:  { "video_b64": "...", "caption": "...", "blog": "...", "size": N }
          نجاح برابط:   { "url": "...", "caption": "...", "blog": "..." }
          فشل:          { "error": "..." }
        """
        if not TUMBLR_KEY:
            return JSONResponse({"error": "TUMBLR_API_KEY غير موجود"}, status_code=500)

        shuffled = random.sample(VIDEO_BLOGS, len(VIDEO_BLOGS))

        video_url = caption = blog_name = post_url = None

        async with httpx.AsyncClient(timeout=10) as client:
            for blog in shuffled:
                try:
                    offset = random.randint(0, 20)
                    r = await client.get(
                        f"https://api.tumblr.com/v2/blog/{blog}/posts/video",
                        params={"api_key": TUMBLR_KEY, "limit": 20, "offset": offset},
                    )
                    if r.status_code != 200:
                        continue
                    posts = r.json().get("response", {}).get("posts", [])
                    if not posts:
                        continue

                    post = random.choice(posts)
                    url  = post.get("video_url")
                    if not url:
                        players = post.get("player", [])
                        for p in players:
                            embed = p.get("embed_code", "")
                            if embed.startswith("http"):
                                url = embed
                                break
                    if not url or not url.startswith("http"):
                        continue

                    video_url = url
                    post_url  = post.get("post_url", "")
                    blog_name = blog
                    caption   = (
                        (post.get("summary") or post.get("caption") or "")
                        .replace("<br>", " ").replace("</p>", " ")
                    )
                    import re
                    caption = re.sub(r"<[^>]+>", "", caption).strip()[:100]
                    break

                except Exception:
                    continue

        if not video_url:
            return JSONResponse({"error": "لم أجد فيديو الآن — حاول مرة أخرى"}, status_code=404)

        # حاول تحميل الفيديو
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                dl = await client.get(video_url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                if dl.status_code != 200:
                    raise Exception("فشل التحميل")
                content = dl.content

            if not content:
                raise Exception("الملف فارغ")

            if len(content) > MAX_BYTES:
                # أرجع الرابط فقط
                return JSONResponse({"url": post_url or video_url, "caption": caption, "blog": blog_name})

            return JSONResponse({
                "video_b64": base64.b64encode(content).decode(),
                "caption":   caption,
                "blog":      blog_name,
                "size":      len(content),
            })

        except Exception:
            return JSONResponse({"url": post_url or video_url, "caption": caption, "blog": blog_name})

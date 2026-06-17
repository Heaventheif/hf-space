"""
plugins/sc_preview.py
endpoint: POST /sc/preview

يجلب مقطع الـ preview (30 ثانية) من SoundCloud بدون تسجيل دخول
──────────────────────────────────────────────────────────────────
التدفق:
  1. نجيب client_id من صفحة SoundCloud (أو من cache)
  2. نستدعي /resolve API لنحصل على track info + media transcodings
  3. نختار الـ transcoding المناسب (preview/mp3 أو hls)
  4. نجيب رابط stream المؤقت ونُرجعه
"""

import re, json, base64, logging
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "جلب مقطع preview من SoundCloud (30 ثانية)"
log = logging.getLogger("sc_preview")

# ─── ثابت ─────────────────────────────────────────────────────
SC_BASE    = "https://soundcloud.com"
SC_API     = "https://api-v2.soundcloud.com"
MAX_BYTES  = 10 * 1024 * 1024   # 10MB كافية للـ preview

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://soundcloud.com/",
}

# ─── cache بسيط للـ client_id ─────────────────────────────────
_client_id_cache: str | None = None


async def _get_client_id(client: httpx.AsyncClient) -> str:
    """يجيب client_id من صفحة SoundCloud أو من الـ JS bundles"""
    global _client_id_cache
    if _client_id_cache:
        return _client_id_cache

    # 1. جيب الصفحة الرئيسية
    r = await client.get(SC_BASE + "/", headers=HEADERS, timeout=15)
    html = r.text

    # 2. ابحث عن روابط JS bundles
    js_urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    if not js_urls:
        js_urls = re.findall(r'"(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)

    # 3. فتش في آخر عدة bundles عن client_id
    for js_url in reversed(js_urls[-5:]):
        try:
            js_r = await client.get(js_url, headers=HEADERS, timeout=10)
            match = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{20,})"', js_r.text)
            if match:
                _client_id_cache = match.group(1)
                log.info(f"client_id found: {_client_id_cache[:8]}...")
                return _client_id_cache
        except Exception:
            continue

    raise RuntimeError("تعذّر استخراج client_id من SoundCloud")


async def _resolve_track(client: httpx.AsyncClient, sc_url: str, client_id: str) -> dict:
    """يحل رابط SoundCloud ويُرجع معلومات الـ track"""
    r = await client.get(
        f"{SC_API}/resolve",
        params={"url": sc_url, "client_id": client_id},
        headers=HEADERS,
        timeout=15,
    )
    if r.status_code == 401:
        # client_id انتهت صلاحيته — امسح الـ cache
        global _client_id_cache
        _client_id_cache = None
        raise RuntimeError("client_id منتهية — أعد المحاولة")
    r.raise_for_status()
    return r.json()


def _pick_preview_transcoding(track: dict) -> dict | None:
    """
    يختار أفضل transcoding للـ preview:
    - يفضل format=mp3 + preset=mp3_standard
    - يقبل hls كبديل
    - يفلتر على quality=preview أو duration صغير
    """
    media = track.get("media", {})
    transcodings = media.get("transcodings", [])

    previews = [t for t in transcodings if t.get("snipped") is True]

    # إذا ما فيه snipped صريح — خذ أقصر duration
    if not previews:
        previews = sorted(
            transcodings,
            key=lambda t: t.get("duration", 999999)
        )

    if not previews:
        return None

    # فضل progressive (mp3 مباشر) على hls
    for t in previews:
        if t.get("format", {}).get("protocol") == "progressive":
            return t

    return previews[0]


async def _get_stream_url(client: httpx.AsyncClient, transcoding: dict, client_id: str) -> str:
    """يحول رابط transcoding إلى رابط stream مباشر"""
    url = transcoding.get("url")
    if not url:
        raise RuntimeError("الـ transcoding لا يحتوي على url")

    r = await client.get(
        url,
        params={"client_id": client_id},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    stream_url = data.get("url")
    if not stream_url:
        raise RuntimeError("لم يُرجع الـ API رابط stream")
    return stream_url


async def _download_preview(client: httpx.AsyncClient, stream_url: str) -> bytes:
    """يحمل الـ preview — يدعم progressive (mp3) و hls (m3u8)"""

    # ─── Progressive MP3 ─────────────────────────────────────
    if ".m3u8" not in stream_url:
        r = await client.get(
            stream_url,
            headers=HEADERS,
            timeout=30,
            follow_redirects=True,
        )
        r.raise_for_status()
        raw = r.content
        if not raw:
            raise RuntimeError("الملف فارغ")
        if len(raw) > MAX_BYTES:
            raise RuntimeError("الملف أكبر من 10MB")
        return raw

    # ─── HLS (m3u8) ──────────────────────────────────────────
    r = await client.get(stream_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    m3u8 = r.text

    # استخرج روابط الـ segments
    base_url = stream_url.rsplit("/", 1)[0]
    segments = []
    for line in m3u8.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg_url = line if line.startswith("http") else f"{base_url}/{line}"
            segments.append(seg_url)

    if not segments:
        raise RuntimeError("لم تُوجد segments في ملف HLS")

    # حمّل كل الـ segments ودمجهم
    chunks = []
    total = 0
    for seg_url in segments:
        seg = await client.get(seg_url, headers=HEADERS, timeout=15)
        seg.raise_for_status()
        chunks.append(seg.content)
        total += len(seg.content)
        if total > MAX_BYTES:
            raise RuntimeError("الملف أكبر من 10MB")

    return b"".join(chunks)


# ══════════════════════════════════════════════════════════════
def register(app):

    @app.post("/sc/preview")
    async def sc_preview(request: Request):
        """
        Body: { "url": "https://soundcloud.com/artist/track" }

        Response:
        {
          "title":      "اسم الأغنية",
          "artist":     "اسم الفنان",
          "duration_ms": 30000,
          "format":     "mp3" | "aac",
          "audio_b64":  "base64...",
          "size":       123456
        }
        """
        try:
            body    = await request.json()
            sc_url  = body.get("url", "").strip()

            if not sc_url:
                return JSONResponse({"error": "url مطلوب"}, status_code=400)

            if "soundcloud.com" not in sc_url:
                return JSONResponse({"error": "الرابط يجب أن يكون من soundcloud.com"}, status_code=400)

            async with httpx.AsyncClient(timeout=60) as client:

                # 1. client_id
                try:
                    client_id = await _get_client_id(client)
                except RuntimeError as e:
                    return JSONResponse({"error": str(e)}, status_code=503)

                # 2. resolve track
                try:
                    track = await _resolve_track(client, sc_url, client_id)
                except RuntimeError as e:
                    return JSONResponse({"error": str(e)}, status_code=502)
                except httpx.HTTPStatusError as e:
                    return JSONResponse(
                        {"error": f"SoundCloud رفض الطلب: {e.response.status_code}"},
                        status_code=502
                    )

                if track.get("kind") != "track":
                    return JSONResponse(
                        {"error": "الرابط ليس track — جرب رابط أغنية مباشر"},
                        status_code=400
                    )

                # 3. اختر transcoding
                transcoding = _pick_preview_transcoding(track)
                if not transcoding:
                    return JSONResponse(
                        {"error": "لا يوجد preview متاح لهذه الأغنية"},
                        status_code=404
                    )

                log.info(f"transcoding: {transcoding.get('format')} snipped={transcoding.get('snipped')} dur={transcoding.get('duration')}ms")

                # 4. رابط stream
                try:
                    stream_url = await _get_stream_url(client, transcoding, client_id)
                except Exception as e:
                    return JSONResponse({"error": f"فشل جلب رابط الـ stream: {e}"}, status_code=502)

                # 5. تحميل الـ preview
                try:
                    audio_bytes = await _download_preview(client, stream_url)
                except Exception as e:
                    return JSONResponse({"error": f"فشل تحميل الـ preview: {e}"}, status_code=502)

            # تحديد الـ format
            fmt = "aac"
            if transcoding.get("format", {}).get("mime_type", "").startswith("audio/mpeg"):
                fmt = "mp3"
            elif ".mp3" in stream_url:
                fmt = "mp3"

            return JSONResponse({
                "title":       track.get("title", "بدون عنوان"),
                "artist":      track.get("user", {}).get("username", ""),
                "duration_ms": transcoding.get("duration", 0),
                "format":      fmt,
                "audio_b64":   base64.b64encode(audio_bytes).decode(),
                "size":        len(audio_bytes),
            })

        except Exception as e:
            log.exception("sc_preview error")
            return JSONResponse({"error": str(e)[:300]}, status_code=500)


    @app.post("/sc/preview/url")
    async def sc_preview_url(request: Request):
        """
        نفس /sc/preview لكن يُرجع رابط stream مباشر بدل base64
        مفيد إذا أردت تشغيله مباشرة بدون تحميل

        Body:    { "url": "https://soundcloud.com/..." }
        Response: { "title": "...", "artist": "...", "stream_url": "...", "duration_ms": 30000 }
        """
        try:
            body   = await request.json()
            sc_url = body.get("url", "").strip()

            if not sc_url or "soundcloud.com" not in sc_url:
                return JSONResponse({"error": "url غير صالح"}, status_code=400)

            async with httpx.AsyncClient(timeout=30) as client:
                client_id    = await _get_client_id(client)
                track        = await _resolve_track(client, sc_url, client_id)
                transcoding  = _pick_preview_transcoding(track)

                if not transcoding:
                    return JSONResponse({"error": "لا يوجد preview"}, status_code=404)

                stream_url = await _get_stream_url(client, transcoding, client_id)

            return JSONResponse({
                "title":       track.get("title", "بدون عنوان"),
                "artist":      track.get("user", {}).get("username", ""),
                "duration_ms": transcoding.get("duration", 0),
                "stream_url":  stream_url,
            })

        except Exception as e:
            log.exception("sc_preview_url error")
            return JSONResponse({"error": str(e)[:300]}, status_code=500)

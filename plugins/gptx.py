"""
plugins/gptx.py
endpoint: POST /gptx
GPT-4o عبر GitHub Models — جلسات جماعية + دعم الصور
"""
import os, base64, httpx
from datetime import datetime
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "GPT-4o — جلسات جماعية + دعم الصور"

# ─── Shared HTTP clients (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)
_http_dl = httpx.AsyncClient(
    timeout=15,
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

GITHUB_TOKEN = os.environ.get("GITHUB_MODELS_TOKEN", "")
MONGO_URI    = os.environ.get("MONGO_URI", "")

SYSTEM = 'أنت مساعد ذكي اسمك "Sunken". أجب بإيجاز باللغة العربية (أقل من 150 كلمة). كن ودوداً ومهذباً.'

# ─── MongoDB ──────────────────────────────────────────────────
_db = None

async def _get_db():
    global _db
    if _db is not None:
        return _db
    if not MONGO_URI:
        return None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = client["sunken"]["gptx_sessions"]
        return _db
    except Exception:
        return None

async def _load(thread_id: str) -> list:
    col = await _get_db()
    if not col:
        return []
    doc = await col.find_one({"_id": thread_id})
    return (doc or {}).get("messages", [])[-10:]

async def _save(thread_id: str, messages: list):
    col = await _get_db()
    if not col:
        return
    await col.update_one(
        {"_id": thread_id},
        {"$set": {"messages": messages[-10:], "updated_at": datetime.utcnow()}},
        upsert=True,
    )

async def _clear(thread_id: str):
    col = await _get_db()
    if col:
        await col.delete_one({"_id": thread_id})


def register(app):

    @app.post("/gptx")
    async def gptx(request: Request):
        """
        Body: {
          "thread_id":    "group_123",
          "sender_name":  "Ahmed",
          "prompt":       "سؤالك",
          "image_url":    "https://..." (اختياري),
          "image_b64":    "base64..."  (اختياري بديل),
          "image_type":   "image/jpeg" (اختياري),
          "clear":        false
        }
        Response: { "reply": "..." }
        """
        try:
            body        = await request.json()
            thread_id   = body.get("thread_id", "default")
            sender_name = body.get("sender_name", "مستخدم")
            prompt      = body.get("prompt", "").strip()
            image_url   = body.get("image_url", "")
            image_b64   = body.get("image_b64", "")
            image_type  = body.get("image_type", "image/jpeg")
            do_clear    = body.get("clear", False)

            if do_clear:
                await _clear(thread_id)
                return JSONResponse({"reply": "🧹 تم مسح ذاكرة المجموعة."})

            if not GITHUB_TOKEN:
                return JSONResponse({"error": "GITHUB_MODELS_TOKEN غير مضبوط"}, status_code=500)

            # ─── جهّز محتوى المستخدم ──────────────────────────
            has_image = bool(image_url or image_b64)

            if has_image:
                # حمّل الصورة إذا وُجد رابط
                if image_url and not image_b64:
                    img_r = await _http_dl.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
                    img_r.raise_for_status()
                    image_b64  = base64.b64encode(img_r.content).decode()
                    image_type = img_r.headers.get("content-type", "image/jpeg")

                user_content = [
                    {
                        "type":      "image_url",
                        "image_url": {"url": f"data:{image_type};base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": f"[{sender_name}]: {prompt}" if prompt else f"[{sender_name}]: ما هذه الصورة؟",
                    },
                ]
            else:
                if not prompt:
                    return JSONResponse({"error": "prompt أو image مطلوب"}, status_code=400)
                user_content = f"[{sender_name}]: {prompt}"

            ctx = await _load(thread_id)

            messages = [
                {"role": "system", "content": SYSTEM},
                *ctx,
                {"role": "user", "content": user_content},
            ]

            r = await _http.post(
                "https://models.inference.ai.azure.com/chat/completions",
                json={
                    "model":       "gpt-4o",
                    "messages":    messages,
                    "temperature": 0.7,
                    "max_tokens":  2048,
                },
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Content-Type":  "application/json",
                },
            )
            if r.status_code == 401:
                return JSONResponse({"error": "GITHUB_MODELS_TOKEN غير صالح"}, status_code=401)
            if r.status_code == 429:
                return JSONResponse({"error": "تجاوزت الحد اليومي لـ GitHub Models"}, status_code=429)
            if r.status_code == 404:
                return JSONResponse({"error": "النموذج غير متاح"}, status_code=404)
            r.raise_for_status()
            data = r.json()

            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not reply:
                return JSONResponse({"error": "استجابة فارغة"}, status_code=502)

            # خزّن النص فقط في السياق (لا الصورة)
            user_text = f"[{sender_name}]: {'[صورة] ' if has_image else ''}{prompt}".strip()
            await _save(thread_id, [
                *ctx,
                {"role": "user",      "content": user_text},
                {"role": "assistant", "content": reply},
            ])

            return JSONResponse({"reply": reply})

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

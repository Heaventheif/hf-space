"""
plugins/cerebras.py
endpoint: POST /cerebras
جلسات جماعية محفوظة في MongoDB
"""
import os, httpx
from datetime import datetime
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "Cerebras GPT OSS — جلسات جماعية"

CEREBRAS_KEY = os.environ.get("CEREBRAS_API_KEY", "")

# ─── Shared HTTP client (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=30,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)
MONGO_URI    = os.environ.get("MONGO_URI", "")

SYSTEM = 'أنت بوت مساعد ذكي اسمك "Sunken". أجب دائماً باللغة العربية بإيجاز (أقل من 300 كلمة). كن ودوداً ومهذباً.'

MODELS = {
    "120b": "gpt-oss-120b",
    "20b":  "gpt-oss-20b",
}
DEFAULT_MODEL = "gpt-oss-120b"

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
        _db = client["sunken"]["cerebras_sessions"]
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

    @app.post("/cerebras")
    async def cerebras(request: Request):
        """
        Body: {
          "thread_id": "group_123",
          "sender_name": "Ahmed",
          "prompt": "سؤالك",
          "model": "120b" | "20b",
          "clear": false
        }
        Response: { "reply": "...", "model": "..." }
        """
        try:
            body        = await request.json()
            thread_id   = body.get("thread_id", "default")
            sender_name = body.get("sender_name", "مستخدم")
            prompt      = body.get("prompt", "").strip()
            model_key   = body.get("model", "120b")
            do_clear    = body.get("clear", False)

            if do_clear:
                await _clear(thread_id)
                return JSONResponse({"reply": "🧹 تم مسح ذاكرة المجموعة."})

            if not prompt:
                return JSONResponse({"error": "prompt مطلوب"}, status_code=400)

            if not CEREBRAS_KEY:
                return JSONResponse({"error": "CEREBRAS_API_KEY غير مضبوط"}, status_code=500)

            model = MODELS.get(model_key, DEFAULT_MODEL)
            ctx   = await _load(thread_id)

            # أضف اسم المرسل للسياق الجماعي
            user_content = f"[{sender_name}]: {prompt}"

            messages = [
                {"role": "system", "content": SYSTEM},
                *ctx,
                {"role": "user", "content": user_content},
            ]

            r = await _http.post(
                "https://api.cerebras.ai/v1/chat/completions",
                json={
                    "model":                  model,
                    "messages":               messages,
                    "max_completion_tokens":  1024,
                    "temperature":            0.7,
                    "stream":                 False,
                },
                headers={
                    "Authorization": f"Bearer {CEREBRAS_KEY}",
                    "Content-Type":  "application/json",
                },
            )
            if r.status_code == 401:
                return JSONResponse({"error": "CEREBRAS_API_KEY غير صالح"}, status_code=401)
            if r.status_code == 429:
                return JSONResponse({"error": "تجاوزت حد الطلبات، انتظر قليلاً"}, status_code=429)
            r.raise_for_status()
            data = r.json()

            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not reply:
                return JSONResponse({"error": "استجابة فارغة"}, status_code=502)

            await _save(thread_id, [
                *ctx,
                {"role": "user",      "content": user_content},
                {"role": "assistant", "content": reply},
            ])

            return JSONResponse({"reply": reply, "model": model})

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

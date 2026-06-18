"""
plugins/tts.py
endpoint: POST /tts
يحوّل النص إلى صوت باستخدام Gemini TTS
مع تدوير عشوائي على 30 صوتاً مدعوماً (بدون تكرار حتى تنتهي الدورة)
"""

import os
import random
import base64
import logging
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

DESCRIPTION = "Gemini TTS — تحويل النص إلى صوت (30 صوتاً)"

# ─── مفاتيح Gemini ────────────────────────────────────────────
GEMINI_KEYS = [k for k in [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
] if k and len(k) > 10]

TTS_MODEL = "gemini-2.5-flash-preview-tts"

# ─── قائمة الأصوات الـ 30 ────────────────────────────────────
ALL_VOICES = [
    "Aoede", "Autonoe", "Puck", "Fenrir", "Algieba", "Despina", "Gacrux",
    "Zephyr", "Callirrhoe", "Charon", "Rasalgethi", "Iapetus", "Erinome",
    "Schedar", "Sulafat", "Vindemiatrix", "Achird", "Leda", "Sadaltager",
    "Adhil", "Alkaid", "Ankaa", "Arneb", "Baten", "Capella", "Castor",
    "Deneb", "Kraz", "Mizar", "Pollux",
]

# Pool في الذاكرة (يُعاد ملؤه عند النفاد)
_voice_pool: list[str] = []

def _next_voice() -> str:
    """تختار صوتاً عشوائياً بدون تكرار حتى تنتهي الدورة كاملة."""
    global _voice_pool
    if not _voice_pool:
        _voice_pool = ALL_VOICES.copy()
        random.shuffle(_voice_pool)
    return _voice_pool.pop(0)


# ─── استدعاء Gemini TTS ──────────────────────────────────────
async def _call_tts(text: str, voice: str) -> bytes:
    """
    يستدعي Gemini TTS ويرجع bytes صوتية خام (PCM/mp3).
    يجرب كل مفاتيح Gemini حتى ينجح.
    """
    from google import genai
    from google.genai import types

    for key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=key)
            response = await client.aio.models.generate_content(
                model=TTS_MODEL,
                contents=text,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice
                            )
                        )
                    ),
                ),
            )
            audio_data = response.candidates[0].content.parts[0].inline_data.data
            if audio_data:
                return audio_data
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                continue
            logger.warning(f"[tts] key failed: {e}")
            continue

    raise RuntimeError("كل مفاتيح Gemini فشلت أو استنفدت حصتها")


# ─── register ────────────────────────────────────────────────
def register(app):

    @app.post("/tts")
    async def tts_endpoint(request: Request):
        """
        Body:
          {
            "text": "النص المراد تحويله",
            "voice": "Aoede"          // اختياري — إذا غاب يُختار تلقائياً
          }

        Response (إذا طُلب base64=true في query):
          { "audio_base64": "...", "voice": "Aoede", "content_type": "audio/mp3" }

        Response (افتراضي):
          ملف صوتي مباشر (audio/mp3)
        """
        try:
            body      = await request.json()
            text      = (body.get("text") or "").strip()
            voice_req = (body.get("voice") or "").strip()
            as_base64 = body.get("base64", False)

            if not text:
                return JSONResponse({"error": "text مطلوب"}, status_code=400)

            if len(text) > 3000:
                return JSONResponse({"error": "النص طويل جداً (3000 حرف كحد أقصى)"}, status_code=400)

            # اختر الصوت
            voice = voice_req if voice_req in ALL_VOICES else _next_voice()
            logger.info(f"[tts] voice={voice} | text_len={len(text)}")

            # استدعي TTS في thread executor (المكتبة sync أحياناً)
            loop = asyncio.get_event_loop()
            audio_bytes = await _call_tts(text, voice)

            if as_base64:
                return JSONResponse({
                    "audio_base64":  base64.b64encode(audio_bytes).decode(),
                    "voice":         voice,
                    "content_type":  "audio/mp3",
                })

            # إرجاع الصوت مباشرة كـ binary
            return Response(
                content=audio_bytes,
                media_type="audio/mp3",
                headers={
                    "X-Voice-Used": voice,
                    "Content-Disposition": f'attachment; filename="tts_{voice}.mp3"',
                },
            )

        except Exception as e:
            logger.error(f"[tts] error: {e}")
            return JSONResponse({"error": str(e)[:200]}, status_code=503)

    @app.get("/tts/voices")
    async def tts_voices():
        """يُرجع قائمة الأصوات المدعومة."""
        return JSONResponse({
            "voices":       ALL_VOICES,
            "total":        len(ALL_VOICES),
            "model":        TTS_MODEL,
            "pool_remaining": len(_voice_pool),
        })

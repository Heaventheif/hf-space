"""
plugins/mood_sticker.py
══════════════════════════════════════════════════════════════════
يصنّف عنوان الأغنية إلى "مزاج/نوع" عبر LLM، ثم يبحث في Giphy عن GIF
مناسب لهذا المزاج، ويُرجع بايتات GIF مباشرة (binary) — لتجنّب أي
طلب HTTP إضافي من جهة Sv2 (Render)، وبالتالي أسرع إيصال ممكن.

Endpoint:
  GET /stickers/mood?title=<عنوان الأغنية>
      → يرجع GIF (binary, image/gif) مباشرة

متغيرات البيئة المطلوبة:
  GIPHY_API_KEY        مفتاح Giphy (إلزامي)
  MOOD_AI_PROVIDER      أحد: groq | sambanova | gemini | cerebras | github
                         (افتراضي: groq)
  GROQ_API_KEY / SAMBANOVA_API_KEY / GEMINI_API_KEY / CEREBRAS_API_KEY / GITHUB_TOKEN
                         حسب المزود المختار
══════════════════════════════════════════════════════════════════
"""

import os
import logging
import httpx
from fastapi import HTTPException, Query
from fastapi.responses import Response

logger = logging.getLogger("mood_sticker")

DESCRIPTION = "يصنّف مزاج الأغنية عبر LLM ويرجع GIF مناسب من Giphy"

DOCKERFILE_DEPS = ["httpx"]

# ─── قائمة موسّعة من الفئات (مزاج + نوع + ثقافة) ─────────────────
MOOD_CATEGORIES = [
    "sad", "happy", "energetic", "chill", "rap", "romantic",
    "asian pop", "korean pop", "japanese", "arabic", "angry",
    "epic", "dark", "dreamy", "party", "slow", "fast", "nostalgic",
    "heartbreak", "love", "motivational", "aggressive", "funny",
    "relaxing", "summer vibe", "winter vibe", "latin", "reggae",
    "rock", "electronic", "lofi",
]

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"

# ─── إعداد مزوّد الذكاء الاصطناعي (قابل للتبديل) ─────────────────
AI_PROVIDER = os.environ.get("MOOD_AI_PROVIDER", "groq").lower()

PROVIDER_CONFIG = {
    "groq": {
        "url":   "https://api.groq.com/openai/v1/chat/completions",
        "key":   os.environ.get("GROQ_API_KEY", ""),
        "model": "llama-3.1-8b-instant",
    },
    "sambanova": {
        "url":   "https://api.sambanova.ai/v1/chat/completions",
        "key":   os.environ.get("SAMBANOVA_API_KEY", ""),
        "model": "Meta-Llama-3.1-8B-Instruct",
    },
    "cerebras": {
        "url":   "https://api.cerebras.ai/v1/chat/completions",
        "key":   os.environ.get("CEREBRAS_API_KEY", ""),
        "model": "llama3.1-8b",
    },
    "gemini": {
        "url":   "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "key":   os.environ.get("GEMINI_API_KEY", ""),
        "model": "gemini-2.5-flash",
    },
    "github": {
        "url":   "https://models.inference.ai.azure.com/chat/completions",
        "key":   os.environ.get("GITHUB_TOKEN", ""),
        "model": "gpt-4o-mini",
    },
}


def _build_prompt(title: str) -> str:
    categories = ", ".join(MOOD_CATEGORIES)
    return (
        f"صنّف عنوان الأغنية التالي إلى فئة واحدة فقط من هذه القائمة: "
        f"{categories}\n\n"
        f"عنوان الأغنية: \"{title}\"\n\n"
        f"أجب بكلمة أو كلمتين فقط (اسم الفئة الإنجليزي من القائمة أعلاه)، "
        f"بدون أي شرح أو علامات ترقيم إضافية."
    )


async def _classify_via_openai_compatible(client: httpx.AsyncClient, cfg: dict, prompt: str) -> str:
    res = await client.post(
        cfg["url"],
        headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 10,
            "temperature": 0.2,
        },
        timeout=12.0,
    )
    res.raise_for_status()
    data = res.json()
    return data["choices"][0]["message"]["content"].strip()


async def _classify_via_gemini(client: httpx.AsyncClient, cfg: dict, prompt: str) -> str:
    res = await client.post(
        f"{cfg['url']}?key={cfg['key']}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 10, "temperature": 0.2},
        },
        timeout=12.0,
    )
    res.raise_for_status()
    data = res.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


async def classify_mood(title: str) -> str:
    """يرجع كلمة/كلمتين تصف مزاج الأغنية. عند أي فشل يرجع 'dance' كقيمة احتياطية."""
    cfg = PROVIDER_CONFIG.get(AI_PROVIDER)
    if not cfg or not cfg["key"]:
        logger.warning("[mood_sticker] لا يوجد مفتاح صالح للمزود %s — استخدام fallback", AI_PROVIDER)
        return "dance"

    prompt = _build_prompt(title)
    try:
        async with httpx.AsyncClient() as client:
            if AI_PROVIDER == "gemini":
                raw = await _classify_via_gemini(client, cfg, prompt)
            else:
                raw = await _classify_via_openai_compatible(client, cfg, prompt)

        mood = raw.lower().strip().strip(".").strip('"')
        # تأكيد أن الفئة معقولة (تطابق تقريبي مع القائمة أو نص قصير)
        if len(mood) > 40 or not mood:
            return "dance"
        return mood
    except Exception as e:
        logger.warning("[mood_sticker] فشل تصنيف المزاج عبر %s: %s", AI_PROVIDER, e)
        return "dance"


async def fetch_giphy_gif(mood: str) -> bytes:
    giphy_key = os.environ.get("GIPHY_API_KEY", "")
    if not giphy_key:
        raise HTTPException(status_code=500, detail="GIPHY_API_KEY غير مضبوط")

    query = f"{mood} dance"

    async with httpx.AsyncClient() as client:
        search_res = await client.get(
            GIPHY_SEARCH_URL,
            params={
                "api_key": giphy_key,
                "q": query,
                "limit": 10,
                "rating": "pg-13",
            },
            timeout=10.0,
        )
        search_res.raise_for_status()
        results = search_res.json().get("data", [])

        if not results:
            raise HTTPException(status_code=404, detail=f"لا نتائج Giphy لـ: {query}")

        import random
        chosen = random.choice(results)
        gif_url = chosen["images"]["downsized"]["url"]

        gif_res = await client.get(gif_url, timeout=15.0)
        gif_res.raise_for_status()
        return gif_res.content


def register(app):

    @app.get("/stickers/mood")
    async def mood_sticker(title: str = Query(..., min_length=1)):
        mood = await classify_mood(title)
        try:
            gif_bytes = await fetch_giphy_gif(mood)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"فشل جلب GIF من Giphy: {e}")

        return Response(
            content=gif_bytes,
            media_type="image/gif",
            headers={"X-Mood-Category": mood},
        )

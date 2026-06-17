"""
plugins/image.py — v5
يستخدم requests مباشرة بدل InferenceClient لتجنب مشكلة Pillow
"""

import os
import base64
import logging
import asyncio
import requests
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
DESCRIPTION = "HuggingFace Inference API — توليد الصور"
HF_TOKEN = os.environ.get("HF_TOKEN", "")

SHORTCUTS = {
    "flux":     "black-forest-labs/FLUX.1-schnell",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
    
    "sdxl":     "stabilityai/sdxl-turbo",
    "sd":       "stabilityai/sdxl-turbo",
}
DEFAULT_MODEL = "flux"

def resolve_model(name: str) -> str:
    key = (name or DEFAULT_MODEL).lower().strip()
    if key in SHORTCUTS: return SHORTCUTS[key]
    if "/" in name: return name
    for k, v in SHORTCUTS.items():
        if key in k: return v
    return SHORTCUTS[DEFAULT_MODEL]

def _generate_sync(model_id: str, prompt: str, width: int, height: int) -> bytes:
    token = HF_TOKEN.strip()
    if not token:
        raise RuntimeError("HF_TOKEN غير موجود")

    url = f"https://router.huggingface.co/hf-inference/models/{model_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": prompt,
        "parameters": {"width": width, "height": height},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=60)

    if r.status_code != 200:
        raise RuntimeError(f"HF خطأ {r.status_code}: {r.text[:200]}")

    if not r.content:
        raise RuntimeError("صورة فارغة")

    return r.content

def register(app):

    @app.post("/image")
    async def image_endpoint(request: Request):
        try:
            body      = await request.json()
            prompt    = (body.get("prompt") or "").strip()
            model_raw = body.get("model", DEFAULT_MODEL)
            width     = int(body.get("width",  1024))
            height    = int(body.get("height", 1024))

            if not prompt:
                return JSONResponse({"error": "prompt مطلوب"}, status_code=400)

            model_id = resolve_model(model_raw)
            logger.info(f"[image] {model_raw} → {model_id} | prompt={prompt[:60]}")

            try:
                loop      = asyncio.get_event_loop()
                img_bytes = await loop.run_in_executor(
                    None, _generate_sync, model_id, prompt, width, height
                )
                img_b64 = base64.b64encode(img_bytes).decode("utf-8")
                return JSONResponse({
                    "image_base64": img_b64,
                    "content_type": "image/jpeg",
                    "model_used":   model_id,
                })
            except Exception as e:
                logger.error(f"[image] error: {e}")
                return JSONResponse({"error": str(e), "model_used": model_id}, status_code=503)

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    @app.get("/image/models")
    async def image_models():
        return JSONResponse({"shortcuts": SHORTCUTS, "default_model": DEFAULT_MODEL})

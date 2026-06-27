"""
plugins/translate.py
endpoint: POST /translate
"""
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

DESCRIPTION = "ترجمة النصوص عبر Google Translate"

# ─── Shared HTTP client (connection pooling) ──────────────────
_http = httpx.AsyncClient(
    timeout=15,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)


def register(app):

    @app.post("/translate")
    async def translate(request: Request):
        """
        Body: { "text": "...", "to": "ar" }
        Response: { "result": "..." }
        """
        try:
            body = await request.json()
            text = body.get("text", "").strip()
            to   = body.get("to", "ar").strip()

            if not text:
                return JSONResponse({"error": "text مطلوب"}, status_code=400)

            url = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl=auto&tl={to}&dt=t&q={httpx.URL(text)}"
            )

            r = await _http.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": to, "dt": "t", "q": text},
            )
            r.raise_for_status()
            data = r.json()

            translated = "".join(
                part[0] for part in data[0] if part[0]
            )
            if not translated:
                return JSONResponse({"error": "استجابة فارغة"}, status_code=502)

            return JSONResponse({"result": translated})

        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

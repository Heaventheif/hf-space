"""
proxy_client.py
- يوجّه أي طلب HTTP من HF عبر Cloudflare Worker لتجنب حظر IP

متغير البيئة المطلوب:
  CF_WORKER_URL — رابط الـ Worker، مثال: https://your-name.workers.dev
"""

import os
import requests
from urllib.parse import quote

CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").rstrip("/")


def _proxied(url: str) -> str:
    if not CF_WORKER_URL:
        raise RuntimeError("CF_WORKER_URL غير مضبوط في متغيرات البيئة.")
    return f"{CF_WORKER_URL}/?url={quote(url, safe='')}"


def proxy_get(url: str, **kwargs) -> requests.Response:
    """GET عبر الـ Worker. باقي الـ kwargs تُمرَّر لـ requests.get (headers, timeout...)."""
    return requests.get(_proxied(url), **kwargs)


def proxy_post(url: str, **kwargs) -> requests.Response:
    """POST عبر الـ Worker. باقي الـ kwargs تُمرَّر لـ requests.post (data, headers, timeout...)."""
    return requests.post(_proxied(url), **kwargs)


def proxy_head(url: str, **kwargs) -> requests.Response:
    """HEAD عبر الـ Worker."""
    return requests.head(_proxied(url), **kwargs)


# ─── مثال استخدام ──────────────────────────────────────────────
if __name__ == "__main__":
    r = proxy_get("https://example.com", timeout=15)
    print(r.status_code, r.text[:200])

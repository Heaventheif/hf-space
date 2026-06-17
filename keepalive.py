"""
keepalive.py
يرسل ping لنفسه كل 30 دقيقة لمنع نوم الـ Space
المتغيرات المطلوبة: SPACE_URL
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

SPACE_URL = os.environ.get("SPACE_URL", "").rstrip("/")


async def keep_alive():
    if not SPACE_URL:
        logger.warning("⚠️  SPACE_URL غير موجود — keepalive معطّل")
        return

    logger.info(f"✅ keepalive يعمل → {SPACE_URL}/health كل 30 دقيقة")
    await asyncio.sleep(60)  # انتظر دقيقة بعد البدء

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{SPACE_URL}/health")
                logger.info(f"💓 ping → {r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️  ping فشل: {e}")

        await asyncio.sleep(1800)  # كل 30 دقيقة

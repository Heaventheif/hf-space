"use strict";

const axios = require("axios");
const fs    = require("fs-extra");
const os    = require("os");
const path  = require("path");

const { sendMoodSticker } = require("../utils/danceSticker.js");

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/125.0.0.0 Safari/537.36",
  "Accept-Language": "en-US,en;q=0.9",
};

// ── كاش client_id: مدة 12 ساعة بدل 6، ونجدد في الخلفية قبل انتهائه ──
let _clientId  = null;
let _clientExp = 0;
let _refreshing = false; // منع التجديد المتزامن

async function fetchClientId() {
  const page = await axios.get("https://soundcloud.com", {
    headers: BROWSER_HEADERS,
    timeout: 15000,
  });

  const scriptUrls = [
    ...page.data.matchAll(/https:\/\/a-v2\.sndcdn\.com\/assets\/[^"]+\.js/g),
  ].map(m => m[0]);

  if (!scriptUrls.length) throw new Error("لم تُوجد سكريبتات SoundCloud");

  for (const url of scriptUrls.slice(-5)) {
    try {
      const script = await axios.get(url, { headers: BROWSER_HEADERS, timeout: 10000 });
      const match  = script.data.match(/client_id:"([a-zA-Z0-9]{20,32})"/);
      if (match) return match[1];
    } catch (_) {}
  }

  throw new Error("فشل استخراج client_id من SoundCloud");
}

async function getClientId() {
  const now = Date.now();

  // إذا الكاش صالح → ارجعه فوراً
  if (_clientId && now < _clientExp) return _clientId;

  // إذا الكاش قارب الانتهاء (أقل من ساعة) → جدد في الخلفية وارجع القديم
  if (_clientId && now < _clientExp + 60 * 60 * 1000 && !_refreshing) {
    _refreshing = true;
    fetchClientId()
      .then(id => {
        _clientId  = id;
        _clientExp = Date.now() + 12 * 60 * 60 * 1000;
      })
      .catch(() => {})
      .finally(() => { _refreshing = false; });
    return _clientId;
  }

  // أول مرة أو انتهى الكاش كلياً → انتظر
  const id = await fetchClientId();
  _clientId  = id;
  _clientExp = Date.now() + 12 * 60 * 60 * 1000;
  return _clientId;
}

// جلب client_id مسبقاً عند تشغيل البوت
getClientId().catch(() => {});

// ── جلب قائمة نتائج ───────────────────────────────────────────
async function searchTracks(query, limit = 7) {
  const client_id = await getClientId();

  const res = await axios.get("https://api-v2.soundcloud.com/search/tracks", {
    params: {
      q: query, client_id, limit,
      offset: 0, linked_partitioning: 1,
      app_version: "1733219585", app_locale: "en",
    },
    headers: BROWSER_HEADERS,
    timeout: 15000,
  });

  const tracks = res.data?.collection;
  if (!tracks?.length) throw new Error("لم تُوجد نتائج على SoundCloud");
  return tracks;
}

// ── تحويل track إلى ملف mp3 ───────────────────────────────────
async function streamTrack(track) {
  const client_id    = await getClientId();
  const transcodings = track.media?.transcodings ?? [];
  if (!transcodings.length) throw new Error("لا يوجد بث متاح لهذا المقطع");

  const pick =
    transcodings.find(t => t.snipped && t.format?.protocol === "progressive") ||
    transcodings.find(t => t.snipped && t.format?.protocol === "hls")         ||
    transcodings.find(t => t.format?.protocol === "progressive")               ||
    transcodings.find(t => t.format?.protocol === "hls")                       ||
    transcodings[0];

  const streamRes = await axios.get(pick.url, {
    params: { client_id, track_authorization: track.track_authorization ?? "" },
    headers: BROWSER_HEADERS,
    timeout: 15000,
  });

  const streamUrl = streamRes.data?.url;
  if (!streamUrl) throw new Error("فشل استخراج رابط البث");

  const filePath = path.join(os.tmpdir(), `sc_${Date.now()}.mp3`);

  // ← تحميل مباشر كـ stream إلى القرص بدل arraybuffer (أسرع للملفات الكبيرة)
  const dlRes = await axios.get(streamUrl, {
    responseType: "stream",
    headers:      BROWSER_HEADERS,
    timeout:      60000,
  });

  await new Promise((resolve, reject) => {
    const writer = fs.createWriteStream(filePath);
    dlRes.data.pipe(writer);
    writer.on("finish", resolve);
    writer.on("error", reject);
  });

  const size = (await fs.stat(filePath)).size;
  if (!size) throw new Error("ملف الصوت فارغ");

  return {
    filePath,
    title:      track.title || "بدون عنوان",
    artist:     track.publisher_metadata?.artist || track.user?.username || "",
    durationMs: track.full_duration || track.duration || 0,
    isSnipped:  !!pick.snipped,
  };
}

function fmtDuration(ms) {
  if (!ms) return "";
  const s = Math.round(ms / 1000), m = Math.floor(s / 60);
  return `⏱ ${m}:${String(s % 60).padStart(2, "0")}`;
}

async function cleanTemp(p) {
  try { await fs.remove(p); } catch (_) {}
}

// ── إرسال مقطع صوت ───────────────────────────────────────────
async function sendTrack(api, threadID, messageID, track, statusMsgId = null) {
  let filePath = null;
  try {
    const result = await streamTrack(track);
    filePath = result.filePath;

    const body =
      `🎵 ${result.title}` +
      `${result.artist     ? `\n👤 ${result.artist}`               : ""}` +
      `${result.durationMs ? `\n${fmtDuration(result.durationMs)}` : ""}` +
      `\n🔊 ${result.isSnipped ? "مقطع Preview 30ث" : "بث كامل"} — SoundCloud`;

    await new Promise((res, rej) =>
      api.sendMessage(
        { body, attachment: fs.createReadStream(filePath) },
        threadID,
        err => err ? rej(err) : res(),
        messageID
      )
    );

    // حذف رسالة الانتظار + ستيكر في الخلفية (لا يحجبان)
    if (statusMsgId) api.unsendMessage(statusMsgId).catch(() => {});
    sendMoodSticker(api, threadID); // fire-and-forget
  } finally {
    if (filePath) cleanTemp(filePath);
  }
}

// ═══════════════════════════════════════════════════════════════
module.exports = {
  config: {
    name:        "sc",
    aliases:     ["بريفيو", "مقطع"],
    version:     "5.1",
    role:        0,
    countDown:   10,
    category:    "media",
    description: "بحث وتشغيل مقاطع من SoundCloud — أضف s لعرض قائمة نتائج",
    guide: { en:
      "{pn} <اسم>       — تشغيل أول نتيجة مباشرة\n" +
      "{pn} s <اسم>     — عرض قائمة نتائج للاختيار"
    },
  },

  onStart: async ({ api, message, args, event }) => {
    const { threadID, messageID } = event;

    if (!args[0]) return message.reply(
      "🎵 SoundCloud\n\n" +
      ".sc <اسم الأغنية>      — تشغيل أول نتيجة مباشرة\n" +
      ".sc s <اسم الأغنية>    — عرض قائمة للاختيار\n\n" +
      "مثال:\n" +
      ".sc after the dark mr kitty\n" +
      ".sc s mr kitty"
    );

    const showList = args[0].toLowerCase() === "s";
    const query    = (showList ? args.slice(1) : args).join(" ").trim();
    if (!query) return message.reply("❌ أرسل اسم الأغنية.");

    try {
      const tracks = await searchTracks(query, showList ? 7 : 1);

      if (showList) {
        let text = `🎵 نتائج البحث في SoundCloud:\n${"─".repeat(22)}\n`;
        tracks.slice(0, 7).forEach((t, i) => {
          const dur = t.full_duration || t.duration || 0;
          text += `${i + 1}. ${t.title || "بدون عنوان"}\n`;
          text += `   👤 ${t.user?.username || ""} ${dur ? fmtDuration(dur) : ""}\n`;
          text += `${"─".repeat(22)}\n`;
        });
        text += `🔢 أرسل رقم الأغنية للتشغيل\n⏳ تنتهي بعد دقيقتين`;

        const sent = await new Promise((res, rej) =>
          api.sendMessage(text, threadID, (err, info) => err ? rej(err) : res(info), messageID)
        );

        if (sent?.messageID && global.Kagenou?.replies) {
          global.Kagenou.replies[sent.messageID] = {
            commandName: "sc",
            author:      event.senderID,
            tracks:      tracks.slice(0, 7),
            statusMsgId: sent.messageID,
            timestamp:   Date.now(),
          };
        }
        return;
      }

      await sendTrack(api, threadID, messageID, tracks[0]);

    } catch (err) {
      console.error("[sc] خطأ:", err.message);
      api.sendMessage(`❌ ${err.message?.substring(0, 200) || "خطأ غير معروف"}`, threadID, null, messageID);
    }
  },

  onReply: async ({ api, event, Reply }) => {
    if (!Reply?.tracks || event.senderID !== Reply.author) return;

    const { threadID, messageID } = event;
    const idx = parseInt(event.body?.trim()) - 1;

    if (isNaN(idx) || idx < 0 || idx >= Reply.tracks.length)
      return api.sendMessage(`❌ أرسل رقماً من 1 إلى ${Reply.tracks.length}`, threadID);

    if (global.Kagenou?.replies?.[Reply.statusMsgId])
      delete global.Kagenou.replies[Reply.statusMsgId];

    const listMsgId = Reply.statusMsgId;
    api.editMessage(`⏳ جارٍ تحميل: ${Reply.tracks[idx].title || ""}...`, listMsgId).catch(() => {});
    try {
      await sendTrack(api, threadID, messageID, Reply.tracks[idx], listMsgId);
    } catch (err) {
      api.sendMessage(`❌ ${err.message?.substring(0, 200)}`, threadID, null, messageID);
    }
  },
};

"use strict";

const axios = require("axios");
const fs    = require("fs-extra");
const os    = require("os");
const path  = require("path");

const API_BASE = "https://ccproject.serv00.net/ytdl2.php";

const { sendMoodSticker } = require("../utils/danceSticker.js");

const EMOJI_PAIRS = [
  ["👍", "❤️"], ["😆", "😮"], ["😢", "😡"],
  ["🥰", "👏"], ["🔥", "💯"], ["😍", "😭"], ["🤔", "👀"],
];

// ── جلب معلومات + رابط التحميل ───────────────────────────────
async function fetchInfo(youtubeUrl, type = "mp3") {
  const res = await axios.get(API_BASE, {
    params: { url: youtubeUrl, type },
    timeout: 30000,
  });
  const data = res.data;
  if (!data || typeof data !== "object")
    throw new Error("استجابة غير متوقعة من الـ API الخارجي");
  const { title, download } = data;
  if (!download) throw new Error(data.error || "لم يُرجع الـ API رابط تحميل");
  return { title: title || "بدون عنوان", downloadUrl: download };
}

// ── تحميل وإرسال ─────────────────────────────────────────────
async function downloadAndSend(api, threadID, messageID, youtubeUrl, wantMp4, statusMsgId = null) {
  const type = wantMp4 ? "mp4" : "mp3";
  let title, downloadUrl;

  try {
    ({ title, downloadUrl } = await fetchInfo(youtubeUrl, type));
  } catch (e) {
    return api.sendMessage(`❌ ${e.response?.data?.error || e.message}`, threadID, null, messageID);
  }

  const filePath = path.join(os.tmpdir(), `ydl_${Date.now()}.${type}`);
  try {
    const res = await axios.get(downloadUrl, {
      responseType: "arraybuffer",
      timeout:      120000,
      maxContentLength: 50 * 1024 * 1024,
    });

    const buffer = Buffer.from(res.data);
    if (buffer.length === 0)      throw new Error("الملف فارغ");
    if (buffer.length > 26214400) throw new Error("الملف أكبر من 25MB");

    await fs.writeFile(filePath, buffer);

    await new Promise((resolve, reject) =>
      api.sendMessage(
        { body: `${wantMp4 ? "🎬" : "🎵"} ${title}`, attachment: fs.createReadStream(filePath) },
        threadID,
        err => err ? reject(err) : resolve(),
        messageID
      )
    );

    if (statusMsgId) { try { await api.unsendMessage(statusMsgId); } catch (_) {} }
    if (!wantMp4) sendMoodSticker(api, threadID); // fire-and-forget

  } catch (e) {
    api.sendMessage(`❌ ${e.response?.data?.error || e.message}`, threadID, null, messageID);
  } finally {
    try { await fs.remove(filePath); } catch (_) {}
  }
}

// ── بحث ──────────────────────────────────────────────────────
async function searchYT(query, limit = 7) {
  const url = `https://yt-dlp-stream.onrender.com/api/v3/q?=${encodeURIComponent(query)}&?=${limit}`;
  const res  = await axios.get(url, { timeout: 25000 });
  const data = res.data;
  if (Array.isArray(data))          return data;
  if (Array.isArray(data?.results)) return data.results;
  if (Array.isArray(data?.data))    return data.data;
  return [];
}

// ── بناء نص القائمة ───────────────────────────────────────────
function buildListText(results, wantMp4) {
  let text = `${wantMp4 ? "🎬" : "🎵"} نتائج البحث:\n${"─".repeat(22)}\n`;
  results.forEach((v, i) => {
    const [mp3E, mp4E] = EMOJI_PAIRS[i];
    text +=
      `${i + 1}. ${v.title}\n` +
      `   ⏱ ${v.duration || "--"}\n` +
      `   ${mp3E} mp3  |  ${mp4E} mp4\n` +
      `${"─".repeat(22)}\n`;
  });
  text += `🔢 رُد بالرقم (مثال: 1 أو 1 mp4)\nأو تفاعل بالإيموجي\n⏳ تنتهي بعد دقيقتين.`;
  return text;
}

// ═══════════════════════════════════════════════════════════════
module.exports = {
  config: {
    name:        "ydl",
    aliases:     ["ytdl2"],
    version:     "2.0",
    role:        0,
    countDown:   15,
    category:    "download",
    description: "تحميل من يوتيوب عبر ccproject — أضف s لعرض قائمة، وmp4 للفيديو",
    guide: { en:
      "{pn} <اسم>           — تحميل أول نتيجة مباشرة (MP3)\n" +
      "{pn} s <اسم>         — عرض قائمة نتائج\n" +
      "{pn} mp4 <اسم>       — تحميل أول نتيجة مباشرة (MP4)\n" +
      "{pn} s mp4 <اسم>     — عرض قائمة نتائج (MP4)\n" +
      "{pn} <رابط>          — تحميل مباشر MP3\n" +
      "{pn} mp4 <رابط>      — تحميل مباشر MP4"
    },
  },

  onStart: async ({ api, message, args, event }) => {
    const { threadID, messageID } = event;

    if (!args[0]) return message.reply(
      "📥 يوتيوب دونلودر\n\n" +
      "🎵 ydl <اسم>         — تحميل مباشر (MP3)\n" +
      "🎬 ydl mp4 <اسم>     — تحميل مباشر (MP4)\n" +
      "📋 ydl s <اسم>       — قائمة نتائج (MP3)\n" +
      "📋 ydl s mp4 <اسم>   — قائمة نتائج (MP4)\n" +
      "🔗 ydl <رابط>        — تحميل مباشر"
    );

    // ── تحليل الوسائط ─────────────────────────────────────────
    let remaining = [...args];
    const showList = remaining[0]?.toLowerCase() === "s";
    if (showList) remaining = remaining.slice(1);

    const wantMp4 = remaining[0]?.toLowerCase() === "mp4";
    if (wantMp4) remaining = remaining.slice(1);

    const query = remaining.join(" ").trim();
    if (!query) return message.reply("❌ أرسل اسم الأغنية أو الرابط.");

    // ── رابط مباشر ────────────────────────────────────────────
    const isUrl = /^https?:\/\//i.test(query);
    if (isUrl) {
      return await downloadAndSend(api, threadID, messageID, query, wantMp4);
    }

    // ── بحث مباشر بدون قائمة ──────────────────────────────────
    if (!showList) {
      try {
        const results = await searchYT(query, 1);
        if (!results.length)
          return api.sendMessage("❌ لم تُعثر على نتائج.", threadID, null, messageID);
        return await downloadAndSend(api, threadID, messageID, results[0].url || results[0].short_url, wantMp4);
      } catch (e) {
        return api.sendMessage(`❌ ${e.message}`, threadID, null, messageID);
      }
    }

    // ── قائمة ────────────────────────────────────────────────
    try {
      const results = await searchYT(query, 7);
      if (!results.length)
        return api.sendMessage("❌ لم تُعثر على نتائج.", threadID, null, messageID);

      const list = results.slice(0, 7);
      const sent = await new Promise((resolve, reject) =>
        api.sendMessage(buildListText(list, wantMp4), threadID,
          (err, info) => err ? reject(err) : resolve(info), messageID)
      );

      if (sent?.messageID) {
        if (global.Kagenou?.replies) {
          global.Kagenou.replies[sent.messageID] = {
            commandName: "ydl",
            author:      event.senderID,
            results:     list,
            wantMp4,
            statusMsgId: sent.messageID,
            timestamp:   Date.now(),
          };
        }

        if (global.client?.reactionListener) {
          global.client.reactionListener[sent.messageID] = {
            author: event.senderID,
            callback: async ({ api, event: re }) => {
              const idx = EMOJI_PAIRS.findIndex(([a, b]) => re.reaction === a || re.reaction === b);
              if (idx === -1 || idx >= list.length) return;

              const wantMp4R = re.reaction === EMOJI_PAIRS[idx][1];
              const chosen   = list[idx];

              delete global.client.reactionListener[sent.messageID];
              if (global.Kagenou?.replies) delete global.Kagenou.replies[sent.messageID];

              try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, sent.messageID); } catch (_) {}
              await downloadAndSend(api, threadID, messageID, chosen.url || chosen.short_url, wantMp4R, sent.messageID);
            },
          };
          setTimeout(() => { delete global.client.reactionListener[sent.messageID]; }, 120000);
        }
      }
    } catch (e) {
      api.sendMessage(`❌ ${e.message}`, threadID, null, messageID);
    }
  },

  onReply: async ({ api, event, Reply }) => {
    if (event.senderID !== Reply.author || !Reply.results) return;

    const { threadID, messageID } = event;
    const parts   = event.body?.trim().split(/\s+/) || [];
    const idx     = parseInt(parts[0]) - 1;
    const wantMp4 = parts[1]?.toLowerCase() === "mp4" ? true
                  : parts[1]?.toLowerCase() === "mp3" ? false
                  : Reply.wantMp4 ?? false;

    if (isNaN(idx) || idx < 0 || idx >= Reply.results.length)
      return api.sendMessage(`❌ أرسل رقماً من 1 إلى ${Reply.results.length}`, threadID);

    const chosen = Reply.results[idx];

    delete global.client?.reactionListener?.[Reply.statusMsgId];
    delete global.Kagenou?.replies?.[Reply.statusMsgId];

    const listMsgId = Reply.statusMsgId;
    try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, listMsgId); } catch (_) {}
    await downloadAndSend(api, threadID, messageID, chosen.url || chosen.short_url, wantMp4, listMsgId);
  },
};

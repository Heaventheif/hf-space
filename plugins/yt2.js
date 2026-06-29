"use strict";

const axios = require("axios");
const fs    = require("fs-extra");
const os    = require("os");
const path  = require("path");

const BASE = "https://yt-dlp-stream.onrender.com/api";

const { sendMoodSticker } = require("../utils/danceSticker.js");

const EMOJI_PAIRS = [
  ["👍", "❤️"], ["😆", "😮"], ["😢", "😡"],
  ["🥰", "👏"], ["🔥", "💯"], ["😍", "😭"], ["🤔", "👀"],
];

// ── تحميل ملف إلى /tmp ────────────────────────────────────────
async function getStream(url) {
  const ext      = url.match(/\.(mp4|mp3|webm|m4a)/i)?.[1] || "mp3";
  const filePath = path.join(os.tmpdir(), `yt2_${Date.now()}.${ext}`);

  const res = await axios.get(url, {
    responseType: "arraybuffer",
    timeout:      120000,
    maxContentLength: 50 * 1024 * 1024,
    maxBodyLength:    50 * 1024 * 1024,
  });

  const buffer = Buffer.from(res.data);
  if (buffer.length === 0)      throw new Error("الملف فارغ.");
  if (buffer.length > 26214400) throw new Error("الملف أكبر من 25MB.");

  await fs.writeFile(filePath, buffer);
  return { stream: fs.createReadStream(filePath), filePath };
}

async function cleanTemp(filePath) {
  try { if (await fs.pathExists(filePath)) await fs.remove(filePath); } catch (_) {}
}

// ── v2 (جلب روابط التحميل) ───────────────────────────────────
async function v2(query) {
  const url = `${BASE}/v2/q?=${encodeURIComponent(query)}`;
  const res = await axios.get(url, { timeout: 30000 });
  const data = res.data;
  if (Array.isArray(data)) return data[0] || {};
  if (!data || typeof data !== "object") return {};
  return data;
}

// ── v3 (بحث) ─────────────────────────────────────────────────
async function v3(query, limit = 7) {
  const url = `${BASE}/v3/q?=${encodeURIComponent(query)}&?=${limit}`;
  const res = await axios.get(url, { timeout: 25000 });
  const data = res.data;
  if (Array.isArray(data))               return { results: data };
  if (!data || typeof data !== "object") return { results: [] };
  if (Array.isArray(data.results))       return data;
  if (Array.isArray(data.data))          return { results: data.data };
  return { results: [] };
}

// ── استخراج روابط من v2 ───────────────────────────────────────
function parse(d) {
  if (!d || typeof d !== "object") return { title: "بدون عنوان", author: "", mp4Url: null, mp3Url: null };
  const m = (d.media && typeof d.media === "object" && !Array.isArray(d.media)) ? d.media : {};
  function getUrl(f) {
    if (!f) return null;
    if (typeof f === "string") return f;
    if (typeof f === "object" && typeof f.url === "string") return f.url;
    return null;
  }
  return {
    title:  d.title  || "بدون عنوان",
    author: d.author || d.channel || "",
    mp4Url: getUrl(m.mp4) || getUrl(d.mp4) || null,
    mp3Url: getUrl(m.mp3) || getUrl(d.mp3) || null,
  };
}

// ── تحميل + إرسال ────────────────────────────────────────────
async function downloadAndSend(api, threadID, messageID, query, wantMp4, statusMsgId = null) {
  const p   = parse(await v2(query));
  const url = wantMp4 ? p.mp4Url : p.mp3Url;

  if (!url)
    return api.sendMessage(`❌ الرابط غير متاح.\n💡 جرّب النوع الآخر.`, threadID, null, messageID);

  let filePath = null;
  try {
    const { stream, filePath: fp } = await getStream(url);
    filePath = fp;

    await new Promise((resolve, reject) =>
      api.sendMessage(
        {
          body:       `${wantMp4 ? "🎬" : "🎵"} ${p.title}\n📺 ${p.author}`.trim(),
          attachment: stream,
        },
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
    if (filePath) await cleanTemp(filePath);
  }
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
  text += `🔢 رُد بالرقم، أو تفاعل بإيموجي (mp3/mp4)\n⏳ تنتهي بعد دقيقتين.`;
  return text;
}

// ═══════════════════════════════════════════════════════════════
module.exports = {
  config: {
    name:        "yt2",
    aliases:     ["يوتيوب2"],
    version:     "5.0",
    role:        0,
    countDown:   15,
    category:    "download",
    description: "تحميل من يوتيوب عبر yt-dlp-stream — أضف s لعرض قائمة، وmp4 للفيديو",
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
      "🎵 yt2 <اسم>         — تحميل مباشر (MP3)\n" +
      "🎬 yt2 mp4 <اسم>     — تحميل مباشر (MP4)\n" +
      "📋 yt2 s <اسم>       — قائمة نتائج (MP3)\n" +
      "📋 yt2 s mp4 <اسم>   — قائمة نتائج (MP4)\n" +
      "🔗 yt2 <رابط>        — تحميل مباشر"
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
        const res = await v3(query, 1);
        if (!res.results?.length)
          return api.sendMessage("❌ لم تُعثر على نتائج.", threadID, null, messageID);
        const first = res.results[0];
        return await downloadAndSend(api, threadID, messageID, first.url || first.short_url, wantMp4);
      } catch (e) {
        return api.sendMessage(`❌ ${e.message}`, threadID, null, messageID);
      }
    }

    // ── قائمة ────────────────────────────────────────────────
    try {
      const res = await v3(query, 7);
      if (!res.results?.length)
        return api.sendMessage("❌ لم تُعثر على نتائج.", threadID, null, messageID);

      const list = res.results.slice(0, 7);
      const sent = await new Promise((resolve, reject) =>
        api.sendMessage(buildListText(list, wantMp4), threadID,
          (err, info) => err ? reject(err) : resolve(info), messageID)
      );

      if (sent?.messageID) {
        if (global.Kagenou?.replies) {
          global.Kagenou.replies[sent.messageID] = {
            commandName: "yt2",
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
              const reaction = re.reaction;
              const idx = EMOJI_PAIRS.findIndex(([mp3, mp4]) => reaction === mp3 || reaction === mp4);
              if (idx === -1 || idx >= list.length) return;

              const wantMp4R = reaction === EMOJI_PAIRS[idx][1];
              const chosen   = list[idx];

              delete global.client.reactionListener[sent.messageID];
              if (global.Kagenou?.replies) delete global.Kagenou.replies[sent.messageID];

              try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, sent.messageID); } catch (_) {}
              await downloadAndSend(api, threadID, messageID, chosen.url || chosen.short_url, wantMp4R, sent.messageID);
            },
          };
          setTimeout(() => {
            delete global.client?.reactionListener?.[sent.messageID];
          }, 120000);
        }
      }
    } catch (e) {
      api.sendMessage(`❌ ${e.response?.data?.error || e.message}`, threadID, null, messageID);
    }
  },

  onReply: async ({ api, event, Reply, message }) => {
    if (event.senderID !== Reply.author || !Reply.results) return;

    const { threadID, messageID } = event;
    const parts   = event.body?.trim().split(/\s+/) || [];
    const idx     = parseInt(parts[0]) - 1;
    const wantMp4 = parts[1]?.toLowerCase() === "mp4"
      ? true
      : parts[1]?.toLowerCase() === "mp3"
        ? false
        : Reply.wantMp4 ?? false;

    if (isNaN(idx) || idx < 0 || idx >= Reply.results.length)
      return message.reply(`❌ أرسل رقماً من 1 إلى ${Reply.results.length}`);

    const chosen = Reply.results[idx];

    delete global.client?.reactionListener?.[Reply.statusMsgId];
    delete global.Kagenou?.replies?.[Reply.statusMsgId];

    const listMsgId = Reply.statusMsgId;
    try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, listMsgId); } catch (_) {}
    await downloadAndSend(api, threadID, messageID, chosen.url || chosen.short_url, wantMp4, listMsgId);
  },
};

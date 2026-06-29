"use strict";

const axios = require("axios");
const fs    = require("fs-extra");
const os    = require("os");
const path  = require("path");

const { sendMoodSticker } = require("../utils/danceSticker.js");

const HF = `http://localhost:${process.env.PORT || 10000}`;

const EMOJI_PAIRS = [
  ["👍", "❤️"], ["😆", "😮"], ["😢", "😡"],
  ["🥰", "👏"], ["🔥", "💯"], ["😍", "😭"],
  ["🤔", "👀"], ["🎉", "🎊"], ["💙", "💜"], ["🌟", "⭐"],
];

// ── بحث ──────────────────────────────────────────────────────
async function ytSearch(query, limit = 10) {
  const { data } = await axios.post(
    `${HF}/yt/search`,
    { query, limit },
    { timeout: 30000, headers: { "Content-Type": "application/json" } }
  );
  if (!data.results?.length) throw new Error("لا توجد نتائج");
  return data.results;
}

// ── تحميل ─────────────────────────────────────────────────────
async function downloadFromHF(ytUrl, wantMp4) {
  const endpoint = wantMp4 ? "/yt/video" : "/yt/audio";
  const ext      = wantMp4 ? "mp4" : "mp3";
  const filePath = path.join(os.tmpdir(), `yt_${Date.now()}.${ext}`);

  const res = await axios.post(
    `${HF}${endpoint}`,
    { url: ytUrl },
    {
      responseType:     "arraybuffer",
      timeout:          5 * 60 * 1000,
      maxContentLength: 45 * 1024 * 1024,
      maxBodyLength:    45 * 1024 * 1024,
      headers: { "Content-Type": "application/json" },
    }
  );

  const ct = res.headers["content-type"] || "";
  if (ct.includes("application/json")) {
    const errText = Buffer.from(res.data).toString();
    let errMsg = "خطأ غير معروف من HF Space";
    try { errMsg = JSON.parse(errText).error || errMsg; } catch (_) {}
    throw new Error(errMsg);
  }

  const buf = Buffer.from(res.data);
  if (!buf.length) throw new Error("الملف فارغ");

  await fs.writeFile(filePath, buf);

  function decodeHeader(h) {
    if (!h) return "";
    try { return decodeURIComponent(h); } catch (_) { return h; }
  }

  return {
    stream:   fs.createReadStream(filePath),
    filePath,
    title:    decodeHeader(res.headers["x-title"])    || "media",
    duration: res.headers["x-duration"]               || "0",
    uploader: decodeHeader(res.headers["x-uploader"]) || "",
  };
}

async function cleanTemp(p) {
  try { if (p && await fs.pathExists(p)) await fs.remove(p); } catch (_) {}
}

// ── تحميل + إرسال ────────────────────────────────────────────
async function downloadAndSend(api, threadID, messageID, ytUrl, wantMp4, statusMsgId = null) {
  let filePath = null;
  try {
    const dl  = await downloadFromHF(ytUrl, wantMp4);
    filePath   = dl.filePath;

    const fmtDur = (sec) => {
      const s = parseInt(sec) || 0;
      if (!s) return "";
      const m = Math.floor(s / 60), ss = s % 60;
      return ` ⏱ ${m}:${String(ss).padStart(2, "0")}`;
    };

    const body =
      `${wantMp4 ? "🎬" : "🎵"} ${dl.title}` +
      `${fmtDur(dl.duration)}` +
      `${dl.uploader ? `\n📺 ${dl.uploader}` : ""}` +
      `\n🎚 ${wantMp4 ? "360p" : "128kbps"}`;

    await new Promise((res, rej) =>
      api.sendMessage(
        { body, attachment: dl.stream },
        threadID,
        err => err ? rej(err) : res(),
        messageID
      )
    );

    if (statusMsgId) { try { await api.unsendMessage(statusMsgId); } catch (_) {} }
    if (!wantMp4) sendMoodSticker(api, threadID); // fire-and-forget

  } catch (err) {
    let msg = err.message || "خطأ غير معروف";
    if (err.response?.data) {
      try {
        const t = Buffer.isBuffer(err.response.data)
          ? err.response.data.toString()
          : JSON.stringify(err.response.data);
        msg = JSON.parse(t).error || msg;
      } catch (_) {}
    }
    api.sendMessage(`❌ ${msg.substring(0, 160)}`, threadID, null, messageID);
  } finally {
    await cleanTemp(filePath);
  }
}

// ── بناء نص القائمة ───────────────────────────────────────────
function buildListText(results, wantMp4) {
  let text = `${wantMp4 ? "🎬" : "🎵"} نتائج البحث:\n${"─".repeat(22)}\n`;
  results.forEach((v, i) => {
    const [mp3E, mp4E] = EMOJI_PAIRS[i];
    text +=
      `${i + 1}. ${v.title}\n` +
      `   ⏱ ${v.duration || "--"}  📺 ${v.uploader || ""}\n` +
      `   ${mp3E} mp3  |  ${mp4E} mp4\n` +
      `${"─".repeat(22)}\n`;
  });
  text += `🔢 رُد بالرقم (مثال: 3 mp4)\nأو تفاعل بالإيموجي\n⏳ تنتهي بعد دقيقتين`;
  return text;
}

// ═══════════════════════════════════════════════════════════════
module.exports = {
  config: {
    name:        "yt",
    aliases:     ["يوتيوب"],
    version:     "6.0",
    role:        0,
    countDown:   15,
    category:    "download",
    description: "تحميل من يوتيوب — أضف s لعرض قائمة، وmp4 للفيديو",
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
      "🎵 yt <اسم>          — تحميل مباشر (MP3)\n" +
      "🎬 yt mp4 <اسم>      — تحميل مباشر (MP4)\n" +
      "📋 yt s <اسم>        — قائمة نتائج (MP3)\n" +
      "📋 yt s mp4 <اسم>    — قائمة نتائج (MP4)\n" +
      "🔗 yt <رابط>         — تحميل مباشر\n\n" +
      "🎚 الجودة: صوت 128kbps | فيديو 360p"
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
        const results = await ytSearch(query, 1);
        return await downloadAndSend(api, threadID, messageID, results[0].url, wantMp4);
      } catch (e) {
        return api.sendMessage(`❌ ${e.message}`, threadID, null, messageID);
      }
    }

    // ── قائمة ────────────────────────────────────────────────
    try {
      const results = await ytSearch(query, 10);
      const list    = results.slice(0, 10);

      const sent = await new Promise((res, rej) =>
        api.sendMessage(buildListText(list, wantMp4), threadID,
          (err, info) => err ? rej(err) : res(info), messageID)
      );

      if (sent?.messageID) {
        if (global.Kagenou?.replies) {
          global.Kagenou.replies[sent.messageID] = {
            commandName: "yt",
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
              const idx = EMOJI_PAIRS.findIndex(([m3, m4]) => reaction === m3 || reaction === m4);
              if (idx < 0 || idx >= list.length) return;

              const wantMp4R = reaction === EMOJI_PAIRS[idx][1];
              const chosen   = list[idx];

              delete global.client.reactionListener[sent.messageID];
              if (global.Kagenou?.replies) delete global.Kagenou.replies[sent.messageID];

              try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, sent.messageID); } catch (_) {}
              await downloadAndSend(api, threadID, messageID, chosen.url, wantMp4R, sent.messageID);
            },
          };
          setTimeout(() => {
            delete global.client?.reactionListener?.[sent.messageID];
          }, 120000);
        }
      }
    } catch (e) {
      api.sendMessage(`❌ ${e.message?.substring(0, 150) || "خطأ في البحث"}`, threadID, null, messageID);
    }
  },

  onReply: async ({ api, event, Reply, message }) => {
    if (!Reply?.results || event.senderID !== Reply.author) return;

    const { threadID, messageID } = event;
    const parts   = event.body?.trim().split(/\s+/) || [];
    const idx     = parseInt(parts[0]) - 1;
    const wantMp4 = parts[1]?.toLowerCase() === "mp4"
      ? true
      : parts[1]?.toLowerCase() === "mp3"
        ? false
        : Reply.wantMp4 ?? false;

    if (isNaN(idx) || idx < 0 || idx >= Reply.results.length)
      return message.reply(`❌ أرسل رقماً من 1 إلى ${Reply.results.length}\nمثال: 3 mp4`);

    const chosen = Reply.results[idx];

    delete global.client?.reactionListener?.[Reply.statusMsgId];
    delete global.Kagenou?.replies?.[Reply.statusMsgId];

    const listMsgId = Reply.statusMsgId;
    try { await api.editMessage(`⏳ جارٍ تحميل: ${chosen.title || ''}...`, listMsgId); } catch (_) {}
    await downloadAndSend(api, threadID, messageID, chosen.url, wantMp4, listMsgId);
  },
};

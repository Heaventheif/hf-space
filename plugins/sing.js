const axios = require('axios');
const fs    = require('fs-extra');
const path  = require('path');
const os    = require('os');

const { sendMoodSticker } = require("../utils/danceSticker.js");

if (!global.soundcloudSearchSessions) global.soundcloudSearchSessions = {};
if (!global.__singCleanupRegistered) {
  global.__singCleanupRegistered = true;
  setInterval(() => {
    const now = Date.now();
    for (const uid in global.soundcloudSearchSessions)
      if (now - global.soundcloudSearchSessions[uid].timestamp > 120000)
        delete global.soundcloudSearchSessions[uid];
  }, 60000);
}

function getApiKey() {
  const keys = [process.env.FERDEV_API_KEY, process.env.FERDEV_API_KEY2, process.env.FERDEV_API_KEY3].filter(Boolean);
  return keys.length === 0 ? "FREE" : keys[Math.floor(Math.random() * keys.length)];
}

function getTempPath() {
  return path.join(os.tmpdir(), `sing_${Date.now()}.mp3`);
}

function react(api, msgID, emoji) {
  try { api.setMessageReaction(emoji, msgID, () => {}, true); } catch (_) {}
}

// ── تحميل وإرسال أغنية ────────────────────────────────────────
async function downloadAndSend(api, threadID, messageID, originMsgID, track, statusMsgId = null) {
  const filePath = getTempPath();
  try {
    const dlRes = await axios.get('https://api.ferdev.my.id/downloader/soundcloud', {
      params: { link: track.url, apikey: getApiKey() },
      timeout: 20000,
    });

    const downloadUrl =
      dlRes.data?.result?.downloadUrl ||
      dlRes.data?.result?.url         ||
      dlRes.data?.result?.download_url;

    if (!downloadUrl) throw new Error("لم يُرجع الـ API رابط تحميل.");

    // ← stream مباشر للقرص بدل arraybuffer (أسرع وأقل ذاكرة)
    const streamRes = await axios({
      url: downloadUrl, method: 'GET', responseType: 'stream',
      timeout: 90000,
    });

    await new Promise((resolve, reject) => {
      const writer = fs.createWriteStream(filePath);
      streamRes.data.pipe(writer);
      writer.on("finish", resolve);
      writer.on("error", reject);
    });

    const size = (await fs.stat(filePath)).size;
    if (!size)           throw new Error("الملف فارغ.");
    if (size > 26214400) throw new Error("الملف أكبر من 25MB.");

    await new Promise((resolve, reject) =>
      api.sendMessage(
        { body: `🎵 ${track.title}`, attachment: fs.createReadStream(filePath) },
        threadID,
        err => err ? reject(err) : resolve(),
        messageID
      )
    );

    if (statusMsgId) api.unsendMessage(statusMsgId).catch(() => {});
    if (originMsgID) react(api, originMsgID, "✅");
    sendMoodSticker(api, threadID); // fire-and-forget

  } catch (error) {
    if (originMsgID) react(api, originMsgID, "❌");
    let msg;
    if (error.message.includes("25MB"))       msg = "⚠️ الملف أكبر من 25MB.";
    else if (error.code === 'ECONNABORTED')   msg = "❌ انتهت مهلة التحميل.";
    else if (error.message.includes("يُرجع")) msg = "❌ فشل الـ API في إرجاع رابط التحميل.";
    else                                      msg = "❌ فشل التحميل، قد يكون المحتوى محمياً.";
    api.sendMessage(msg, threadID, null, messageID);
  } finally {
    fs.remove(filePath).catch(() => {});
  }
}

// ═══════════════════════════════════════════════════════════════
module.exports = {
  config: {
    name:     "sing",
    version:  "5.1.0",
    countDown: 5,
    role:     0,
    description: "بحث وتحميل أغاني من SoundCloud — أضف s لعرض قائمة نتائج",
    category: "media",
    guides:   "sing [اسم] | sing s [اسم]",
  },

  onChat: async function({ api, event, message }) {
    const { threadID, senderID, body, messageID } = event;
    if (!body) return;

    const trimmed = body.trim();
    const lower   = trimmed.toLowerCase();
    const TRIGGERS = ['sing ', 'mp3 ', 'song ', 'اغنية ', 'أغنية '];
    const trigger  = TRIGGERS.find(t => lower.startsWith(t));
    if (!trigger) {
      const session = global.soundcloudSearchSessions[senderID];
      if (session && /^\d+$/.test(lower)) {
        if (Date.now() - session.timestamp > 120000) {
          delete global.soundcloudSearchSessions[senderID];
          return message.reply("⏳ انتهت الجلسة، ابحث مجدداً.");
        }
        const index = parseInt(lower) - 1;
        if (index < 0 || index >= session.results.length)
          return message.reply(`❌ اختر رقماً من 1 إلى ${session.results.length}`);

        const chosenTrack = session.results[index];
        const originMsgID = session.originMsgID;
        delete global.soundcloudSearchSessions[senderID];

        if (originMsgID) react(api, originMsgID, "🤖");
        let stMsgId3 = null;
        try {
          const st3 = await new Promise((res, rej) =>
            api.sendMessage(`⏳ جارٍ تحميل: ${chosenTrack.title || ""}...`,
              threadID, (err, info) => err ? rej(err) : res(info), messageID)
          );
          stMsgId3 = st3?.messageID;
        } catch (_) {}
        await downloadAndSend(api, threadID, messageID, originMsgID, chosenTrack, stMsgId3);
      }
      return;
    }

    const rest      = trimmed.slice(trigger.length).trim();
    const showList  = rest.toLowerCase().startsWith("s ");
    const songName  = showList ? rest.slice(2).trim() : rest;
    if (!songName) return message.reply("❌ مثال: sing shape of you");

    react(api, messageID, "🤖");

    try {
      const res = await axios.get('https://api.ferdev.my.id/search/soundcloud', {
        params: { query: songName, apikey: getApiKey() },
        timeout: 20000,
      });

      const items = res.data?.result || [];
      if (items.length === 0) {
        react(api, messageID, "❌");
        return api.sendMessage("❌ لم يتم العثور على نتائج.", threadID, null, messageID);
      }

      const allTracks = [];
      items.slice(0, 7).forEach(track => {
        const title = track.title || `أغنية ${allTracks.length + 1}`;
        const url   = track.url || track.permalink_url || track.link;
        if (url) allTracks.push({ title, url });
      });

      if (allTracks.length === 0) {
        react(api, messageID, "❌");
        return api.sendMessage("❌ فشل استخراج الروابط.", threadID, null, messageID);
      }

      if (!showList) {
        react(api, messageID, "✅");
        return await downloadAndSend(api, threadID, messageID, messageID, allTracks[0]);
      }

      let msg = `🎵 نتائج البحث:\n${"─".repeat(22)}\n`;
      allTracks.forEach((t, i) => {
        msg += `${i + 1}. 📝 ${t.title}\n${"─".repeat(22)}\n`;
      });
      msg += `🔢 أرسل رقم الأغنية (1-${allTracks.length}) للتحميل.\n⏳ تنتهي بعد دقيقتين.`;

      global.soundcloudSearchSessions[senderID] = {
        results: allTracks, timestamp: Date.now(), originMsgID: messageID,
      };

      api.sendMessage(msg, threadID, null, messageID);
      react(api, messageID, "✅");

    } catch (error) {
      react(api, messageID, "❌");
      if (error.code === 'ECONNABORTED' || error.message.includes('timeout'))
        return api.sendMessage("❌ انتهت مهلة البحث، حاول مرة أخرى.", threadID, null, messageID);
      api.sendMessage("❌ خطأ أثناء البحث.", threadID, null, messageID);
    }
  },
};

const { callAiJson, disabledMessage, isEnabled, json } = require("./api_common");

function parseDay(text) {
  const s = String(text || "");
  const zh = s.match(/(?:\u5468|\u661f\u671f|\u793c\u62dc)\s*([\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u59291234567])/u);
  if (zh) {
    const token = zh[1];
    if (token === "\u516d" || token === "6") return { day: "sat", unsupported: true };
    if (token === "\u65e5" || token === "\u5929" || token === "7") return { day: "sun", unsupported: true };
    return { day: ({ "\u4e00": "mon", "1": "mon", "\u4e8c": "tue", "2": "tue", "\u4e09": "wed", "3": "wed", "\u56db": "thu", "4": "thu", "\u4e94": "fri", "5": "fri" }[token] || ""), unsupported: false };
  }
  const en = s.match(/\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b/i);
  if (!en) return { day: "", unsupported: false };
  const token = en[1].slice(0, 3).toLowerCase();
  const day = ({ mon: "mon", tue: "tue", wed: "wed", thu: "thu", fri: "fri", sat: "sat", sun: "sun" }[token] || "");
  return { day, unsupported: day === "sat" || day === "sun" };
}

function cnHour(raw) {
  const s = String(raw || "").trim();
  const d = { "\u96f6": 0, "\u4e00": 1, "\u4e8c": 2, "\u4e24": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5, "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9 };
  if (Object.prototype.hasOwnProperty.call(d, s)) return d[s];
  if (s === "\u5341") return 10;
  if (s.startsWith("\u5341")) return 10 + (d[s.slice(1)] || 0);
  const m = s.match(/^([\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d])\u5341([\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d])?$/u);
  if (m) return (d[m[1]] || 0) * 10 + (m[2] ? d[m[2]] || 0 : 0);
  return null;
}

function period(text) {
  const s = String(text || "");
  if (/\u4e0a\u5348|morning/i.test(s)) return "morning";
  if (/\u4e2d\u5348|noon/i.test(s)) return "noon";
  if (/\u4e0b\u5348|afternoon/i.test(s)) return "afternoon";
  if (/\u665a\u4e0a|\u508d\u665a|evening|night/i.test(s)) return "evening";
  return "";
}

function adjust(hm, p) {
  if (!hm || (p !== "afternoon" && p !== "evening")) return hm;
  const [h, m] = hm.split(":").map(Number);
  if (!Number.isFinite(h) || h >= 12) return hm;
  return `${String(h + 12).padStart(2, "0")}:${String(m || 0).padStart(2, "0")}`;
}

function parseHm(raw, p = "") {
  const t = String(raw || "").trim().replace(/\s+/g, "").replace(/\uff1a/g, ":").replace(/\./g, ":");
  let m = t.match(/^(\d{1,2})(?::(\d{1,2}))?$/);
  if (m) return adjust(`${String(Number(m[1])).padStart(2, "0")}:${String(m[2] == null ? 0 : Number(m[2])).padStart(2, "0")}`, p);
  m = t.match(/^(\d{1,2})\u70b9(?:(\d{1,2})\u5206?)?$/u);
  if (m) return adjust(`${String(Number(m[1])).padStart(2, "0")}:${String(m[2] == null ? 0 : Number(m[2])).padStart(2, "0")}`, p);
  m = t.match(/^([\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})\u70b9(?:(\d{1,2})\u5206?)?$/u);
  if (!m) return "";
  const h = cnHour(m[1]);
  const minute = m[2] == null ? 0 : Number(m[2]);
  if (!Number.isFinite(h) || !Number.isFinite(minute)) return "";
  return adjust(`${String(h).padStart(2, "0")}:${String(minute).padStart(2, "0")}`, p);
}

function parseTimeRange(text) {
  const s = String(text || "");
  const p = period(s);
  const token = "(?:\\d{1,2}(?:[:\\uff1a.]\\d{1,2}|\\u70b9(?:\\d{1,2})?(?:\\u5206)?)?|[\\u96f6\\u4e00\\u4e8c\\u4e24\\u4e09\\u56db\\u4e94\\u516d\\u4e03\\u516b\\u4e5d\\u5341]{1,3}\\u70b9(?:\\d{1,2})?(?:\\u5206)?)";
  const m = s.match(new RegExp(`(${token})\\s*(?:\\u5230|\\u81f3|~|\\uFF5E|-|\\u2014|\\u2013)\\s*(${token})`));
  if (!m) return { start: "", end: "", raw: "", period: p };
  return { start: parseHm(m[1], p), end: parseHm(m[2], p), raw: m[0] || "", period: p };
}

function actionOf(text) {
  const s = String(text || "");
  if (/\u5220\u9664|\u5220\u6389|\u79fb\u9664|\u53d6\u6d88|remove|delete|cancel/i.test(s)) return "delete";
  if (/\u4fee\u6539|\u8c03\u6574|\u6539\u5230|\u6539\u6210|\u6539\u4e3a|\u53d8\u66f4|\u66f4\u65b0|reschedule|change|move|update/i.test(s)) return "update";
  if (/\u52a0|\u65b0\u589e|\u6dfb\u52a0|\u5b89\u6392|\u521b\u5efa|\u52a0\u5165|add|create|schedule/i.test(s)) return "add";
  return "";
}

function targetOf(text) {
  const s = String(text || "");
  if (/[A-Za-z]{2,}\d{2,}[A-Za-z0-9]*/.test(s) || /\u8bfe\u7a0b\u8868|\u8bfe\u8868|\u8bfe\u7a0b|\u4e0a\u8bfe|\u6559\u5ba4|\u8001\u5e08|lecture|class|course|timetable|room|teacher|lec|lab|prac/i.test(s)) return "course";
  if (/\u6d3b\u52a8|\u884c\u7a0b|\u4e8b\u9879|\u4f1a\u8bae|event|activity|meeting|appointment/i.test(s)) return "activity";
  return "";
}

function cleanName(text, target, rawTime) {
  let s = String(text || "");
  if (rawTime) s = s.replace(rawTime, " ");
  s = s
    .replace(/(?:\u5468|\u661f\u671f|\u793c\u62dc)\s*[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u59291234567]/gu, " ")
    .replace(/\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b/gi, " ")
    .replace(/(?:\u4e2a\u4eba\u65e5\u7a0b|\u65e5\u7a0b\u8868|\u65e5\u7a0b|\u884c\u7a0b|\u8bfe\u7a0b\u8868|\u8bfe\u8868|calendar|personal schedule|course schedule|class schedule|timetable)/gi, " ")
    .replace(/(?:\u5e2e\u6211|\u8bf7|\u4e00\u4e0b|\u4e00\u4e2a|\u4e00\u6761|\u4e00\u95e8|\u4e00\u8282|\u4e2a|\u95e8|\u8282)/gu, " ")
    .replace(/(?:\u52a0|\u65b0\u589e|\u6dfb\u52a0|\u5b89\u6392|\u521b\u5efa|\u52a0\u5165|\u5220\u9664|\u5220\u6389|\u79fb\u9664|\u53d6\u6d88|\u4fee\u6539|\u8c03\u6574|\u6539\u5230|\u6539\u6210|\u6539\u4e3a|\u53d8\u66f4|\u66f4\u65b0|add|create|remove|delete|cancel|reschedule|change|move|update|schedule)/gi, " ");
  if (target === "course") s = s.replace(/(?:\u8bfe\u7a0b|\u8bfe|\u4e0a\u8bfe|lecture|class|course|lec|lab|prac)/gi, " ");
  else s = s.replace(/(?:\u6d3b\u52a8|\u4e8b\u9879|\u4f1a\u8bae|event|activity|meeting|appointment)/gi, " ");
  return s.replace(/[\uff0c,\u3002\uff1b;]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
}

function fallback(text, context = []) {
  const s = String(text || "").trim();
  if (!s) return { handled: false, action: "none", target: null, matchName: "", item: {}, confidence: 0 };
  const action = actionOf(s) || "open";
  const target = targetOf(s) || "";
  const day = parseDay(s);
  const range = parseTimeRange(s);
  const scheduleWords = /(?:\u4e2a\u4eba\u65e5\u7a0b|\u6211\u7684\u65e5\u7a0b|\u65e5\u7a0b\u8868|\u8bfe\u7a0b\u8868|\u8bfe\u8868|personal schedule|course schedule|class schedule|timetable)/i.test(s);
  const recentSchedule = Array.isArray(context) && context.slice(-3).some((x) => /schedule|\u65e5\u7a0b|\u8bfe\u7a0b|\u8bfe\u8868/i.test(String(x && x.text || "")));
  if (!scheduleWords && !target && !(recentSchedule && action !== "open")) {
    return { handled: false, action: "none", target: null, matchName: "", item: {}, confidence: 0 };
  }
  const item = {};
  if (day.day) item.day = day.day;
  if (range.start) item.start = range.start;
  if (range.end) item.end = range.end;
  if (range.period) item.period = range.period;
  if (action === "add") item.name = cleanName(s, target || "activity", range.raw);
  const matchName = action === "delete" || action === "update" ? cleanName(s, target || "activity", range.raw) : "";
  return { handled: true, action, target: target || null, matchName, item, confidence: action === "open" ? 0.82 : 0.68 };
}

function sanitize(data, fb) {
  const src = data && typeof data === "object" ? data : {};
  const item = src.item && typeof src.item === "object" ? { ...src.item } : {};
  const action = ["add", "update", "delete", "open", "none"].includes(String(src.action || "").toLowerCase()) ? String(src.action).toLowerCase() : fb.action;
  const targetRaw = String(src.target || "").toLowerCase();
  const target = targetRaw === "course" || targetRaw === "activity" ? targetRaw : fb.target;
  return {
    handled: Boolean(src.handled ?? fb.handled),
    action,
    target,
    matchName: String(src.matchName || src.match_name || fb.matchName || "").trim(),
    item: Object.keys(item).length ? item : fb.item,
    confidence: Math.max(0, Math.min(Number(src.confidence ?? fb.confidence ?? 0), 1))
  };
}

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  if (!isEnabled()) return json(403, { error: disabledMessage() });

  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const text = String(body.text || "").trim();
    const fb = fallback(text, body.context);
    if (!text || !fb.handled) return json(200, fb);

    const system = [
      "Parse a local Personal Schedule edit command. Output strict JSON only.",
      "Schema: {\"handled\":true,\"action\":\"add|update|delete|open|none\",\"target\":\"course|activity\",\"matchName\":\"\",\"item\":{\"day\":\"mon|tue|wed|thu|fri\",\"name\":\"\",\"start\":\"HH:MM\",\"end\":\"HH:MM\",\"period\":\"morning|noon|afternoon|evening\",\"location\":\"\",\"teacher\":\"\",\"intro\":\"\"},\"confidence\":0.0}",
      "Use currentSchedule as the authoritative list. If the user says course, class, timetable, lecture, lab, or includes a course-code-like token, target must be course."
    ].join("\n");
    const payload = JSON.stringify({ text, recentContext: body.context || [], currentSchedule: body.schedule || {} });
    const parsed = await callAiJson(system, payload).catch(() => null);
    return json(200, sanitize(parsed, fb));
  } catch (e) {
    return json(200, { handled: false, action: "none", target: null, matchName: "", item: {}, confidence: 0 });
  }
};

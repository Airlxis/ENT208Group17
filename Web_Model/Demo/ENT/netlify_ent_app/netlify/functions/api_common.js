const fs = require("fs");
const path = require("path");

function json(statusCode, data, headers = {}) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
    body: JSON.stringify(data)
  };
}

function isEnabled() {
  return String(process.env.APP_ENABLED || "true").toLowerCase() === "true";
}

function disabledMessage() {
  return process.env.APP_DISABLED_MESSAGE || "服务暂停，请稍后再试";
}

function readJson(name) {
  try {
    const p = path.join(__dirname, "data", name);
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (e) {
    return [];
  }
}

const DATASETS = {
  all: "qa.json",
  study: "qa_study.json",
  intern: "qa_intern.json",
  campus: "qa_campus.json",
  life: "qa_life.json",
  research: "qa_research.json"
};

function normalize(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function tokens(text) {
  const t = normalize(text);
  if (!t) return [];
  const parts = t.split(/\s+/).filter(Boolean);
  const chars = Array.from(t.replace(/\s+/g, ""));
  return Array.from(new Set([...parts, ...chars]));
}

function scoreItem(query, item) {
  const qTokens = tokens(query);
  if (!qTokens.length) return 0;
  const haystack = normalize(`${item.q || ""} ${item.a || ""}`);
  if (!haystack) return 0;
  let score = 0;
  for (const t of qTokens) {
    if (t.length > 1 && haystack.includes(t)) score += 2;
    else if (t.length === 1 && haystack.includes(t)) score += 0.4;
  }
  if (haystack.includes(normalize(query))) score += 8;
  return score;
}

function iconFor(text) {
  const t = String(text || "").toLowerCase();
  if (/简历|resume|面试|interview|投递|offer|实习|就业/.test(t)) return "briefcase";
  if (/活动|社团|志愿|讲座|比赛|校园|迎新|报到|注册/.test(t)) return "calendar";
  if (/宿舍|报修|饮食|吃饭|食堂|医疗|emergency|生活/.test(t)) return "home";
  if (/论文|科研|暑研|实验|文献|研究/.test(t)) return "search";
  return "book";
}

function toCard(item) {
  const q = String(item && item.q ? item.q : "").trim();
  const a = String(item && item.a ? item.a : "").trim();
  return {
    title: q.replace(/[？?]\s*$/g, "") || "推荐内容",
    desc: a.split(/\r?\n/)[0].slice(0, 120),
    icon: iconFor(`${q} ${a}`),
    action: q,
    details: a
  };
}

function loadQa(key) {
  return readJson(DATASETS[key] || DATASETS.all).filter((x) => x && (x.q || x.a));
}

function topMatches(key, query, limit = 8) {
  const rows = loadQa(key);
  const scored = rows
    .map((item, idx) => ({ item, idx, score: query ? scoreItem(query, item) : rows.length - idx }))
    .filter((x) => !query || x.score > 0)
    .sort((a, b) => b.score - a.score || a.idx - b.idx)
    .slice(0, limit)
    .map((x) => toCard(x.item));
  if (scored.length) return scored;
  return rows.slice(0, limit).map(toCard);
}

function routeFor(text) {
  const t = String(text || "");
  if (/(?:\u4e2a\u4eba\u65e5\u7a0b|\u6211\u7684\u65e5\u7a0b|\u65e5\u7a0b\u8868|\u65e5\u7a0b\u4fee\u6539|\u4fee\u6539\u65e5\u7a0b|\u8bfe\u7a0b\u8868|\u8bfe\u8868|\u4e2a\u4eba\u8bfe\u8868|\u6211\u7684\u8bfe\u8868|personal schedule|my schedule|course schedule|class schedule|timetable)/i.test(t)) {
    return { moduleKey: "schedule", pageKey: null, confidence: 0.82 };
  }
  if (/宿舍|报修|吃饭|食堂|医疗|生活费|紧急|生病|医保/.test(t)) {
    return { moduleKey: "life", pageKey: null, confidence: 0.62 };
  }
  if (/简历|面试|实习|投递|内推|offer|秋招|春招|就业/.test(t)) {
    return { moduleKey: "intern", pageKey: null, confidence: 0.62 };
  }
  if (/社团|活动|志愿|讲座|比赛|迎新|校园活动|报到|开学|新生|注册/.test(t)) {
    return { moduleKey: "campus", pageKey: null, confidence: 0.62 };
  }
  if (/论文|实验|科研|暑研|套磁|文献|投稿|研究/.test(t)) {
    return { moduleKey: "research", pageKey: null, confidence: 0.62 };
  }
  if (/选课|课程|考试|复习|英语|雅思|托福|写作|口语|学习/.test(t)) {
    return { moduleKey: "study", pageKey: null, confidence: 0.62 };
  }
  return { moduleKey: null, pageKey: null, confidence: 0 };
}

function normalizeLanguage(language) {
  return String(language || "").toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
}

function sanitizeContext(context, maxItems = 3) {
  const rows = Array.isArray(context) ? context : [];
  return rows
    .filter((x) => x && (x.role === "user" || x.role === "assistant") && String(x.text || "").trim())
    .slice(-maxItems)
    .map((x) => ({
      role: x.role === "assistant" ? "assistant" : "user",
      text: String(x.text || "").trim().slice(0, 600)
    }));
}

function formatContextNote(context, language) {
  const items = sanitizeContext(context);
  if (!items.length) return "";
  const en = normalizeLanguage(language) === "en-US";
  const label = (role) => en ? (role === "assistant" ? "Assistant" : "User") : (role === "assistant" ? "助手" : "用户");
  const lines = items.map((x, i) => `${i + 1}. ${label(x.role)}：${x.text}`).join("\n");
  return en
    ? `Simple recent context, at most three short messages. Use it only to resolve references such as "it", "that one", or "delete it"; do not invent details not shown here.\n${lines}`
    : `简单前后文，最多三句短消息。只用于理解“它/刚才那个/删了”等省略指代，不要编造这里没有的信息。\n${lines}`;
}

function buildPrompt(message, language = "zh-CN", context = []) {
  const lang = normalizeLanguage(language);
  const en = lang === "en-US";
  const route = routeFor(message);
  const cleanMessage = String(message || "").trim();
  const contextDependent = /(?:\u521a\u624d|\u4e4b\u524d|\u4e0a\u4e00\u4e2a|\u8fd9\u4e2a|\u90a3\u4e2a|\u5b83|\u5b83\u7684|\u4e0a\u4e0b\u6587|\u8bb0\u4f4f|\u5220\u4e86|\u53d6\u6d88\u5b83|\b(?:previous|earlier|that one|it|context|remember)\b)/i.test(cleanMessage);
  const shouldUseRefs = !contextDependent && (cleanMessage.length >= 3 || /[A-Za-z0-9]{3,}/.test(cleanMessage));
  const refs = shouldUseRefs ? topMatches(route.moduleKey || "all", message, 3) : [];
  const refText = refs
    .map((r, i) => `${i + 1}. ${r.action}\n${r.details}`)
    .join("\n\n");
  return [
    en
      ? "You are XJTLU Life Assistant, a practical assistant for Xi'an Jiaotong-Liverpool University students."
      : "你是“西浦生活助手”，一名面向西交利物浦大学（XJTLU）学生的智能助手，主要帮助新生和在校生解决与西浦学习、生活相关的问题。",
    en
      ? "Do not use emoji, kaomoji, or decorative symbols."
      : "严禁使用表情符号、颜文字、特殊符号，不要输出任何 emoji。",
    en
      ? "Answer in the same language as the user. Be clear and useful: do not over-compress the answer or omit key steps just to be brief."
      : "回答要求：使用自然、清晰、信息足够的简体中文；不要为了简短而省略关键步骤，也不要只给一句过度压缩的结论。",
    en
      ? "For uncertain or changeable university rules, say that the student should follow the latest official XJTLU notice."
      : "对不确定或可能变动的校内规定，提醒以学校官方最新通知为准。",
    formatContextNote(context, lang),
    refText
      ? (en
        ? `Internal reference information follows. Synthesize it in your own words and do not copy it verbatim:\n${refText}`
        : `下面是内部参考信息，请综合后用自己的话回答，不要逐字复读：\n${refText}`)
      : ""
  ].filter(Boolean).join("\n\n");
}

function buildModelMessages({ message, language = "zh-CN", context = [] }) {
  return [
    { role: "system", content: buildPrompt(message, language, context) },
    ...sanitizeContext(context).map((x) => ({ role: x.role, content: x.text })),
    { role: "user", content: message }
  ];
}

async function callChatCompletions({ apiKey, model, baseUrl, provider, message, language, context }) {
  if (!apiKey) return "";
  const resp = await fetch(`${baseUrl.replace(/\/+$/g, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`
    },
    body: JSON.stringify({
      model,
      messages: buildModelMessages({ message, language, context }),
      temperature: 0.4
    })
  });
  if (!resp.ok) throw new Error(`${provider} error ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  return String(data?.choices?.[0]?.message?.content || "").trim();
}

function getDeepSeekConfig() {
  const enabled = String(process.env.DEEPSEEK_ENABLED || "false").toLowerCase() === "true";
  if (!enabled || !process.env.DEEPSEEK_API_KEY) return null;
  return {
    apiKey: process.env.DEEPSEEK_API_KEY,
    model: process.env.DEEPSEEK_MODEL || "deepseek-chat",
    baseUrl: process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/v1",
    provider: "DeepSeek"
  };
}

function getOpenAIConfig() {
  const enabled = String(process.env.OPENAI_ENABLED || "true").toLowerCase() === "true";
  if (!enabled || !process.env.OPENAI_API_KEY) return null;
  return {
    apiKey: process.env.OPENAI_API_KEY,
    model: process.env.OPENAI_MODEL || "gpt-4o-mini",
    baseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1",
    provider: "OpenAI"
  };
}

function getPreferredModelConfig() {
  return getDeepSeekConfig() || getOpenAIConfig();
}

async function callDeepSeek(message, language, context) {
  const config = getDeepSeekConfig();
  if (!config) return "";
  return callChatCompletions({
    ...config,
    message,
    language,
    context
  });
}

async function callOpenAI(message, language, context) {
  const config = getOpenAIConfig();
  if (!config) return "";
  return callChatCompletions({
    ...config,
    message,
    language,
    context
  });
}

async function callAiJson(systemPrompt, userPrompt) {
  const config = getPreferredModelConfig();
  if (!config) return null;
  const resp = await fetch(`${config.baseUrl.replace(/\/+$/g, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.apiKey}`
    },
    body: JSON.stringify({
      model: config.model,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userPrompt }
      ],
      temperature: 0.25
    })
  });
  if (!resp.ok) throw new Error(`${config.provider} error ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  const raw = String(data?.choices?.[0]?.message?.content || "").trim();
  const match = raw.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
  if (!match) return null;
  return JSON.parse(match[1]);
}

async function answer(message, language = "zh-CN", context = []) {
  const lang = normalizeLanguage(language);
  try {
    const deepseek = await callDeepSeek(message, lang, context);
    if (deepseek) return deepseek;

    const ai = await callOpenAI(message, lang, context);
    if (ai) return ai;
  } catch (e) {
    // Fall back to local QA below.
  }

  const route = routeFor(message);
  const key = route.moduleKey || "all";
  const refs = topMatches(key, message, 2);
  if (lang === "en-US") {
    return [
      `I received your question: ${message}`,
      "",
      refs.length
        ? "I found related local reference material, but the cloud model is currently unavailable, so I cannot safely rewrite the full answer into English right now."
        : "The cloud model is currently unavailable, so I can only return this local fallback message.",
      "For university rules or time-sensitive details, please follow the latest official XJTLU notice."
    ].join("\n");
  }
  if (refs.length) {
    const first = refs[0];
    return [
      first.details || first.desc || "我找到了相关的西浦生活助手参考内容。",
      "",
      "提示：当前未配置或暂时无法使用云端模型，所以以上是基于本地 QA 数据的兜底回复。具体以学校官方最新通知为准。"
    ].join("\n");
  }

  return [
    `已收到你的问题：${message}`,
    "",
    "当前未配置或暂时无法使用云端模型，因此返回本地兜底回复。具体以学校官方最新通知为准。"
  ].join("\n");
}

module.exports = {
  answer,
  buildModelMessages,
  buildPrompt,
  callAiJson,
  disabledMessage,
  getPreferredModelConfig,
  isEnabled,
  json,
  routeFor,
  topMatches,
  toCard
};

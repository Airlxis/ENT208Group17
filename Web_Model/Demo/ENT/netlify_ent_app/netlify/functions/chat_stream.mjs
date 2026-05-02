import apiCommon from "./api_common.js";

const { buildModelMessages } = apiCommon;

const encoder = new TextEncoder();

function encodeSse(data) {
  return encoder.encode(`data: ${JSON.stringify(data)}\n\n`);
}

function textSse(data) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

function encodeDone() {
  return encoder.encode("data: [DONE]\n\n");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isEnabled() {
  return String(process.env.APP_ENABLED || "true").toLowerCase() === "true";
}

function disabledMessage() {
  return process.env.APP_DISABLED_MESSAGE || "服务暂停，请稍后再试";
}

function getPreferredModelConfig() {
  const deepSeekEnabled = String(process.env.DEEPSEEK_ENABLED || "false").toLowerCase() === "true";
  if (deepSeekEnabled && process.env.DEEPSEEK_API_KEY) {
    return {
      apiKey: process.env.DEEPSEEK_API_KEY,
      model: process.env.DEEPSEEK_MODEL || "deepseek-chat",
      baseUrl: process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/v1",
      provider: "DeepSeek"
    };
  }

  const openAiEnabled = String(process.env.OPENAI_ENABLED || "true").toLowerCase() === "true";
  if (openAiEnabled && process.env.OPENAI_API_KEY) {
    return {
      apiKey: process.env.OPENAI_API_KEY,
      model: process.env.OPENAI_MODEL || "gpt-4o-mini",
      baseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1",
      provider: "OpenAI"
    };
  }

  return null;
}

async function streamFallback(controller, message, language = "zh-CN") {
  const en = String(language || "").toLowerCase().startsWith("en");
  const reply = en
    ? [
      `I received your question: ${message}`,
      "",
      "The cloud model is currently unavailable, so I can only return this local fallback message.",
      "For university rules or time-sensitive details, please follow the latest official XJTLU notice."
    ].join("\n")
    : [
      `已收到你的问题：${message}`,
      "",
      "当前未配置或暂时无法使用云端模型，因此返回本地兜底回复。具体以学校官方最新通知为准。"
    ].join("\n");
  for (const ch of Array.from(reply)) {
    controller.enqueue(encodeSse({ delta: ch }));
    await sleep(12);
  }
}

async function streamModel(controller, message, config, language, context) {
  const resp = await fetch(`${config.baseUrl.replace(/\/+$/g, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.apiKey}`
    },
    body: JSON.stringify({
      model: config.model,
      messages: buildModelMessages({ message, language, context }),
      temperature: 0.4,
      stream: true
    })
  });

  if (!resp.ok || !resp.body) {
    throw new Error(`${config.provider} stream error ${resp.status}: ${await resp.text()}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const events = buffer.split("\n\n");
    buffer = events.pop() || "";

    for (const event of events) {
      const lines = event.split("\n");
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;

        try {
          const obj = JSON.parse(payload);
          const delta = obj?.choices?.[0]?.delta?.content || "";
          if (delta) controller.enqueue(encodeSse({ delta }));
        } catch (e) {
          // Ignore malformed upstream SSE fragments and continue streaming.
        }
      }
    }
  }
}

export default async function handler(req) {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  if (!isEnabled()) {
    return new Response(textSse({ error: disabledMessage() }) + "data: [DONE]\n\n", {
      status: 403,
      headers: { "Content-Type": "text/event-stream; charset=utf-8" }
    });
  }

  let message = "";
  let language = "zh-CN";
  let context = [];
  try {
    const body = await req.json();
    message = String(body.message || "").trim();
    language = String(body.language || "zh-CN");
    context = Array.isArray(body.context) ? body.context : [];
  } catch (e) {
    message = "";
  }

  if (!message) {
    return new Response(textSse({ error: "message 不能为空" }) + "data: [DONE]\n\n", {
      status: 400,
      headers: { "Content-Type": "text/event-stream; charset=utf-8" }
    });
  }

  const stream = new ReadableStream({
    async start(controller) {
      try {
        const config = getPreferredModelConfig();
        if (config) await streamModel(controller, message, config, language, context);
        else await streamFallback(controller, message, language);
      } catch (e) {
        try {
          await streamFallback(controller, message, language);
        } catch (fallbackError) {
          controller.enqueue(encodeSse({ error: fallbackError?.message || e?.message || "服务异常" }));
        }
      } finally {
        controller.enqueue(encodeDone());
        controller.close();
      }
    }
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no"
    }
  });
}

const { callAiJson, json } = require("./api_common");

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const q = String(body.q || "").replace(/[？?]\s*$/g, "").trim();
    const a = String(body.a || "").trim();
    try {
      const rewritten = await callAiJson(
        [
          "你是校园生活助手的推荐内容改写器。",
          "请把一条 QA 命中改写成适合右侧栏详情页展示的内容。",
          "要求：礼貌但简洁，全部用陈述句。",
          "强约束：不要复制 q 原句；不要出现“问/答/Q/A/？”；不要输出方括号列表原样。",
          "title 是陈述句标题，必须包含明确主语，信息完整但简洁。",
          "desc 是 1 句具体描述。",
          "details 是 4 到 8 条要点组成的数组；如果是流程，按顺序输出，每条都能独立执行。",
          "只输出严格 JSON：{\"title\":\"...\",\"desc\":\"...\",\"details\":[\"...\"],\"icon\":\"book\"}"
        ].join("\n"),
        JSON.stringify({
          contextKey: body.contextKey || "",
          q,
          a,
          icon: body.icon || "book"
        }, null, 2)
      );

      if (rewritten && typeof rewritten === "object" && !Array.isArray(rewritten)) {
        const details = Array.isArray(rewritten.details)
          ? rewritten.details.map((x) => String(x || "").trim()).filter(Boolean).join("\n")
          : String(rewritten.details || "").trim();
        return json(200, {
          title: String(rewritten.title || "").trim() || q || "推荐内容",
          desc: String(rewritten.desc || "").trim() || a.split(/\r?\n/)[0].slice(0, 120),
          details: details || a,
          icon: rewritten.icon || body.icon || "book"
        });
      }
    } catch (e) {
      // Fall back to deterministic rewriting below.
    }

    return json(200, {
      title: q || "推荐内容",
      desc: a.split(/\r?\n/)[0].slice(0, 120),
      details: a,
      icon: body.icon || "book"
    });
  } catch (e) {
    return json(500, { error: e && e.message ? e.message : "服务异常" });
  }
};

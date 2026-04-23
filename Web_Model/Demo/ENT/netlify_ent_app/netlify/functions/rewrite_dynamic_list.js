const { callAiJson, json } = require("./api_common");

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const items = Array.isArray(body.items) ? body.items : [];
    const payload = items.slice(0, 12).map((item) => ({
      q: String(item.q || ""),
      a: String(item.a || ""),
      icon: item.icon || "book"
    }));

    try {
      const rewritten = await callAiJson(
        [
          "你是校园生活助手的推荐列表文案改写器。",
          "输入是若干条 QA 命中，请把每条改写成适合右侧栏 AI 卡片展示的文案。",
          "总体风格：礼貌但极简、信息密度高、全部用陈述句。",
          "强约束：不要输出疑问句；不要直接复制 q 原句；不要出现“问/答/Q/A”。",
          "title 必须是事实字段型标题，包含明确主语，直接点题。",
          "desc 是 1 句摘要，尽量包含最关键的时间、地点、入口、材料、截止或费用等信息。",
          "只输出严格 JSON 数组，长度必须与输入一致：[{\"title\":\"...\",\"desc\":\"...\",\"icon\":\"book\"}]"
        ].join("\n"),
        JSON.stringify({ contextKey: body.contextKey || "", items: payload }, null, 2)
      );

      if (Array.isArray(rewritten) && rewritten.length) {
        return json(200, {
          items: items.map((item, idx) => {
            const r = rewritten[idx] || {};
            return {
              title: String(r.title || "").trim() || String(item.q || "").replace(/[？?]\s*$/g, "").trim() || "推荐内容",
              desc: String(r.desc || "").trim() || String(item.a || "").split(/\r?\n/)[0].slice(0, 100),
              icon: r.icon || item.icon || "book"
            };
          })
        });
      }
    } catch (e) {
      // Fall back to deterministic rewriting below.
    }

    return json(200, {
      items: items.map((item) => {
        const q = String(item.q || "").replace(/[？?]\s*$/g, "").trim();
        const a = String(item.a || "").trim();
        return {
          title: q || "推荐内容",
          desc: a.split(/\r?\n/)[0].slice(0, 100),
          icon: item.icon || "book"
        };
      })
    });
  } catch (e) {
    return json(500, { error: e && e.message ? e.message : "服务异常" });
  }
};

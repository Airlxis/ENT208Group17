const { answer, disabledMessage, isEnabled, json } = require("./api_common");

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  if (!isEnabled()) return json(403, { error: disabledMessage() });

  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const message = String(body.message || "").trim();
    if (!message) return json(400, { error: "message 不能为空" });
    return json(200, { reply: await answer(message) });
  } catch (e) {
    return json(500, { error: e && e.message ? e.message : "服务异常" });
  }
};

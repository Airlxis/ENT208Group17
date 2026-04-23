const { json } = require("./api_common");

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const message = String(body.message || "").trim();
    const title = message ? (message.length > 16 ? `${message.slice(0, 16)}...` : message) : "新对话";
    return json(200, { title });
  } catch (e) {
    return json(200, { title: "新对话" });
  }
};

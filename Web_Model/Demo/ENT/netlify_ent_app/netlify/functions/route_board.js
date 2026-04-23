const { json, routeFor } = require("./api_common");

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  try {
    const body = event.body ? JSON.parse(event.body) : {};
    return json(200, routeFor(body.text || body.message || ""));
  } catch (e) {
    return json(200, { moduleKey: null, pageKey: null, confidence: 0 });
  }
};

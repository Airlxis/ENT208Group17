exports.handler = async function handler() {
  const enabled = String(process.env.APP_ENABLED || "true").toLowerCase() === "true";
  const message = process.env.APP_DISABLED_MESSAGE || "服务暂停，请稍后再试";
  const version = process.env.APP_VERSION || "v1";

  return {
    statusCode: 200,
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({
      enabled,
      message,
      version,
      ts: Date.now()
    })
  };
};

const { json, routeFor, topMatches } = require("./api_common");

const SUBMODULES = {
  study: ["study_course", "study_exam", "study_language", "study_planning"],
  intern: ["intern_resume", "intern_interview", "intern_network", "intern_plan"],
  campus: ["campus_club", "campus_event", "campus_volunteer", "campus_leadership"],
  life: ["life_dorm", "life_health", "life_food", "life_emergency"],
  research: ["research_topic", "research_paper", "research_method", "research_writing"]
};

exports.handler = async function handler(event) {
  if (event.httpMethod !== "POST") return json(405, { error: "Method Not Allowed" });
  try {
    const body = event.body ? JSON.parse(event.body) : {};
    const moduleKey = String(body.moduleKey || "").trim();
    const queryText = String(body.queryText || "").trim();
    const maxCards = Number(body.max_cards || body.maxCards || 8) || 8;
    const submoduleKeys = Array.isArray(body.submoduleKeys) ? body.submoduleKeys : (SUBMODULES[moduleKey] || []);
    const submodules = {};

    for (const key of submoduleKeys) {
      submodules[key] = topMatches(moduleKey, `${queryText} ${key}`, Math.min(4, maxCards));
    }

    const route = routeFor(queryText);
    const globalHits = route.moduleKey === moduleKey ? topMatches("all", queryText, 3) : [];

    return json(200, {
      moduleKey,
      cards: topMatches(moduleKey, queryText, maxCards),
      submodules,
      globalHits
    });
  } catch (e) {
    return json(500, { error: e && e.message ? e.message : "服务异常" });
  }
};

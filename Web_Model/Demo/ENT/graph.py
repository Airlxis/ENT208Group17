import os
import json
import asyncio
import difflib
import re
import logging
import urllib.parse
from typing import Optional
import sys

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_deepseek import ChatDeepSeek
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from pydantic import BaseModel

# 加载环境变量
load_dotenv(override=True)

# 简单日志（输出到运行终端）
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qa-hit")
logger.setLevel(logging.INFO)
# Ensure QA hit logs always show in terminal (even if uvicorn logging config changes)
if not logger.handlers:
    _h = logging.StreamHandler(stream=sys.stdout)
    _h.setLevel(logging.INFO)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
logger.propagate = False


def _qa_terminal_log(msg: str) -> None:
    """
    Always print to the running IDE terminal.
    Using print() avoids uvicorn/logger configuration swallowing custom logs.
    """
    try:
        print(msg, flush=True)
    except Exception:
        pass


# ======================
# 语言模型（后端核心）
# ======================

class ChatRequest(BaseModel):
    message: str
    language: str = "zh-CN"
    context: list[dict] = []


class ChatResponse(BaseModel):
    reply: str


class ScheduleCommandParseRequest(BaseModel):
    text: str
    language: str = "zh-CN"
    context: list[dict] = []
    schedule: dict = {}


class ScheduleCommandParseResponse(BaseModel):
    handled: bool = False
    action: Optional[str] = None
    target: Optional[str] = None
    matchName: str = ""
    item: Optional[dict] = None
    confidence: float = 0.0


class TitleRequest(BaseModel):
    message: str
    language: str = "zh-CN"


class TitleResponse(BaseModel):
    title: str

class ModuleScanRequest(BaseModel):
    moduleKey: str
    submoduleKeys: list[str] = []
    max_cards: int = 8
    queryText: str = ""
    language: str = "zh-CN"


def _tokenize_mixed(text: str) -> list[str]:
    """
    No extra deps tokenizer.
    - English/digits: [A-Za-z0-9]+ lowercased
    - Chinese: bigrams over contiguous CJK blocks
    """
    t = (text or "").strip()
    if not t:
        return []

    tokens: list[str] = []
    # English/number tokens
    for w in re.findall(r"[A-Za-z0-9]+", t):
        if w:
            tokens.append(w.lower())

    # Chinese bigrams
    for m in re.finditer(r"[\u4e00-\u9fff]+", t):
        s = m.group(0)
        if not s:
            continue
        if len(s) == 1:
            tokens.append(s)
            continue
        for i in range(len(s) - 1):
            tokens.append(s[i : i + 2])

    return tokens


def _bm25_scores(query_tokens: list[str], docs_tokens: list[list[str]], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """
    Tiny BM25 implementation (no deps). Returns score for each doc.
    """
    if not query_tokens or not docs_tokens:
        return [0.0 for _ in docs_tokens]

    N = len(docs_tokens)
    avgdl = sum(len(d) for d in docs_tokens) / max(1, N)

    # document frequencies
    df: dict[str, int] = {}
    for doc in docs_tokens:
        seen = set(doc)
        for tok in seen:
            df[tok] = df.get(tok, 0) + 1

    # idf
    idf: dict[str, float] = {}
    for tok, f in df.items():
        idf[tok] = max(0.0, ( (N - f + 0.5) / (f + 0.5) ))

    scores: list[float] = []
    for doc in docs_tokens:
        dl = len(doc)
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        s = 0.0
        denom_base = k1 * (1 - b + b * (dl / (avgdl or 1.0)))
        for qt in query_tokens:
            f = tf.get(qt, 0)
            if f <= 0:
                continue
            # idf variant (log omitted for speed/stability in small N)
            w = idf.get(qt, 0.0)
            s += (w * (f * (k1 + 1))) / (f + denom_base)
        scores.append(s)
    return scores


def _score_cards_for_query(cards: list["DynamicCard"], query: str) -> list["DynamicCard"]:
    """
    Sort cards by relevance to query using BM25 + field boosts.
    """
    q = (query or "").strip()
    if not q or not cards:
        return cards

    q_tokens = _tokenize_mixed(q)
    if not q_tokens:
        return cards

    # Build docs tokens with weighting by repetition
    docs_tokens: list[list[str]] = []
    title_tokens_list: list[set[str]] = []
    desc_tokens_list: list[set[str]] = []
    for c in cards:
        title = c.title or ""
        desc = c.desc or ""
        details = c.details or ""
        t_tok = _tokenize_mixed(title)
        d_tok = _tokenize_mixed(desc)
        b_tok = _tokenize_mixed(details)
        # weight: title x2, desc x1, details x1
        doc = (t_tok + t_tok) + d_tok + b_tok
        docs_tokens.append(doc)
        title_tokens_list.append(set(t_tok))
        desc_tokens_list.append(set(d_tok))

    bm25 = _bm25_scores(q_tokens, docs_tokens)

    scored: list[tuple[float, int, DynamicCard]] = []
    qset = set(q_tokens)
    for i, c in enumerate(cards):
        s = bm25[i]
        # boosts
        s += 2.0 * len(qset.intersection(title_tokens_list[i]))
        s += 1.0 * len(qset.intersection(desc_tokens_list[i]))
        scored.append((s, i, c))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [c for _, __, c in scored]


class DynamicCard(BaseModel):
    title: str
    desc: str = ""
    icon: str = "book"
    action: str = ""
    details: str = ""


def _normalize_language(language: str | None) -> str:
    lang = (language or "zh-CN").strip().lower()
    if lang.startswith("en"):
        return "en-US"
    return "zh-CN"


def _has_cjk(text: str | None) -> bool:
    s = str(text or "")
    return bool(re.search(r"[\u3400-\u9fff]", s))


def _enforce_english_list(items: list[dict], context_key: str = "") -> list[dict]:
    """
    Ensure list card copy is English-only.
    If any CJK remains, ask model to translate/adapt as strict JSON.
    """
    if not items:
        return items
    if not any(_has_cjk(str(it.get("title", "")) + " " + str(it.get("desc", ""))) for it in items):
        return items
    try:
        payload = [{"title": str(it.get("title", "")).strip(), "desc": str(it.get("desc", "")).strip()} for it in items]
        prompt = (
            "You are an English UI copy editor for campus assistant cards.\n"
            "Translate/adapt every item into concise natural English.\n"
            "Hard rules:\n"
            "- Output strict JSON array only, same length as input.\n"
            "- Each item must be: {\"title\":\"...\",\"desc\":\"...\"}\n"
            "- Do not output any Chinese text.\n"
            "- Keep meaning and specificity.\n"
        )
        result = model.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=f"contextKey: {context_key}\ninput: {json.dumps(payload, ensure_ascii=False)}"),
            ]
        )
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\[[\s\S]*\]", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        if not isinstance(data, list):
            return items
        out = []
        for i, it in enumerate(items):
            row = data[i] if i < len(data) and isinstance(data[i], dict) else {}
            title = str(row.get("title", it.get("title", ""))).strip() or str(it.get("title", ""))
            desc = str(row.get("desc", it.get("desc", ""))).strip() or str(it.get("desc", ""))
            if _has_cjk(title) or not title:
                title = "Recommended Information"
            if _has_cjk(desc) or not desc:
                desc = "Key information for this topic."
            out.append(
                {
                    "title": title,
                    "desc": desc,
                    "icon": str(it.get("icon", "book")).strip() or "book",
                }
            )
        return out
    except Exception:
        return [
            {
                "title": "Recommended Information",
                "desc": "Key information for this topic.",
                "icon": str(it.get("icon", "book")).strip() or "book",
            }
            for it in items
        ]


def _enforce_english_detail(title: str, desc: str, details: str, context_key: str = "") -> tuple[str, str, str]:
    """
    Ensure detail card fields are English-only.
    """
    if not (_has_cjk(title) or _has_cjk(desc) or _has_cjk(details)):
        return title, desc, details
    try:
        prompt = (
            "You are an English UI copy editor for a campus assistant detail card.\n"
            "Translate/adapt all fields into concise natural English.\n"
            "Hard rules:\n"
            "- Output strict JSON only: {\"title\":\"...\",\"desc\":\"...\",\"details\":[\"...\",\"...\"]}\n"
            "- details must have 4-8 bullet points in English.\n"
            "- Do not output any Chinese text.\n"
        )
        src = {"title": title, "desc": desc, "details": details}
        result = model.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=f"contextKey: {context_key}\ninput: {json.dumps(src, ensure_ascii=False)}"),
            ]
        )
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        nt = str(data.get("title", "")).strip() or title
        nd = str(data.get("desc", "")).strip() or desc
        det = data.get("details", [])
        if isinstance(det, list):
            nlines = [str(x).strip() for x in det if str(x).strip()]
            ndd = "\n".join(nlines) if nlines else details
        else:
            ndd = str(det or "").strip() or details
        if _has_cjk(nt) or not nt:
            nt = "Recommended Information"
        if _has_cjk(nd) or not nd:
            nd = "Key information for this topic."
        if _has_cjk(ndd) or not ndd:
            ndd = (
                "Check the official portal for the latest policy.\n"
                "Prepare required documents before submitting.\n"
                "Follow deadlines and keep confirmation records."
            )
        return nt, nd, ndd
    except Exception:
        return (
            "Recommended Information",
            "Key information for this topic.",
            "Check the official portal for the latest policy.\nPrepare required documents before submitting.\nFollow deadlines and keep confirmation records.",
        )


def _language_instruction(language: str | None) -> str:
    if _normalize_language(language) == "en-US":
        return (
            "\n\nLanguage requirement: answer entirely in natural, clear English with enough detail to be useful. "
            "Translate and adapt any Chinese reference material into English. "
            "Do not include Chinese text unless the user explicitly asks for it."
        )
    return "\n\n语言要求：全部使用自然、清晰、信息足够的简体中文回答，不要为了简短而省略关键步骤。"


def _context_messages(context: list[dict] | None, max_items: int = 3) -> list:
    """
    Convert the browser's lightweight recent-chat context into LangChain messages.
    This is intentionally small: the current app is still mostly single-turn, but
    the model gets the last few simple turns for references such as names.
    """
    out = []
    items = context if isinstance(context, list) else []
    for item in items[-max(0, int(max_items or 0)) :]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        text = text[:600]
        if role == "assistant":
            out.append(AIMessage(content=text))
        else:
            out.append(HumanMessage(content=text))
    return out


class ModuleScanResponse(BaseModel):
    moduleKey: str
    cards: list[DynamicCard]
    submodules: dict[str, list[DynamicCard]]
    globalHits: list[DynamicCard] = []


# ✅ 创建模型（DeepSeek）
model = ChatDeepSeek(model="deepseek-chat")

# ✅ XJTLU学生助手系统提示词
FALLBACK_ENTRY_URL = "https://www.xjtlu.edu.cn/zh/it-services/e-bridge-intro"

SYSTEM_PROMPT = (
    "你是“XJTLU生活助手”，一名面向西交利物浦大学（XJTLU）学生的智能助手，"
    "主要帮助新生和在校生解决与XJTLU学习、生活相关的问题。"
    "简单上下文：用户通常是在本地网页里使用右侧“板块服务”和聊天框。"
    "当用户提到个人日程、课程表、课表或日程修改时，语境是个人日程板块，不是校园活动板块。"
    "回答要结合当前问题给出可执行的下一步，不要只给一句过度压缩的结论。\n"
    "严禁使用表情符号、颜文字、特殊符号（例如✅❌⭐️•等），不要输出任何 emoji。\n"
    "回答要求：\n"
    "1）使用自然、清晰、适度展开的简体中文，礼貌热情，不要写成公文或宣传稿；\n"
    "2）优先从XJTLU学生的视角给出具体、可执行的建议，例如时间规划、课程学习方法、校园资源使用、新生适应等；\n"
    "3）对不确定的校内规定或可能变动的信息，要提醒“具体以学校官方最新通知为准”；\n"
    "4）可以适当鼓励和共情，但不要夸张，也不要频繁重复类似“很高兴为你服务”之类客套话；\n"
    "5）使用用户的同种语言来回答。\n"
    "6）如果回答中涉及外部网站或线上订阅/线上资源/官方入口等需要访问的内容：\n"
    "- 请基于语义和办理意图来判断，不要仅凭是否出现某个关键词；\n"
    "- 如果你知道对应的具体网址：必须在回答中输出完整的 `https://...` 超链接，使用纯文本 URL 形式输出（不要使用 `[文字](URL)` 的 Markdown 链接写法）；\n"
    "- 如果你不确定具体网址：统一使用默认入口链接 `https://www.xjtlu.edu.cn/zh/it-services/e-bridge-intro` 作为回退参考；\n"
    "- 默认链接只在“不确定具体网址”的场景使用。"
)


def _fallback_entry_delta_if_needed(reply: str, user_question: str = "") -> str:
    """
    Return a delta string to append when:
    - the reply mentions online access/portal/reservation/subscription-like content
    - but no http(s) URL is present in the reply.
    """
    if not reply:
        return ""
    if re.search(r"https?://", str(reply), flags=re.IGNORECASE):
        return ""

    text = f"{str(user_question or '')}\n{str(reply or '')}"
    triggers = [
        "官网",
        "入口",
        "链接",
        "网站",
        "系统",
        "平台",
        "在线",
        "线上",
        "订阅",
        "预约",
        "报名",
        "登录",
        "注册",
        "提交",
        "e-bridge",
        "eBridge",
        "e-Bridge",
        "线上预约",
        "在线办理",
    ]
    if any(t in text for t in triggers):
        return "\n\n" + FALLBACK_ENTRY_URL
    return ""


def _llm_should_attach_entry_link(reply: str, language: str | None = "zh-CN") -> Optional[bool]:
    """
    Ask the model to semantically decide whether this reply requires an online-entry URL.
    Returns True/False; returns None on failure.
    """
    text = str(reply or "").strip()
    if not text:
        return False
    if re.search(r"https?://", text, flags=re.IGNORECASE):
        return False

    if _normalize_language(language) == "en-US":
        judge_prompt = (
            "You are a strict classifier. "
            "Given an assistant reply, decide whether the reply requires users to visit an online page/system/portal "
            "to proceed (for example: online application, online booking, web portal, official website entry, e-Bridge, Learning Mall). "
            "Decide by semantics and user intent, not only keywords.\n"
            "Output only YES or NO."
        )
    else:
        judge_prompt = (
            "你是一个严格二分类器。给定助手回复，判断该回复是否需要用户去线上页面/系统/门户网站继续操作"
            "（例如：在线申请、线上预约、网站入口、官网系统、e-Bridge、学习超市）。"
            "请基于语义和办理意图判断，不要只看关键词。\n"
            "只输出：YES 或 NO。"
        )

    try:
        result = model.invoke(
            [
                SystemMessage(content=judge_prompt),
                HumanMessage(content=text[:2200]),
            ]
        )
        raw = (result.content if hasattr(result, "content") else str(result) or "").strip().upper()
        if raw.startswith("YES"):
            return True
        if raw.startswith("NO"):
            return False
        return None
    except Exception:
        return None


def _ensure_entry_link_if_needed(reply: str, language: str | None = "zh-CN", user_question: str = "") -> str:
    # 1) semantic decision by model (primary)
    llm_decision = _llm_should_attach_entry_link(
        f"用户问题：{str(user_question or '').strip()}\n助手回答：{str(reply or '').strip()}",
        language=language,
    )
    if llm_decision is True:
        return str(reply).rstrip() + "\n\n" + FALLBACK_ENTRY_URL
    if llm_decision is False:
        return reply

    # 2) heuristic fallback (safety net when model judge fails)
    delta = _fallback_entry_delta_if_needed(reply, user_question=user_question)
    if not delta:
        return reply
    return str(reply).rstrip() + delta


def _strip_emojis_and_symbols(s: str) -> str:
    """
    Remove emojis and common decorative symbols from output.
    Keep normal Chinese/English letters, numbers, and common punctuation.
    """
    if not s:
        return ""
    # emoji blocks + variation selectors + dingbats
    s = re.sub(r"[\U0001F300-\U0001FAFF]", "", s)
    s = re.sub(r"[\u2600-\u26FF\u2700-\u27BF]", "", s)  # misc symbols + dingbats
    s = s.replace("\uFE0F", "")  # variation selector-16
    # remove some decorative bullets/stars explicitly
    s = s.replace("•", "").replace("★", "").replace("☆", "").replace("●", "").replace("◆", "")
    return s


_URL_RE = re.compile(r"(https?://[^\s<>\]\)\"']+)", re.IGNORECASE)


def _append_link_briefs(text: str, max_links: int = 6, language: str = "zh-CN") -> str:
    """
    If `text` contains URL(s), append a short "相关链接" section with brief hints.
    """
    t = str(text or "").strip()
    if not t:
        return ""
    # avoid repeated appends (idempotent enough)
    if ("相关链接" in t or "Related links" in t) and "http" in t:
        return t
    urls = _URL_RE.findall(t)
    if not urls:
        return t
    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        u0 = u.strip().rstrip(").,;，。；】》")
        if not u0 or u0 in seen:
            continue
        seen.add(u0)
        uniq.append(u0)
        if len(uniq) >= max(1, int(max_links or 1)):
            break

    def _brief(u: str) -> str:
        try:
            p = urllib.parse.urlparse(u)
            host = (p.netloc or "").lower()
            if not host:
                return "相关页面"
            if "xjtlu" in host:
                return "学校官方页面"
            if "mp.weixin.qq.com" in host or host.endswith("weixin.qq.com"):
                return "微信公众号文章"
            if host.endswith("gov.cn"):
                return "政府官网页面"
            return f"{host} 页面"
        except Exception:
            return "相关页面"

    heading = "Related links (click to open):" if _normalize_language(language) == "en-US" else "相关链接（可点击打开）："
    lines = ["", "", heading]
    for u in uniq:
        lines.append(f"- {u}（{_brief(u)}）")
    return (t + "\n" + "\n".join(lines)).strip()

# ======================
# 本地 QA 表（优先命中）
# ======================

QA_JSON_PATH = os.path.join(os.path.dirname(__file__), "qa.json")
QA_TXT_PATH = os.path.join(os.path.dirname(__file__), "qa.txt")
_QA_CACHE = []


def _parse_qa_txt(path: str) -> list[dict]:
    """
    Parse qa.txt format: question line + answer line + blank line (repeat).
    Also supports optional "Q<number> ..." prefix on question lines.
    """
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n").strip() for ln in f.readlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    items: list[dict] = []
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        q = re.sub(r"^\s*Q\d+\s*[\.\:：、\-]?\s*", "", lines[i].strip(), flags=re.IGNORECASE)
        a = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if q and a:
            items.append({"q": q, "a": a})
        i += 3
    return items


def _sync_qa_txt_to_json() -> int:
    """
    If qa.txt exists, convert it into qa.json (single source of truth).
    Returns number of pairs written, or 0 if no sync happened.
    """
    if not os.path.exists(QA_TXT_PATH):
        return 0
    items = _parse_qa_txt(QA_TXT_PATH)
    if not items:
        return 0
    try:
        with open(QA_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return len(items)
    except Exception:
        return 0


def load_qa() -> int:
    """
    加载问答表到内存（总 QA）。
    约定：总 QA 只从 qa.json 读取；如果 qa.txt 存在，则启动/重载时会先把 qa.txt 同步生成 qa.json。
    """
    global _QA_CACHE
    # If qa.txt exists, sync it to qa.json first (so we only query qa.json later)
    _sync_qa_txt_to_json()

    json_items: list[dict] = []
    if os.path.exists(QA_JSON_PATH):
        try:
            with open(QA_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("q", "")).strip()
                    a = str(item.get("a", "")).strip()
                    if q and a:
                        json_items.append({"q": q, "a": a})
        except Exception:
            json_items = []

    _QA_CACHE = json_items
    return len(_QA_CACHE)


def _load_qa_from_path(path: str) -> list[dict]:
    """
    Load QA pairs from a given file path.
    Supported:
    - .json: [{ "q": "...", "a": "..." }, ...]
    - .txt: question line + answer line + empty line
    """
    if not path or not os.path.exists(path):
        return []

    if path.lower().endswith(".json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleaned = []
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("q", "")).strip()
                    a = str(item.get("a", "")).strip()
                    if q and a:
                        cleaned.append({"q": q, "a": a})
            return cleaned
        except Exception:
            return []

    # txt
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n").strip() for ln in f.readlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()

        cleaned = []
        i = 0
        while i < len(lines):
            if not lines[i]:
                i += 1
                continue
            q = re.sub(r"^\s*Q\d+\s*[\.\:：、\-]?\s*", "", lines[i].strip(), flags=re.IGNORECASE)
            a = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if q and a:
                cleaned.append({"q": q, "a": a})
            i += 3
        return cleaned
    except Exception:
        return []


def _resolve_qa_file_by_key(key: str) -> Optional[str]:
    """
    Resolve QA file for a given module/submodule key.
    Convention:
    - qa_<key>.json preferred, else qa_<key>.txt
    Examples:
    - campus -> qa_campus.json / qa_campus.txt
    - campus_volunteer -> qa_campus_volunteer.json / qa_campus_volunteer.txt
    """
    base = os.path.dirname(__file__)
    safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (key or "").strip())
    if not safe:
        return None
    json_path = os.path.join(base, f"qa_{safe}.json")
    txt_path = os.path.join(base, f"qa_{safe}.txt")
    if os.path.exists(json_path):
        return json_path
    if os.path.exists(txt_path):
        return txt_path
    return None


def _qa_pairs_to_cards(pairs: list[dict], max_cards: int, language: str = "zh-CN", context_key: str = "") -> list[DynamicCard]:
    cards: list[DynamicCard] = []
    if not pairs:
        return cards
    take = pairs[: max(0, int(max_cards or 0))]
    for item in take:
        q = str(item.get("q", "")).strip()
        a = str(item.get("a", "")).strip()
        if not q or not a:
            continue
        ql = q.lower()
        al0 = (a.split("\n")[0] if a else "").lower()
        text = f"{ql} {al0}"
        icon = "book"
        if any(k in text for k in ["简历", "resume", "面试", "interview", "投递", "offer", "实习"]):
            icon = "briefcase"
        elif any(k in text for k in ["活动", "社团", "志愿", "讲座", "比赛", "校园"]):
            icon = "calendar"
        elif any(k in text for k in ["宿舍", "报修", "饮食", "吃饭", "医疗", "emergency", "生活"]):
            icon = "home"
        elif any(k in text for k in ["论文", "实验", "方法", "research", "科研", "投稿", "文献"]):
            icon = "flask"
        elif any(k in text for k in ["英语", "雅思", "写作", "口语", "词汇", "阅读"]):
            icon = "globe"
        # 让卡片摘要更“具体”：取前 2 条要点/信息行，优先包含时间/地点/报名等字段
        lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
        key_fields = ("时间", "地点", "报名", "对象", "要求", "截止", "费用", "流程")
        picked: list[str] = []
        for ln in lines[:10]:
            if any(k in ln for k in key_fields):
                picked.append(ln)
            if len(picked) >= 2:
                break
        if not picked:
            picked = lines[:2]
        desc = " · ".join(picked).strip()
        if len(desc) > 80:
            desc = desc[:80].rstrip() + "…"
        cards.append(
            DynamicCard(
                title=q,
                desc=desc,
                icon=icon,
                action=q,
                details=_append_link_briefs(a, language=language),
            )
        )
    if _normalize_language(language) == "en-US" and cards:
        normalized = _enforce_english_list(
            [{"title": c.title, "desc": c.desc, "icon": c.icon} for c in cards],
            context_key=context_key,
        )
        for i, c in enumerate(cards):
            if i >= len(normalized):
                break
            row = normalized[i] if isinstance(normalized[i], dict) else {}
            c.title = str(row.get("title", c.title)).strip() or c.title
            c.desc = str(row.get("desc", c.desc)).strip() or c.desc
            c.icon = str(row.get("icon", c.icon)).strip() or c.icon
    return cards


def qa_match(user_question: str, cutoff: float = 0.72) -> Optional[str]:
    """
    用 difflib 做一个轻量“相似问句”匹配。
    - cutoff 越高越严格（0~1）。
    """
    question = (user_question or "").strip()
    if not question or not _QA_CACHE:
        return None
    qs = [item["q"] for item in _QA_CACHE]
    best = difflib.get_close_matches(question, qs, n=1, cutoff=cutoff)
    if not best:
        return None
    best_q = best[0]
    for item in _QA_CACHE:
        if item["q"] == best_q:
            return item["a"]
    return None


def qa_best_matches(user_question: str, k: int = 3, cutoff: float = 0.72) -> list[dict]:
    """
    Return up to k best matches from the global QA cache as [{q,a,score}, ...]
    """
    question = (user_question or "").strip()
    if not question or not _QA_CACHE:
        return []
    qs = [item["q"] for item in _QA_CACHE]
    best = difflib.get_close_matches(question, qs, n=max(1, int(k or 1)), cutoff=cutoff)
    out: list[dict] = []
    for bq in best:
        for item in _QA_CACHE:
            if item["q"] == bq:
                score = difflib.SequenceMatcher(None, question, bq).ratio()
                out.append({"q": item["q"], "a": item["a"], "score": score})
                break
    out.sort(key=lambda x: -float(x.get("score", 0.0)))
    return out


def _format_multi_refs(refs: list[dict], max_len: int = 1200) -> str:
    """
    refs: [{q,a,score,source}, ...]
    """
    chunks: list[str] = []
    used = 0
    for r in refs:
        q = str(r.get("q", "")).strip()
        a = str(r.get("a", "")).strip()
        src = str(r.get("source", "")).strip()
        if not q or not a:
            continue
        block = f"【{src or 'QA参考'}】\nQ: {q}\nA: {a}\n"
        if used + len(block) > max_len:
            break
        chunks.append(block)
        used += len(block)
    return "\n".join(chunks).strip()


def _top_module_refs(module_key: str, query: str, k: int = 2) -> list[dict]:
    """
    Pull top-k refs from a module QA table using BM25 (no deps).
    """
    mk = (module_key or "").strip()
    q = (query or "").strip()
    if not mk or not q:
        return []
    path = _resolve_qa_file_by_key(mk)
    if not path:
        return []
    pairs = _load_qa_from_path(path)
    if not pairs:
        return []
    docs = []
    keep = []
    for it in pairs:
        qq = str(it.get("q", "")).strip()
        aa = str(it.get("a", "")).strip()
        if not qq or not aa:
            continue
        # doc text: q + first line of a
        first = (aa.splitlines()[0] if aa else "").strip()
        docs.append(_tokenize_mixed(f"{qq} {first}"))
        keep.append((qq, aa))
    if not docs:
        return []
    scores = _bm25_scores(_tokenize_mixed(q), docs)
    ranked = sorted(list(enumerate(scores)), key=lambda x: -x[1])[: max(0, int(k or 0))]
    out = []
    for idx, sc in ranked:
        qq, aa = keep[idx]
        out.append({"q": qq, "a": aa, "score": float(sc), "source": f"模块QA:{mk}"})
    return out


def _global_hits_to_cards(question: str, max_cards: int, language: str = "zh-CN") -> list[DynamicCard]:
    hits = qa_best_matches(question, k=max(1, int(max_cards or 1)), cutoff=0.72)
    cards: list[DynamicCard] = []
    for h in hits:
        q = str(h.get("q", "")).strip()
        a = str(h.get("a", "")).strip()
        if not q or not a:
            continue
        # reuse icon heuristic similar to other cards
        text = (q + " " + (a.splitlines()[0] if a else "")).lower()
        icon = "book"
        if any(k in text for k in ["简历", "resume", "面试", "interview", "投递", "offer", "实习"]):
            icon = "briefcase"
        elif any(k in text for k in ["活动", "社团", "志愿", "讲座", "比赛", "校园", "迎新", "报到", "注册"]):
            icon = "calendar"
        elif any(k in text for k in ["宿舍", "报修", "饮食", "吃饭", "医疗", "emergency", "生活"]):
            icon = "home"
        elif any(k in text for k in ["论文", "实验", "方法", "research", "科研", "投稿", "文献", "暑研", "套磁"]):
            icon = "flask"
        elif any(k in text for k in ["英语", "雅思", "写作", "口语", "词汇", "阅读"]):
            icon = "globe"
        # desc: first key info line
        lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
        desc = (lines[0] if lines else "")[:80].rstrip() + ("…" if len(lines[0]) > 80 else "") if lines else ""
        cards.append(DynamicCard(title=q, desc=desc, icon=icon, action=q, details=_append_link_briefs(a, language=language)))
    cards = cards[: max(0, int(max_cards or 0))]
    if _normalize_language(language) == "en-US" and cards:
        normalized = _enforce_english_list(
            [{"title": c.title, "desc": c.desc, "icon": c.icon} for c in cards],
            context_key="global",
        )
        for i, c in enumerate(cards):
            if i >= len(normalized):
                break
            row = normalized[i] if isinstance(normalized[i], dict) else {}
            c.title = str(row.get("title", c.title)).strip() or c.title
            c.desc = str(row.get("desc", c.desc)).strip() or c.desc
            c.icon = str(row.get("icon", c.icon)).strip() or c.icon
    return cards


def _best_module_qa_match(question: str, cutoff: float = 0.70) -> Optional[dict]:
    """
    Search module/submodule QA tables for the best matching question.
    Returns {moduleKey,pageKey,q,a,icon,score} or None.
    """
    qn = (question or "").strip()
    if not qn:
        return None
    # normalize: drop school name tokens so "新生报到流程是什么" can match
    qn_norm = re.sub(r"(西交利物浦|\u897f\u6d66|XJTLU|xjtlu)", "", qn, flags=re.IGNORECASE).strip()
    if not qn_norm:
        qn_norm = qn

    allowed_pages = {
        "study": [
            "study_my",
            "study_course",
            "study_course_calc",
            "study_course_stats",
            "study_course_cs",
            "study_course_econ",
            "study_english",
            "study_english_vocab",
            "study_english_reading",
            "study_english_writing",
            "study_english_speaking",
            "study_plan",
            "study_writing",
            "study_methods",
        ],
        "intern": ["intern_resume", "intern_interview", "intern_network", "intern_plan"],
        "campus": ["campus_club", "campus_event", "campus_volunteer", "campus_leadership"],
        "life": ["life_dorm", "life_health", "life_food", "life_emergency"],
        "research": ["research_topic", "research_paper", "research_method", "research_writing"],
    }

    keys: list[tuple[str, Optional[str]]] = []
    for mk, pages in allowed_pages.items():
        keys.append((mk, None))
        for pk in pages:
            keys.append((mk, pk))

    best = None
    best_score = 0.0

    for mk, pk in keys:
        qa_key = pk or mk
        path = _resolve_qa_file_by_key(qa_key)
        if not path:
            continue
        pairs = _load_qa_from_path(path)
        if not pairs:
            continue
        qs = [str(item.get("q", "")).strip() for item in pairs if str(item.get("q", "")).strip()]
        if not qs:
            continue
        # difflib ratio search
        # normalize candidates similarly
        qs_norm = [re.sub(r"(西交利物浦|\u897f\u6d66|XJTLU|xjtlu)", "", x, flags=re.IGNORECASE).strip() for x in qs]
        candidates = difflib.get_close_matches(qn_norm, qs_norm, n=1, cutoff=cutoff)
        if not candidates:
            continue
        # map back to original question string
        cand_norm = candidates[0]
        cand_q = ""
        for orig, normed in zip(qs, qs_norm):
            if normed == cand_norm:
                cand_q = orig
                break
        if not cand_q:
            continue
        score = difflib.SequenceMatcher(None, qn_norm, cand_norm).ratio()
        if score >= best_score:
            # fetch answer
            cand_a = ""
            for item in pairs:
                if str(item.get("q", "")).strip() == cand_q:
                    cand_a = str(item.get("a", "")).strip()
                    break
            if not cand_a:
                continue
            # icon heuristic consistent with cards
            text = (cand_q + " " + (cand_a.splitlines()[0] if cand_a else "")).lower()
            icon = "book"
            if any(k in text for k in ["简历", "resume", "面试", "interview", "投递", "offer", "实习"]):
                icon = "briefcase"
            elif any(k in text for k in ["活动", "社团", "志愿", "讲座", "比赛", "校园"]):
                icon = "calendar"
            elif any(k in text for k in ["宿舍", "报修", "饮食", "吃饭", "医疗", "emergency", "生活"]):
                icon = "home"
            elif any(k in text for k in ["论文", "实验", "方法", "research", "科研", "投稿", "文献", "暑研", "套磁"]):
                icon = "flask"
            elif any(k in text for k in ["英语", "雅思", "写作", "口语", "词汇", "阅读"]):
                icon = "globe"
            best = {"moduleKey": mk, "pageKey": pk, "q": cand_q, "a": cand_a, "icon": icon, "score": score}
            best_score = score

    return best


_SCHEDULE_DAY_MAP = {
    "一": "mon",
    "1": "mon",
    "二": "tue",
    "2": "tue",
    "三": "wed",
    "3": "wed",
    "四": "thu",
    "4": "thu",
    "五": "fri",
    "5": "fri",
    "mon": "mon",
    "monday": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "fri": "fri",
    "friday": "fri",
}


def _parse_schedule_day(text: str) -> tuple[str, bool]:
    s = str(text or "")
    zh = re.search(r"(?:周|星期|礼拜)\s*([一二三四五六日天1234567])", s)
    if zh:
        token = zh.group(1)
        if token in ("六", "6", "日", "天", "7"):
            return ("sat" if token in ("六", "6") else "sun", True)
        return (_SCHEDULE_DAY_MAP.get(token, ""), False)
    en = re.search(r"\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", s, re.I)
    if en:
        token = en.group(1).lower()
        if token.startswith("sat"):
            return ("sat", True)
        if token.startswith("sun"):
            return ("sun", True)
        if token.startswith("mon"):
            return ("mon", False)
        if token.startswith("tue"):
            return ("tue", False)
        if token.startswith("wed"):
            return ("wed", False)
        if token.startswith("thu"):
            return ("thu", False)
        if token.startswith("fri"):
            return ("fri", False)
    return ("", False)


def _parse_schedule_hm(raw: str) -> str:
    t = str(raw or "").strip().replace("：", ":").replace(".", ":").replace(" ", "")
    if not t:
        return ""
    m = re.match(r"^(\d{1,2})点(?:(\d{1,2})分?)?$", t)
    if not m:
        m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", t)
    if not m:
        return ""
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    if h < 0 or h > 23 or minute < 0 or minute > 59:
        return ""
    return f"{h:02d}:{minute:02d}"


def _parse_schedule_time_range(text: str) -> tuple[str, str, str]:
    s = str(text or "")
    m = re.search(
        r"(\d{1,2}(?:[:：.]\d{1,2}|点(?:\d{1,2})?(?:分)?)?)\s*(?:到|至|~|～|-|—|–|to)\s*(\d{1,2}(?:[:：.]\d{1,2}|点(?:\d{1,2})?(?:分)?)?)",
        s,
        re.I,
    )
    if not m:
        return ("", "", "")
    return (_parse_schedule_hm(m.group(1)), _parse_schedule_hm(m.group(2)), m.group(0))


def _detect_schedule_action(text: str) -> str:
    s = str(text or "")
    if re.search(r"(删除|删掉|移除|取消|remove|delete|cancel)", s, re.I):
        return "delete"
    if re.search(r"(修改|调整|改到|改成|改为|变更|更新|reschedule|change|move|update)", s, re.I):
        return "update"
    if re.search(r"(加|新增|添加|安排|创建|加入|add|create|schedule|安排到)", s, re.I):
        return "add"
    return ""


def _detect_schedule_target(text: str) -> str:
    s = str(text or "")
    if re.search(r"(课程表|课表|课程|上课|lecture|class|course|timetable|老师|教师|教室|room|teacher|lec|lab|prac)", s, re.I):
        return "course"
    if re.search(r"(活动|行程|事项|会议|event|activity|meeting|appointment)", s, re.I):
        return "activity"
    return ""


def _is_personal_schedule_intent(text: str) -> bool:
    s = str(text or "")
    action = _detect_schedule_action(s)
    target = _detect_schedule_target(s)
    day, _unsupported = _parse_schedule_day(s)
    start, _end, _raw = _parse_schedule_time_range(s)
    if re.search(
        r"(个人日程|我的日程|日程表|日程修改|修改日程|课程表|课表|个人课表|我的课表|personal schedule|my schedule|course schedule|class schedule|timetable)",
        s,
        re.I,
    ):
        return True
    return bool(action and target and (day or start or re.search(r"(日程|schedule|calendar)", s, re.I)))


def _context_has_schedule_reference(context: list[dict] | None) -> bool:
    items = context if isinstance(context, list) else []
    text = "\n".join(str(x.get("text", "")) for x in items if isinstance(x, dict))
    return bool(
        re.search(
            r"(已添加|已修改|Added|Updated).{0,20}(课程|活动|class|activity)|个人日程|课程表|课表|Personal Schedule",
            text,
            re.I,
        )
    )


def _is_contextual_schedule_intent(text: str, context: list[dict] | None) -> bool:
    s = str(text or "").strip()
    if not s or not _context_has_schedule_reference(context):
        return False
    return bool(re.search(r"(删除|删掉|移除|取消|修改|调整|改了|删了|delete|remove|cancel|change|update)", s, re.I))


def _clean_schedule_noise(text: str, kind: str = "activity", raw_time: str = "") -> str:
    t = str(text or "")
    if raw_time:
        t = t.replace(raw_time, " ")
    t = re.sub(r"(?:周|星期|礼拜)\s*[一二三四五六日天1234567]", " ", t)
    t = re.sub(r"\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", " ", t, flags=re.I)
    t = re.sub(r"(?:地点|教室|位置|location|room)\s*[:：]?\s*[A-Za-z0-9_\- ]{2,40}", " ", t, flags=re.I)
    t = re.sub(r"(?:老师|教师|授课人|teacher|lecturer)\s*[:：]?\s*[^，,。；;\n]{1,40}", " ", t, flags=re.I)
    t = re.sub(r"(?:简介|介绍|说明|备注|intro|description)\s*[:：].*$", " ", t, flags=re.I)
    t = re.sub(
        r"(?:个人日程|我的日程|日程表|日程|行程|课程表|课表|个人课表|我的课表|calendar|personal schedule|my schedule|course schedule|class schedule|timetable)",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(r"(?:帮我|请|一下|一个|一条|一门|一节|个|门|节)", " ", t, flags=re.I)
    t = re.sub(r"(?:加|新增|添加|安排|创建|加入|删除|删掉|移除|取消|修改|调整|改到|改成|改为|变更|更新|add|create|remove|delete|cancel|reschedule|change|move|update|schedule)", " ", t, flags=re.I)
    if kind == "course":
        t = re.sub(r"(?:课程|上课|lecture|class|course|lec|lab|prac)", " ", t, flags=re.I)
    else:
        t = re.sub(r"(?:活动|事项|会议|event|activity|meeting|appointment)", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[，,。；;]+", " ", t)).strip()


def _fallback_parse_schedule_command(text: str) -> ScheduleCommandParseResponse:
    s = str(text or "").strip()
    if not s or not _is_personal_schedule_intent(s):
        return ScheduleCommandParseResponse(handled=False, confidence=0.0)
    action = _detect_schedule_action(s) or "open"
    target = _detect_schedule_target(s) or "activity"
    day, _unsupported = _parse_schedule_day(s)
    start, end, raw_time = _parse_schedule_time_range(s)
    item: dict = {}
    if day:
        item["day"] = day
    if start:
        item["start"] = start
    if end:
        item["end"] = end
    if target == "course":
        loc = re.search(r"(?:地点|教室|位置|location|room)\s*[:：]?\s*([A-Za-z0-9_\- ]{2,40})", s, re.I)
        tea = re.search(r"(?:老师|教师|授课人|teacher|lecturer)\s*[:：]?\s*([^，,。；;\n]{1,40})", s, re.I)
        if loc:
            item["location"] = loc.group(1).strip()
        if tea:
            item["teacher"] = tea.group(1).strip()
    else:
        intro = re.search(r"(?:简介|介绍|说明|备注|intro|description)\s*[:：]\s*([^\n]+)", s, re.I)
        if intro:
            item["intro"] = intro.group(1).strip()

    name = ""
    match_name = ""
    if action == "add":
        m = re.search(r"(?:加|新增|添加|安排|创建|加入|add|create|schedule)(?:个|一个|一条|一门|一节)?(?:课程|课|活动|事项|event|activity|class|course)?[:：\s]*([^\n，。,；;]+)", s, re.I)
        name = _clean_schedule_noise(m.group(1), target, raw_time) if m else _clean_schedule_noise(s, target, raw_time)
        if name:
            item["name"] = name[:80]
    elif action in ("update", "delete"):
        m = re.search(r"(?:把|将)?\s*([^，。；;\n]{2,80}?)\s*(?:改到|改成|改为|调整到|调整为|变更为|更新为|删掉|删除|移除|取消|remove|delete|cancel|reschedule|change|move|update)", s, re.I)
        if not m:
            m = re.search(r"(?:修改|调整|更新|删除|删掉|移除|取消|remove|delete|cancel|change|update)\s*(?:课程|课表|课程表|活动|日程|event|course|class)?[:：\s]*([^，。；;\n]{2,80})", s, re.I)
        if m:
            match_name = _clean_schedule_noise(m.group(1), target, raw_time)
    return ScheduleCommandParseResponse(
        handled=True,
        action=action,
        target=target,
        matchName=match_name,
        item=item,
        confidence=0.72 if action != "open" else 0.88,
    )


def _sanitize_schedule_command_payload(data: dict, fallback: ScheduleCommandParseResponse) -> ScheduleCommandParseResponse:
    if not isinstance(data, dict):
        return fallback
    action = str(data.get("action") or "").strip().lower()
    target = str(data.get("target") or "").strip().lower()
    if action not in {"add", "update", "delete", "open", "none"}:
        action = fallback.action or "open"
    if target not in {"course", "activity"}:
        target = fallback.target or "activity"
    item = data.get("item") if isinstance(data.get("item"), dict) else {}
    if item.get("day"):
        day, _unsupported = _parse_schedule_day(str(item.get("day")))
        if not day and str(item.get("day")).lower() in _SCHEDULE_DAY_MAP:
            day = _SCHEDULE_DAY_MAP[str(item.get("day")).lower()]
        item["day"] = day or str(item.get("day")).strip().lower()
    if item.get("start"):
        item["start"] = _parse_schedule_hm(str(item.get("start"))) or str(item.get("start")).strip()
    if item.get("end"):
        item["end"] = _parse_schedule_hm(str(item.get("end"))) or str(item.get("end")).strip()
    if (not item.get("start") or not item.get("end")) and item.get("time"):
        st, ed, _raw = _parse_schedule_time_range(str(item.get("time")))
        if st:
            item["start"] = st
        if ed:
            item["end"] = ed
    try:
        conf = float(data.get("confidence", fallback.confidence or 0.0) or 0.0)
    except Exception:
        conf = float(fallback.confidence or 0.0)
    return ScheduleCommandParseResponse(
        handled=bool(data.get("handled", fallback.handled)),
        action=action,
        target=target,
        matchName=str(data.get("matchName") or data.get("match_name") or fallback.matchName or "").strip(),
        item=item or fallback.item,
        confidence=max(0.0, min(conf, 1.0)),
    )

# ✅ 为兼容现有 langgraph.json，保留一个最小的 graph 对象
#    这里只是简单地把单轮对话包装成一个“图”，不再使用原来的复杂提示词和工具。
def _simple_graph(inputs: dict) -> dict:
    """
    简单包装：接收 {"input": "..."}，返回 {"output": "..."}。
    主要用于兼容 langgraph 的调用方式。
    """
    user_input = inputs.get("input", "")
    res = model.invoke(user_input)
    return {"output": res.content if hasattr(res, "content") else str(res)}


graph = create_react_agent(model=model, tools=[])


# ======================
# FastAPI Web 服务
# ======================

app = FastAPI(title="Local LLM Proxy", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态资源目录（用于前端网页）
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 启动时加载 QA
load_qa()


@app.get("/")
async def index():
    """
    返回前端聊天页面。
    """
    index_path = os.path.join(static_dir, "index.html")
    return FileResponse(index_path)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    接收前端的单条消息，调用本地语言模型返回回复。
    （这里先实现最简单的“无记忆”单轮对话，后面如需对话历史可以再扩展。）
    """
    try:
        # 1) 多命中：从总 QA 表取 Top-K 作为参考
        global_refs = qa_best_matches(req.message, k=3, cutoff=0.62)
        for r in global_refs:
            r["source"] = "总QA"
        if global_refs:
            logger.info(
                "QA HIT (chat) global_refs=%s",
                "; ".join([f"{r.get('q','')}" for r in global_refs]),
            )
            _qa_terminal_log("QA HIT (chat) global_refs=" + "; ".join([str(r.get("q", "")) for r in global_refs]))
        else:
            logger.info("QA MISS (chat) global_refs=0")
            _qa_terminal_log("QA MISS (chat) global_refs=0")

        # 2) 统一走模型，由模型结合 QA 参考与提问来组织回答
        system_prompt = SYSTEM_PROMPT + _language_instruction(req.language)
        if global_refs:
            system_prompt += (
                "\n\n下面是与用户问题相关的多条内部参考信息。请你综合这些信息回答：\n"
                "- 参考信息仅用于理解与核对事实，不要逐字复读参考原文；请用你自己的话同义改写/重组结构\n"
                "- 如果参考信息已经足够回答，就以参考为主组织答案；如果参考信息不足，再结合你对校园常见流程/经验给出建议，并明确哪些是通用建议、哪些需要以学校官方最新通知为准\n"
                "- 如果是流程，请用编号步骤\n"
                f"{_format_multi_refs(global_refs)}\n"
            )

        messages = [
            SystemMessage(content=system_prompt),
            *_context_messages(req.context),
            HumanMessage(content=req.message),
        ]
        result = model.invoke(messages)
        reply = result.content if hasattr(result, "content") else str(result)

        # 轻量清洗：去掉 Markdown 粗体符号和每行开头的项目符号
        if isinstance(reply, str):
            # 1) 把 **粗体** 里的星号去掉，只保留文字
            reply = re.sub(r"\*\*(.*?)\*\*", r"\1", reply)

            # 2) 去掉每行最前面的 "* " 或 "- " 这种项目符号
            cleaned_lines = []
            for line in reply.splitlines():
                cleaned_line = re.sub(r"^\s*[\*\-]\s+", "", line)
                cleaned_lines.append(cleaned_line)
            reply = "\n".join(cleaned_lines)

        reply = _strip_emojis_and_symbols(reply)
        reply = _ensure_entry_link_if_needed(reply, language=req.language, user_question=req.message)

        return ChatResponse(reply=reply)
    except Exception as e:
        # 统一把下游模型错误包装成 502，让前端给出清晰提示
        raise HTTPException(status_code=502, detail=f"调用模型失败：{e}")


def _extract_chunk_content(chunk) -> str:
    """
    从 LangChain 流式 chunk 中提取文本。
    常见结构：AIMessageChunk.content
    """
    if chunk is None:
        return ""
    content = getattr(chunk, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


@app.post("/api/chat_stream")
async def chat_stream(req: ChatRequest):
    """
    SSE 流式输出：前端可边接收边“打字显示”。
    返回格式：每个 token 使用 `data: {"delta": "..."}\\n\\n`，
    结束标记 `data: [DONE]\\n\\n`。
    """

    async def event_generator():
        try:
            # 关键词优先：demo 里“报到/迎新/开学/注册”等必须稳定跳到校园活动模块
            msg = (req.message or "").strip()
            if _is_personal_schedule_intent(msg):
                module_hit = {"moduleKey": "schedule", "pageKey": None, "q": "", "a": "", "icon": "calendar", "score": 1.0}
            elif any(k in msg for k in ["报到", "迎新", "开学", "注册", "新生报到", "新生注册"]):
                module_hit = {"moduleKey": "campus", "pageKey": None, "q": "", "a": "", "icon": "calendar", "score": 1.0}
            else:
                module_hit = _best_module_qa_match(req.message)
            global_refs = qa_best_matches(req.message, k=3, cutoff=0.62)
            for r in global_refs:
                r["source"] = "总QA"
            if global_refs:
                logger.info(
                    "QA HIT (chat_stream) global_refs=%s",
                    "; ".join([f"{r.get('q','')}" for r in global_refs]),
                )
                _qa_terminal_log(
                    "QA HIT (chat_stream) global_refs=" + "; ".join([str(r.get("q", "")) for r in global_refs])
                )
            else:
                logger.info("QA MISS (chat_stream) global_refs=0")
                _qa_terminal_log("QA MISS (chat_stream) global_refs=0")

            system_prompt = SYSTEM_PROMPT + _language_instruction(req.language)
            if module_hit and module_hit.get("moduleKey"):
                # 先把 meta 发给前端：用于自动跳转右侧栏（不再直达详情卡片）
                yield f"data: {json.dumps({'meta': {'moduleKey': module_hit.get('moduleKey'), 'pageKey': module_hit.get('pageKey')}}, ensure_ascii=False)}\n\n"
                # 再从对应模块 QA 表补充 Top-K 参考（语义更贴近）
                module_key = str(module_hit.get("pageKey") or module_hit.get("moduleKey") or "").strip()
                module_refs = _top_module_refs(module_key, req.message, k=3)
                refs = module_refs + global_refs
                if module_refs:
                    logger.info(
                        "QA HIT (chat_stream) module=%s module_refs=%s",
                        module_key,
                        "; ".join([f"{r.get('q','')}" for r in module_refs]),
                    )
                    _qa_terminal_log(
                        f"QA HIT (chat_stream) module={module_key} module_refs="
                        + "; ".join([str(r.get("q", "")) for r in module_refs])
                    )
                else:
                    logger.info("QA MISS (chat_stream) module=%s module_refs=0", module_key)
                    _qa_terminal_log(f"QA MISS (chat_stream) module={module_key} module_refs=0")
                if refs:
                    system_prompt += (
                        "\n\n下面是与用户问题相关的多条内部参考信息。请你综合这些信息生成回答：\n"
                        "- 用礼貌、清晰、适度展开的陈述句回答，不要过度简化\n"
                        "- 参考信息仅用于理解与核对事实，不要逐字复读参考原文；请用你自己的话同义改写/重组结构\n"
                        "- 如果参考信息已经足够回答，就以参考为主组织答案；如果参考信息不足，再结合你对校园常见流程/经验给出建议，并明确哪些是通用建议、哪些需要以学校官方最新通知为准\n"
                        "- 如果是流程，请用编号步骤\n"
                        "参考信息：\n"
                        f"{_format_multi_refs(refs)}\n"
                    )
            elif global_refs:
                system_prompt += (
                    "\n\n下面是与用户问题相关的多条内部参考信息。请你综合这些信息回答：\n"
                    "- 参考信息仅用于理解与核对事实，不要逐字复读参考原文；请用你自己的话同义改写/重组结构\n"
                    "- 如果参考信息已经足够回答，就以参考为主组织答案；如果参考信息不足，再结合你对校园常见流程/经验给出建议，并明确哪些是通用建议、哪些需要以学校官方最新通知为准\n"
                    "- 如果是流程，请用编号步骤\n"
                    f"{_format_multi_refs(global_refs)}\n"
                )

            messages = [
                SystemMessage(content=system_prompt),
                *_context_messages(req.context),
                HumanMessage(content=req.message),
            ]

            accumulated = ""
            produced_any = False

            # 1) 优先走 async streaming
            if hasattr(model, "astream"):
                async for chunk in model.astream(messages):
                    token_text = _extract_chunk_content(chunk)
                    if not token_text:
                        continue
                    if token_text.startswith(accumulated):
                        delta = token_text[len(accumulated) :]
                    else:
                        delta = token_text
                    if not delta:
                        continue
                    delta = _strip_emojis_and_symbols(delta)
                    if not delta:
                        continue
                    produced_any = True
                    accumulated += delta
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"

            # 2) 其次走 sync streaming
            elif hasattr(model, "stream"):
                for chunk in model.stream(messages):
                    token_text = _extract_chunk_content(chunk)
                    if not token_text:
                        continue
                    if token_text.startswith(accumulated):
                        delta = token_text[len(accumulated) :]
                    else:
                        delta = token_text
                    if not delta:
                        continue
                    delta = _strip_emojis_and_symbols(delta)
                    if not delta:
                        continue
                    produced_any = True
                    accumulated += delta
                    yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"

            # 3) 最后兜底：单次 invoke，然后逐字符模拟
            if not produced_any:
                result = model.invoke(messages)
                reply = result.content if hasattr(result, "content") else str(result)
                reply = _strip_emojis_and_symbols(reply)
                reply = _ensure_entry_link_if_needed(reply, language=req.language, user_question=req.message)
                for ch in reply:
                    produced_any = True
                    accumulated += ch
                    yield f"data: {json.dumps({'delta': ch}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.008)

            # Safety-net: if streaming produced text but no URL appeared,
            # append the fallback entry link based on trigger keywords.
            extra_delta = _fallback_entry_delta_if_needed(accumulated)
            if extra_delta:
                yield f"data: {json.dumps({'delta': extra_delta}, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/session_title", response_model=TitleResponse)
async def session_title(req: TitleRequest):
    """
    基于用户的“第一句话”生成一个会话标题（用于左侧历史列表）。
    """
    try:
        if _normalize_language(req.language) == "en-US":
            title_prompt = (
                "You are a conversation title generator. "
                "Generate a concise English title from the user's first message, no more than 6 words. "
                "Output only the title text, with no explanation, numbering, quotes, or asterisks."
            )
        else:
            title_prompt = (
                "你是一个会话标题生成器。"
                "根据用户的第一句话，用不超过 16 个中文字符生成一个简洁标题。"
                "只输出标题文本本身，不要输出任何额外解释、编号或符号（不要加引号、不要加星号）。"
            )
        messages = [
            SystemMessage(content=title_prompt),
            HumanMessage(content=req.message),
        ]
        result = model.invoke(messages)
        raw = result.content if hasattr(result, "content") else str(result)
        # 清洗：去掉首尾空白、去掉行首项目符号、去掉引号等
        raw = raw.strip()
        raw = re.sub(r"^\s*[\*\-\u2022]\s*", "", raw)
        raw = raw.replace('"', "").replace("'", "").strip()
        # 如果模型意外换行，取第一行
        raw = raw.splitlines()[0].strip() if raw else raw
        if not raw:
            raise ValueError("empty title")
        if _normalize_language(req.language) == "en-US" and len(raw.split()) > 6:
            raw = " ".join(raw.split()[:6]).rstrip()
        elif len(raw) > 16:
            raw = raw[:16].rstrip()
        return TitleResponse(title=raw)
    except Exception:
        # 兜底：直接截断用户第一句话
        t = (req.message or "").strip()
        if not t:
            t = "New chat" if _normalize_language(req.language) == "en-US" else "新对话"
        if _normalize_language(req.language) == "en-US" and len(t.split()) > 6:
            t = " ".join(t.split()[:6]).rstrip()
        elif len(t) > 16:
            t = t[:16].rstrip() + "…"
        return TitleResponse(title=t)


@app.post("/api/reload_qa")
async def reload_qa():
    """
    重新加载总 QA（你更新文件后，不想重启服务时用）。
    如果 qa.txt 存在，会先同步生成 qa.json，再从 qa.json 载入内存。
    """
    count = load_qa()
    return {"ok": True, "count": count, "source": QA_JSON_PATH}


@app.post("/api/parse_schedule_command", response_model=ScheduleCommandParseResponse)
async def parse_schedule_command(req: ScheduleCommandParseRequest):
    """
    Parse a personal-schedule command into a small mutation object.
    The browser applies the mutation to localStorage, so this endpoint only
    classifies intent and extracts fields.
    """
    text = (req.text or "").strip()
    fallback = _fallback_parse_schedule_command(text)
    contextual_intent = _is_contextual_schedule_intent(text, req.context)
    if contextual_intent and not fallback.handled:
        fallback = ScheduleCommandParseResponse(
            handled=True,
            action=_detect_schedule_action(text) or "none",
            target=None,
            matchName="",
            item={},
            confidence=0.62,
        )
    if not text or not fallback.handled:
        return fallback

    try:
        context_payload = [
            {"role": str(x.get("role", ""))[:20], "text": str(x.get("text", ""))[:600]}
            for x in (req.context if isinstance(req.context, list) else [])[-3:]
            if isinstance(x, dict)
        ]
        schedule_payload = req.schedule if isinstance(req.schedule, dict) else {}
        if _normalize_language(req.language) == "en-US":
            sys_prompt = (
                "You parse commands for the Personal Schedule board in a local campus assistant.\n"
                "Personal schedule, timetable, course schedule, class schedule, or schedule modification must route here, not to Campus Activities.\n"
                "Use recentContext to resolve vague references such as it, that one, the previous item, delete it, or change it.\n"
                "Extract only the user's intended local schedule edit. Output strict JSON only.\n"
                "Schema: {\"handled\":true,\"action\":\"add|update|delete|open|none\",\"target\":\"course|activity\","
                "\"matchName\":\"existing item name if updating/deleting, otherwise empty\","
                "\"item\":{\"day\":\"mon|tue|wed|thu|fri\",\"name\":\"\",\"start\":\"HH:MM\",\"end\":\"HH:MM\","
                "\"location\":\"\",\"teacher\":\"\",\"intro\":\"\"},\"confidence\":0~1}.\n"
                "Use target=course for classes/timetable/course table/lecture/lab. Use target=activity for personal events or activities placed on the schedule.\n"
                "If fields are missing but the user clearly wants to open or modify Personal Schedule, set action=open or none and confidence above 0.6."
            )
            human = {"text": text, "recentContext": context_payload}
        else:
            sys_prompt = (
                "你负责解析本地网页“个人日程”板块的指令。\n"
                "凡是个人日程、我的日程、日程表、课程表、课表、日程修改，都属于个人日程板块，不属于校园活动板块。\n"
                "请使用 recentContext 解析“它、这个、刚才那个、删了、改了”等省略指代。\n"
                "只抽取用户想对本地日程做的修改，并只输出严格 JSON。\n"
                "格式：{\"handled\":true,\"action\":\"add|update|delete|open|none\",\"target\":\"course|activity\","
                "\"matchName\":\"更新或删除时用于匹配的已有名称，没有则空\","
                "\"item\":{\"day\":\"mon|tue|wed|thu|fri\",\"name\":\"\",\"start\":\"HH:MM\",\"end\":\"HH:MM\","
                "\"location\":\"\",\"teacher\":\"\",\"intro\":\"\"},\"confidence\":0~1}。\n"
                "课程表、课表、课程、上课、lecture、lab、class、course 用 target=course；个人日程里的活动/事项用 target=activity。\n"
                "如果字段不足但用户明显想打开或修改个人日程，action=open 或 none，confidence 大于 0.6。"
            )
            human = {"用户输入": text, "recentContext": context_payload}
        sys_prompt += (
            "\nUse currentSchedule as the authoritative list of existing schedule items. "
            "When the user refers to an item by weekday, time period, or approximate name, "
            "choose the matching existing item and put its exact existing name in matchName. "
            "If exactly one class/activity matches a day and period, treat it as the intended item. "
            "If the user says course, class, timetable, lecture, lab, or includes a course-code-like token, target must be course, not activity. "
            "Only use target=activity when the user clearly refers to activities, events, meetings, or appointments.\n"
        )
        human["currentSchedule"] = schedule_payload
        result = model.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=json.dumps(human, ensure_ascii=False))])
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        parsed = _sanitize_schedule_command_payload(data, fallback)
        if parsed.handled and parsed.confidence >= 0.55:
            return parsed
    except Exception:
        pass
    return fallback


@app.post("/api/module_qa_scan", response_model=ModuleScanResponse)
async def module_qa_scan(req: ModuleScanRequest):
    """
    Scan a module's dedicated QA table and return "pre-generated" dynamic cards
    for:
    - the module itself
    - each provided submodule key

    This endpoint is deterministic: it only scans module/submodule QA tables
    and converts matched entries into UI cards.
    """
    module_key = (req.moduleKey or "").strip()
    if not module_key:
        raise HTTPException(status_code=400, detail="moduleKey required")

    max_cards = max(0, min(int(req.max_cards or 0), 30))
    query_text = (req.queryText or "").strip()

    # global hits from the overall QA table (used to pin top results)
    global_hits = _global_hits_to_cards(query_text, max_cards=max_cards, language=req.language) if query_text else []
    if query_text:
        if global_hits:
            logger.info(
                "QA HIT (module_qa_scan) module=%s query=%s global_hits=%s",
                module_key,
                query_text,
                "; ".join([str(c.title) for c in global_hits[:5]]),
            )
            _qa_terminal_log(
                f"QA HIT (module_qa_scan) module={module_key} query={query_text} global_hits="
                + "; ".join([str(c.title) for c in global_hits[:5]])
            )
        else:
            logger.info("QA MISS (module_qa_scan) module=%s query=%s global_hits=0", module_key, query_text)
            _qa_terminal_log(f"QA MISS (module_qa_scan) module={module_key} query={query_text} global_hits=0")

    module_path = _resolve_qa_file_by_key(module_key)
    module_pairs = _load_qa_from_path(module_path) if module_path else []
    module_cards = _qa_pairs_to_cards(
        module_pairs,
        max_cards=max_cards,
        language=req.language,
        context_key=module_key,
    )
    if query_text:
        module_cards = _score_cards_for_query(module_cards, query_text)
        if module_cards:
            logger.info(
                "QA RANK (module_qa_scan) module=%s query=%s top_cards=%s",
                module_key,
                query_text,
                "; ".join([str(c.title) for c in module_cards[:5]]),
            )
            _qa_terminal_log(
                f"QA RANK (module_qa_scan) module={module_key} query={query_text} top_cards="
                + "; ".join([str(c.title) for c in module_cards[:5]])
            )

    submodules: dict[str, list[DynamicCard]] = {}
    for sk in req.submoduleKeys or []:
        skey = str(sk or "").strip()
        if not skey:
            continue
        spath = _resolve_qa_file_by_key(skey)
        spairs = _load_qa_from_path(spath) if spath else []
        cards = _qa_pairs_to_cards(
            spairs,
            max_cards=max_cards,
            language=req.language,
            context_key=skey,
        )
        submodules[skey] = _score_cards_for_query(cards, query_text) if query_text else cards

    return ModuleScanResponse(moduleKey=module_key, cards=module_cards, submodules=submodules, globalHits=global_hits)


class RewriteCardRequest(BaseModel):
    contextKey: str
    q: str
    a: str
    icon: Optional[str] = None
    language: str = "zh-CN"


class RewriteCardResponse(BaseModel):
    title: str
    desc: str
    details: str
    icon: str


@app.post("/api/rewrite_dynamic_card", response_model=RewriteCardResponse)
async def rewrite_dynamic_card(req: RewriteCardRequest):
    """
    Rewrite ONE QA hit into a declarative, well-formatted module card.
    Triggered only after the user clicks the AI card.
    """
    q = (req.q or "").strip()
    a = (req.a or "").strip()
    context_key = (req.contextKey or "").strip()
    icon = (req.icon or "book").strip() or "book"
    if not q or not a:
        raise HTTPException(status_code=400, detail="q and a required")

    try:
        if _normalize_language(req.language) == "en-US":
            prompt = (
                "You are a campus-life assistant content rewriter. Rewrite the QA hit for UI display.\n"
                "Requirements: polite, concise, declarative English.\n"
                "Hard rules:\n"
                "- Do not copy the q text; do not output Q/A labels or raw bracket lists.\n"
                "- Translate and adapt any Chinese source text into English.\n"
                "- title: a declarative title with a clear subject, concise but complete.\n"
                "- desc: one specific sentence.\n"
                "- details: 4 to 8 actionable bullet points. If it is a process, keep the order clear.\n"
                "- Output strict JSON only: {\"title\":\"...\",\"desc\":\"...\",\"details\":[\"...\",\"...\"]}\n"
            )
        else:
            prompt = (
                "你是校园生活助手的“推荐内容改写器”。请把 QA 命中改写成适合 UI 展示的内容。\n"
                "要求：礼貌但简洁，全部用陈述句。\n"
                "强约束：\n"
                "- 不要复制 q 原句；不要出现“问/答/Q/A/？”；不要输出方括号列表原样。\n"
                "- title：陈述句标题，必须包含明确主语（例如“学校/新生/宿舍/校园卡/海外暑研申请人”等），尽量主谓宾明确、信息完整（不限制字数，但要简洁）。\n"
                "- desc：1 句，具体描述。\n"
                "- details：4~8 条要点。\n"
                "  - 如果是流程/步骤类，请按顺序输出，每条都能独立执行，并体现编号顺序。\n"
                "  - 每条尽量具体（材料/步骤/截止/入口/注意事项）。\n"
                "- 只输出严格 JSON：{\"title\":\"...\",\"desc\":\"...\",\"details\":[\"...\",\"...\"]}\n"
            )
        if _normalize_language(req.language) == "en-US":
            human_content = f"contextKey: {context_key}\nq: {q}\na: {a}"
        else:
            human_content = f"场景key：{context_key}\nq：{q}\na：{a}"
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=human_content),
        ]
        result = model.invoke(messages)
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        title = str(data.get("title", "")).strip()
        desc = str(data.get("desc", "")).strip()
        details_list = data.get("details", [])
        if isinstance(details_list, list):
            details = "\n".join([str(x).strip() for x in details_list if str(x).strip()])
        else:
            details = str(details_list or "").strip()

        if not title:
            title = q.replace("？", "").replace("?", "").strip()
        if not desc:
            desc = (a.splitlines()[0] if a else "").strip()
        if not details:
            details = a.strip()

        if _normalize_language(req.language) == "en-US":
            title, desc, details = _enforce_english_detail(title, desc, details, context_key=context_key)

        # Ensure links from original QA are preserved and briefly listed
        details = _append_link_briefs(details, language=req.language)

        return RewriteCardResponse(title=title, desc=desc, details=details, icon=icon)
    except Exception:
        # fallback: keep deterministic content but remove question marks
        title = q.replace("？", "").replace("?", "").strip()
        desc = (a.splitlines()[0] if a else "").strip()
        details_src = a.strip()
        if _normalize_language(req.language) == "en-US":
            title, desc, details_src = _enforce_english_detail(title, desc, details_src, context_key=context_key)
        details = _append_link_briefs(details_src, language=req.language)
        return RewriteCardResponse(title=title, desc=desc, details=details, icon=icon)


class RewriteListItem(BaseModel):
    q: str
    a: str
    icon: Optional[str] = None


class RewriteListRequest(BaseModel):
    contextKey: str
    items: list[RewriteListItem]
    language: str = "zh-CN"


class RewriteListItemOut(BaseModel):
    title: str
    desc: str
    icon: str


class RewriteListResponse(BaseModel):
    items: list[RewriteListItemOut]


@app.post("/api/rewrite_dynamic_list", response_model=RewriteListResponse)
async def rewrite_dynamic_list(req: RewriteListRequest):
    """
    Rewrite list titles/descriptions for AI cards (declarative, concise, complete).
    """
    context_key = (req.contextKey or "").strip()
    items = req.items or []
    if not items:
        return RewriteListResponse(items=[])

    # Cap list size for latency/cost control
    items = items[:12]

    def _heuristic_title(q_raw: str, a_raw: str, language: str = "zh-CN") -> str:
        q0 = (q_raw or "").strip()
        a0 = (a_raw or "").strip()
        ql = q0.lower()
        is_en = _normalize_language(language) == "en-US"
        # 字段型：时间/地点/入口/费用等
        if re.search(r"(几号|什么时候|何时|时间|日期|几点|开学|报到|when|date|time|start|begin|semester|enroll|registration)", q0, re.IGNORECASE):
            if ("报到" in q0) or re.search(r"(registration|enroll|check[\-\s]?in)", q0, re.IGNORECASE):
                return "New Student Registration Time" if is_en else "新生报到时间"
            if ("开学" in q0) or re.search(r"(semester|term|class starts?|start date)", q0, re.IGNORECASE):
                return "Semester Start Time" if is_en else "学校开学时间"
            return "Key Timeline Information" if is_en else "相关时间信息"
        if re.search(r"(截止|ddl|deadline|due)", q0, re.IGNORECASE):
            return "Application Deadline" if is_en else "申请截止时间"
        if re.search(r"(地点|地址|在哪|哪里|位置|where|location|address|office)", q0, re.IGNORECASE):
            return "Service Location" if is_en else "相关办理地点"
        if re.search(r"(费用|多少钱|价格|收费|fee|cost|price|payment)", q0, re.IGNORECASE):
            return "Fees and Costs" if is_en else "相关费用信息"
        if re.search(r"(电话|联系方式|邮箱|联系|contact|email|phone)", q0, re.IGNORECASE):
            return "Contact Information" if is_en else "相关联系方式"
        if re.search(r"(入口|链接|网站|官网|平台|系统|link|portal|website|site|system)", q0, re.IGNORECASE):
            return "Official Access Link" if is_en else "相关办理入口"
        # 主题型：提炼关键词
        if "暑研" in q0 or "暑期科研" in q0 or "summer research" in ql:
            return "Overseas Summer Research Application" if is_en else "海外暑研申请要点"
        if "校园卡" in q0:
            return "Campus Card Application and Replacement" if is_en else "校园卡办理与补办要点"
        if "宿舍" in q0:
            return "Dormitory Service Essentials" if is_en else "宿舍办理要点"
        if "选课" in q0:
            return "Course Registration Essentials" if is_en else "选课操作要点"
        # 兜底：取答案首行前若干字
        first = (a0.splitlines()[0] if a0 else "").strip()
        first = re.sub(r"^[\-\*\d\.\)\s]+", "", first)
        first = first.replace("：", "").replace(":", "").strip()
        if first:
            if len(first) > 40:
                return first[:40].rstrip("，。；;") + "…"
            return first.rstrip("，。；;")
        return "Recommended Information" if is_en else "推荐内容"

    try:
        if _normalize_language(req.language) == "en-US":
            prompt = (
                "You are a campus-life assistant card-list copy rewriter.\n"
                "Input contains QA hits. Rewrite each item for compact card-list display.\n"
                "Style: polite, very concise, high information density, declarative English.\n"
                "Hard rules:\n"
                "- Translate and adapt any Chinese source text into English.\n"
                "- Do not copy the question text; do not output Q/A labels.\n"
                "- title: clear subject, factual and specific.\n"
                "- desc: one-sentence summary with the most important time/place/link/material/deadline/cost when available.\n"
                "- Output a strict JSON array with the same length as input: [{\"title\":\"...\",\"desc\":\"...\"}, ...]\n"
                "- Do not output extra text."
            )
        else:
            prompt = (
                "你是校园生活助手的“推荐列表文案改写器”。\n"
                "输入是若干条 QA 命中（q 问句 + a 答案）。请把每条改写成适合卡片列表展示的文案。\n"
                "总体风格：礼貌但极简、信息密度高、全部用陈述句。\n"
                "强约束：\n"
                "- 严禁疑问句：不要出现“？/?/吗/呢/是否/能否/可以吗/怎么/如何/请问”。\n"
                "- 不要把 q 原句直接改写/同义复述；不要出现“问/答/Q/A”。\n"
                "- title：必须包含明确主语（例如“学校/新生/宿舍/校园卡/海外暑研申请人”等）。\n"
                "- title：优先输出“事实字段型标题”，直接点题（例：学校开学时间、新生报到时间、宿舍办理地点、校园卡补办入口、海外暑研申请材料）。\n"
                "  - 允许使用“指南/流程/清单”，但必须是“提炼后的陈述句标题”，不能是“把问句去掉问号再加后缀”。\n"
                "- desc：1 句摘要，尽量包含最关键的 1~2 个信息（时间/地点/入口/材料/截止/费用等）。\n"
                "- 只输出严格 JSON 数组，长度必须与输入一致：[{\"title\":\"...\",\"desc\":\"...\"}, ...]\n"
                "- 不要输出任何额外文字。"
            )
        payload = [{"q": (it.q or "").strip(), "a": (it.a or "").strip()} for it in items]
        human_payload = (
            f"contextKey: {context_key}\ninput: {json.dumps(payload, ensure_ascii=False)}"
            if _normalize_language(req.language) == "en-US"
            else f"场景key：{context_key}\n输入：{json.dumps(payload, ensure_ascii=False)}"
        )
        result = model.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=human_payload),
            ]
        )
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\[[\s\S]*\]", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("not a list")

        out: list[RewriteListItemOut] = []
        for i, it in enumerate(items):
            base_icon = (it.icon or "book").strip() or "book"
            row = data[i] if i < len(data) else {}
            title = str((row or {}).get("title", "")).strip()
            desc = str((row or {}).get("desc", "")).strip()
            # Fallbacks and sanitization
            q_raw = (it.q or "").strip()
            a_raw = (it.a or "").strip()
            if not title:
                title = _heuristic_title(q_raw, a_raw, req.language)
            if not desc:
                desc = ((a_raw.splitlines()[0] if a_raw else "")).strip()
            title = title.replace("？", "").replace("?", "").strip()
            desc = desc.replace("？", "").replace("?", "").strip()
            # 反“问句去问号+后缀”的糊弄：标题与问句过像则强制改为“提炼标题”
            q_slim = re.sub(r"\s+", "", q_raw.replace("？", "").replace("?", ""))
            t_slim = re.sub(r"\s+", "", title)
            if q_slim and (t_slim == q_slim or q_slim in t_slim or t_slim in q_slim):
                title = _heuristic_title(q_raw, a_raw, req.language)

            # 对字段型问法，避免结尾乱加“指南/攻略/教程”（流程可保留）
            is_en = _normalize_language(req.language) == "en-US"
            if re.search(
                r"(几号|什么时候|何时|时间|日期|几点|开学|报到|截止|地点|地址|在哪|电话|联系方式|费用|多少钱|when|date|time|deadline|where|location|contact|fee|cost)",
                q_raw,
                re.IGNORECASE,
            ):
                title = re.sub(r"(指南|攻略|教程|guide|tips?|tutorial)$", "", title, flags=re.IGNORECASE).strip()
                if re.search(r"(开学|semester|term|class starts?|start date)", q_raw, re.IGNORECASE) and (("时间" not in title) if not is_en else ("time" not in title.lower() and "date" not in title.lower())):
                    title = "Semester Start Time" if is_en else "学校开学时间"
                elif re.search(r"(报到|registration|enroll|check[\-\s]?in)", q_raw, re.IGNORECASE) and (("时间" not in title) if not is_en else ("time" not in title.lower() and "date" not in title.lower())):
                    title = "New Student Registration Time" if is_en else "新生报到时间"
                elif re.search(r"(截止|ddl|deadline|due)", q_raw, re.IGNORECASE) and (("截止" not in title) if not is_en else ("deadline" not in title.lower() and "due" not in title.lower())):
                    title = "Application Deadline" if is_en else "申请截止时间"
                elif re.search(r"(地点|地址|在哪|where|location|address)", q_raw, re.IGNORECASE):
                    if (("地点" not in title and "地址" not in title) if not is_en else ("location" not in title.lower() and "address" not in title.lower())):
                        title = "Service Location" if is_en else "相关办理地点"
            out.append(RewriteListItemOut(title=title, desc=desc, icon=base_icon))

        if _normalize_language(req.language) == "en-US":
            normalized = _enforce_english_list(
                [{"title": x.title, "desc": x.desc, "icon": x.icon} for x in out],
                context_key=context_key,
            )
            out = [
                RewriteListItemOut(
                    title=str((it or {}).get("title", "")).strip() or out[i].title,
                    desc=str((it or {}).get("desc", "")).strip() or out[i].desc,
                    icon=str((it or {}).get("icon", out[i].icon)).strip() or out[i].icon,
                )
                for i, it in enumerate(normalized[: len(out)])
            ] + out[len(normalized) :]
        return RewriteListResponse(items=out)

    except Exception:
        out = []
        for it in items:
            base_icon = (it.icon or "book").strip() or "book"
            q_raw = (it.q or "").strip()
            a_raw = (it.a or "").strip()
            title = _heuristic_title(q_raw, a_raw, req.language)
            desc = ((a_raw.splitlines()[0] if a_raw else "")).strip()
            out.append(RewriteListItemOut(title=title, desc=desc, icon=base_icon))
        if _normalize_language(req.language) == "en-US":
            normalized = _enforce_english_list(
                [{"title": x.title, "desc": x.desc, "icon": x.icon} for x in out],
                context_key=context_key,
            )
            out = [
                RewriteListItemOut(
                    title=str((it or {}).get("title", "")).strip() or out[i].title,
                    desc=str((it or {}).get("desc", "")).strip() or out[i].desc,
                    icon=str((it or {}).get("icon", out[i].icon)).strip() or out[i].icon,
                )
                for i, it in enumerate(normalized[: len(out)])
            ] + out[len(normalized) :]
        return RewriteListResponse(items=out)


class RouteBoardRequest(BaseModel):
    text: str
    language: str = "zh-CN"


class RouteBoardResponse(BaseModel):
    moduleKey: Optional[str] = None
    pageKey: Optional[str] = None
    confidence: float = 0.0


@app.post("/api/route_board", response_model=RouteBoardResponse)
async def route_board(req: RouteBoardRequest):
    """
    Given a user question, decide whether it matches one of our right-sidebar boards.
    If matches a sub-board (pageKey), return both moduleKey and pageKey.
    """
    text = (req.text or "").strip()
    if not text:
        return RouteBoardResponse(moduleKey=None, pageKey=None, confidence=0.0)

    allowed_modules = ["study", "intern", "campus", "life", "research", "schedule"]
    allowed_pages = {
        "study": [
            "study_my",
            "study_course",
            "study_course_calc",
            "study_course_stats",
            "study_course_cs",
            "study_course_econ",
            "study_english",
            "study_english_vocab",
            "study_english_reading",
            "study_english_writing",
            "study_english_speaking",
            "study_plan",
            "study_writing",
            "study_methods",
        ],
        "intern": ["intern_resume", "intern_interview", "intern_network", "intern_plan"],
        "campus": ["campus_club", "campus_event", "campus_volunteer", "campus_leadership"],
        "life": ["life_dorm", "life_health", "life_food", "life_emergency"],
        "research": ["research_topic", "research_paper", "research_method", "research_writing"],
        "schedule": ["schedule_home"],
    }

    # quick heuristic fallback
    def _heuristic(t: str) -> RouteBoardResponse:
        if _is_personal_schedule_intent(t):
            return RouteBoardResponse(moduleKey="schedule", pageKey=None, confidence=0.82)
        if any(k in t for k in ["宿舍", "报修", "吃饭", "食堂", "医疗", "生活费", "紧急", "生病", "医保"]):
            return RouteBoardResponse(moduleKey="life", pageKey=None, confidence=0.62)
        if any(k in t for k in ["简历", "面试", "实习", "投递", "内推", "offer", "秋招", "春招"]):
            return RouteBoardResponse(moduleKey="intern", pageKey=None, confidence=0.62)
        if any(k in t for k in ["社团", "活动", "志愿", "讲座", "比赛", "迎新", "校园活动", "报到", "开学", "新生", "注册"]):
            return RouteBoardResponse(moduleKey="campus", pageKey=None, confidence=0.62)
        if any(k in t for k in ["论文", "实验", "科研", "暑研", "套磁", "文献", "投稿"]):
            return RouteBoardResponse(moduleKey="research", pageKey=None, confidence=0.62)
        if any(k in t for k in ["选课", "课程", "考试", "复习", "英语", "雅思", "托福", "写作", "口语"]):
            return RouteBoardResponse(moduleKey="study", pageKey=None, confidence=0.62)
        return RouteBoardResponse(moduleKey=None, pageKey=None, confidence=0.0)

    # 关键词优先：不等模型路由（更快更稳）
    h = _heuristic(text)
    if h and h.moduleKey:
        return h

    try:
        sys_prompt = (
            "你是一个路由器，负责把用户问题路由到右侧栏“板块服务”。\n"
            "可选 moduleKey: study/intern/campus/life/research/schedule 或 null。\n"
            "可选 pageKey: 必须是该 moduleKey 下允许的子板块 key，否则为 null。\n"
            "规则：\n"
            "- 只有明显属于某板块时才返回；不确定就返回 null。\n"
            "- 如果能命中子板块就返回 pageKey，否则只返回 moduleKey。\n"
            "- 个人日程、我的日程、课程表、课表、日程修改、personal schedule、timetable、class schedule 必须返回 schedule，不要返回 campus。\n"
            "- 校园活动只用于社团、志愿、讲座、比赛、活动策划等公共校园活动内容；个人日程里的活动增删改属于 schedule。\n"
            "- 只输出严格 JSON：{\"moduleKey\":...,\"pageKey\":...,\"confidence\":0~1}，不要额外文字。"
        )
        # provide allowed list for grounding
        human = {
            "question": text,
            "allowed": {"modules": allowed_modules, "pages": allowed_pages},
        }
        result = model.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=json.dumps(human, ensure_ascii=False))])
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        module_key = data.get("moduleKey", None)
        page_key = data.get("pageKey", None)
        conf = float(data.get("confidence", 0.0) or 0.0)
        if module_key is None:
            return RouteBoardResponse(moduleKey=None, pageKey=None, confidence=max(0.0, min(conf, 1.0)))
        if module_key not in allowed_modules:
            return _heuristic(text)
        if page_key is not None:
            if page_key not in allowed_pages.get(module_key, []):
                page_key = None
        return RouteBoardResponse(moduleKey=module_key, pageKey=page_key, confidence=max(0.0, min(conf, 1.0)))
    except Exception:
        return _heuristic(text)


if __name__ == "__main__":
    # 方便直接 python graph.py 启动开发服务器
    import uvicorn
    import os
    import socket

    # Windows + reload=True 会起 reloader 子进程，日志容易“跑到别的窗口/进程”里。
    # 这里默认关闭 reload，保证命中日志稳定输出到当前 IDE 终端。
    port_env = (os.environ.get("PORT") or "").strip()
    base_port = int(port_env) if port_env.isdigit() else 8000

    chosen_port: int | None = None
    last_err: OSError | None = None
    for port in range(base_port, base_port + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
            chosen_port = port
            break
        except OSError as e:
            last_err = e
            continue

    if chosen_port is None:
        raise last_err or OSError("No free port found")

    _qa_terminal_log(f"[server] starting on http://127.0.0.1:{chosen_port} (base PORT={base_port})")
    uvicorn.run("graph:app", host="0.0.0.0", port=chosen_port, reload=False, log_level="info")

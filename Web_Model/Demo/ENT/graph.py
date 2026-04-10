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
from langchain_core.messages import SystemMessage, HumanMessage
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


class ChatResponse(BaseModel):
    reply: str


class TitleRequest(BaseModel):
    message: str


class TitleResponse(BaseModel):
    title: str

class ModuleScanRequest(BaseModel):
    moduleKey: str
    submoduleKeys: list[str] = []
    max_cards: int = 8
    queryText: str = ""


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


class ModuleScanResponse(BaseModel):
    moduleKey: str
    cards: list[DynamicCard]
    submodules: dict[str, list[DynamicCard]]
    globalHits: list[DynamicCard] = []


# ✅ 创建模型（DeepSeek）
model = ChatDeepSeek(model="deepseek-chat")

# ✅ 西浦学生助手系统提示词
SYSTEM_PROMPT = (
    "你是“西浦生活助手”，一名面向西交利物浦大学（XJTLU）学生的智能助手，"
    "主要帮助新生和在校生解决与西浦学习、生活相关的问题。"
    "严禁使用表情符号、颜文字、特殊符号（例如✅❌⭐️•等），不要输出任何 emoji。\n"
    "回答要求：\n"
    "1）使用自然、简洁的简体中文，礼貌热情，不要写成公文或宣传稿；\n"
    "2）优先从西浦学生的视角给出具体、可执行的建议，例如时间规划、课程学习方法、校园资源使用、新生适应等；\n"
    "3）对不确定的校内规定或可能变动的信息，要提醒“具体以学校官方最新通知为准”；\n"
    "4）可以适当鼓励和共情，但不要夸张，也不要频繁重复类似“很高兴为你服务”之类客套话。"
    "5）使用用户的同种语言来回答。"
)


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


def _append_link_briefs(text: str, max_links: int = 6) -> str:
    """
    If `text` contains URL(s), append a short "相关链接" section with brief hints.
    """
    t = str(text or "").strip()
    if not t:
        return ""
    # avoid repeated appends (idempotent enough)
    if "相关链接" in t and "http" in t:
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

    lines = ["", "", "相关链接（可点击打开）："]
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


def _qa_pairs_to_cards(pairs: list[dict], max_cards: int) -> list[DynamicCard]:
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
                details=_append_link_briefs(a),
            )
        )
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


def _global_hits_to_cards(question: str, max_cards: int) -> list[DynamicCard]:
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
        elif any(k in text for k in ["活动", "社团", "志愿", "讲座", "比赛", "校园"]):
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
        cards.append(DynamicCard(title=q, desc=desc, icon=icon, action=q, details=_append_link_briefs(a)))
    return cards[: max(0, int(max_cards or 0))]


def _best_module_qa_match(question: str, cutoff: float = 0.70) -> Optional[dict]:
    """
    Search module/submodule QA tables for the best matching question.
    Returns {moduleKey,pageKey,q,a,icon,score} or None.
    """
    qn = (question or "").strip()
    if not qn:
        return None
    # normalize: drop school name tokens so "新生报到流程是什么" can match
    qn_norm = re.sub(r"(西交利物浦|西浦|XJTLU|xjtlu)", "", qn, flags=re.IGNORECASE).strip()
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
        qs_norm = [re.sub(r"(西交利物浦|西浦|XJTLU|xjtlu)", "", x, flags=re.IGNORECASE).strip() for x in qs]
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
        system_prompt = SYSTEM_PROMPT
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
            if any(k in msg for k in ["报到", "迎新", "开学", "注册", "新生报到", "新生注册"]):
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

            system_prompt = SYSTEM_PROMPT
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
                        "- 用礼貌、简洁的陈述句回答\n"
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
                for ch in reply:
                    produced_any = True
                    accumulated += ch
                    yield f"data: {json.dumps({'delta': ch}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.008)

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
        if len(raw) > 16:
            raw = raw[:16].rstrip()
        return TitleResponse(title=raw)
    except Exception:
        # 兜底：直接截断用户第一句话
        t = (req.message or "").strip()
        if not t:
            t = "新对话"
        if len(t) > 16:
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
    global_hits = _global_hits_to_cards(query_text, max_cards=max_cards) if query_text else []
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
    module_cards = _qa_pairs_to_cards(module_pairs, max_cards=max_cards)
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
        cards = _qa_pairs_to_cards(spairs, max_cards=max_cards)
        submodules[skey] = _score_cards_for_query(cards, query_text) if query_text else cards

    return ModuleScanResponse(moduleKey=module_key, cards=module_cards, submodules=submodules, globalHits=global_hits)


class RewriteCardRequest(BaseModel):
    contextKey: str
    q: str
    a: str
    icon: Optional[str] = None


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
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=f"场景key：{context_key}\nq：{q}\na：{a}"),
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

        # Ensure links from original QA are preserved and briefly listed
        details = _append_link_briefs(details)

        return RewriteCardResponse(title=title, desc=desc, details=details, icon=icon)
    except Exception:
        # fallback: keep deterministic content but remove question marks
        title = q.replace("？", "").replace("?", "").strip()
        desc = (a.splitlines()[0] if a else "").strip()
        details = _append_link_briefs(a.strip())
        return RewriteCardResponse(title=title, desc=desc, details=details, icon=icon)


class RewriteListItem(BaseModel):
    q: str
    a: str
    icon: Optional[str] = None


class RewriteListRequest(BaseModel):
    contextKey: str
    items: list[RewriteListItem]


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

    def _heuristic_title(q_raw: str, a_raw: str) -> str:
        q0 = (q_raw or "").strip()
        a0 = (a_raw or "").strip()
        ql = q0.lower()
        # 字段型：时间/地点/入口/费用等
        if re.search(r"(几号|什么时候|何时|时间|日期|几点|开学|报到)", q0):
            if "报到" in q0:
                return "新生报到时间"
            if "开学" in q0:
                return "学校开学时间"
            return "相关时间信息"
        if re.search(r"(截止|ddl)", q0, re.IGNORECASE):
            return "申请截止时间"
        if re.search(r"(地点|地址|在哪|哪里|位置)", q0):
            return "相关办理地点"
        if re.search(r"(费用|多少钱|价格|收费)", q0):
            return "相关费用信息"
        if re.search(r"(电话|联系方式|邮箱|联系)", q0):
            return "相关联系方式"
        if re.search(r"(入口|链接|网站|官网|平台|系统)", q0):
            return "相关办理入口"
        # 主题型：提炼关键词
        if "暑研" in q0 or "暑期科研" in q0 or "summer research" in ql:
            return "海外暑研申请要点"
        if "校园卡" in q0:
            return "校园卡办理与补办要点"
        if "宿舍" in q0:
            return "宿舍办理要点"
        if "选课" in q0:
            return "选课操作要点"
        # 兜底：取答案首行前若干字
        first = (a0.splitlines()[0] if a0 else "").strip()
        first = re.sub(r"^[\-\*\d\.\)\s]+", "", first)
        first = first.replace("：", "").replace(":", "").strip()
        if first:
            if len(first) > 40:
                return first[:40].rstrip("，。；;") + "…"
            return first.rstrip("，。；;")
        return "推荐内容"

    try:
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
        result = model.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=f"场景key：{context_key}\n输入：{json.dumps(payload, ensure_ascii=False)}"),
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
                title = _heuristic_title(q_raw, a_raw)
            if not desc:
                desc = ((a_raw.splitlines()[0] if a_raw else "")).strip()
            title = title.replace("？", "").replace("?", "").strip()
            desc = desc.replace("？", "").replace("?", "").strip()
            # 反“问句去问号+后缀”的糊弄：标题与问句过像则强制改为“提炼标题”
            q_slim = re.sub(r"\s+", "", q_raw.replace("？", "").replace("?", ""))
            t_slim = re.sub(r"\s+", "", title)
            if q_slim and (t_slim == q_slim or q_slim in t_slim or t_slim in q_slim):
                title = _heuristic_title(q_raw, a_raw)

            # 对字段型问法，避免结尾乱加“指南/攻略/教程”（流程可保留）
            if re.search(r"(几号|什么时候|何时|时间|日期|几点|开学|报到|截止|地点|地址|在哪|电话|联系方式|费用|多少钱)", q_raw):
                title = re.sub(r"(指南|攻略|教程)$", "", title).strip()
                if re.search(r"(开学)", q_raw) and ("时间" not in title):
                    title = "学校开学时间"
                elif re.search(r"(报到)", q_raw) and ("时间" not in title):
                    title = "新生报到时间"
                elif re.search(r"(截止)", q_raw) and ("截止" not in title):
                    title = "申请截止时间"
                elif re.search(r"(地点|地址|在哪)", q_raw) and ("地点" not in title and "地址" not in title):
                    title = "相关办理地点"
            out.append(RewriteListItemOut(title=title, desc=desc, icon=base_icon))

        return RewriteListResponse(items=out)

    except Exception:
        out = []
        for it in items:
            base_icon = (it.icon or "book").strip() or "book"
            q_raw = (it.q or "").strip()
            a_raw = (it.a or "").strip()
            title = _heuristic_title(q_raw, a_raw)
            desc = ((a_raw.splitlines()[0] if a_raw else "")).strip()
            out.append(RewriteListItemOut(title=title, desc=desc, icon=base_icon))
        return RewriteListResponse(items=out)


class RouteBoardRequest(BaseModel):
    text: str


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

    allowed_modules = ["study", "intern", "campus", "life", "research"]
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

    # quick heuristic fallback
    def _heuristic(t: str) -> RouteBoardResponse:
        tl = t.lower()
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
            "可选 moduleKey: study/intern/campus/life/research 或 null。\n"
            "可选 pageKey: 必须是该 moduleKey 下允许的子板块 key，否则为 null。\n"
            "规则：\n"
            "- 只有明显属于某板块时才返回；不确定就返回 null。\n"
            "- 如果能命中子板块就返回 pageKey，否则只返回 moduleKey。\n"
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
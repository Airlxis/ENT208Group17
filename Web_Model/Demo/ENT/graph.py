import os
import json
import difflib
import re
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_deepseek import ChatDeepSeek
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel

# 加载环境变量
load_dotenv(override=True)


# ======================
# 语言模型（后端核心）
# ======================

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


# ✅ 创建模型（DeepSeek）
model = ChatDeepSeek(model="deepseek-chat")

# ✅ 西浦学生助手系统提示词
SYSTEM_PROMPT = (
    "你是“西浦生活助手”，一名面向西交利物浦大学（XJTLU）学生的智能助手，"
    "主要帮助新生和在校生解决与西浦学习、生活相关的问题。"
    "不使用表情包"
    "回答要求：\n"
    "1）使用自然、简洁的简体中文，礼貌热情，不要写成公文或宣传稿；\n"
    "2）优先从西浦学生的视角给出具体、可执行的建议，例如时间规划、课程学习方法、校园资源使用、新生适应等；\n"
    "3）对不确定的校内规定或可能变动的信息，要提醒“具体以学校官方最新通知为准”；\n"
    "4）可以适当鼓励和共情，但不要夸张，也不要频繁重复类似“很高兴为你服务”之类客套话。"
)

# ======================
# 本地 QA 表（优先命中）
# ======================

QA_JSON_PATH = os.path.join(os.path.dirname(__file__), "qa.json")
QA_TXT_PATH = os.path.join(os.path.dirname(__file__), "qa.txt")
_QA_CACHE = []


def load_qa() -> int:
    """
    加载问答表到内存。
    支持两种格式：
    1) qa.json：[{ "q": "...", "a": "..." }, ...]
    2) qa.txt：按“问题一行 + 回答一行 + 空一行”循环
    """
    global _QA_CACHE
    cleaned = []

    # 1) 优先加载 qa.json（更规范）
    if os.path.exists(QA_JSON_PATH):
        with open(QA_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("q", "")).strip()
                a = str(item.get("a", "")).strip()
                if q and a:
                    cleaned.append({"q": q, "a": a})
        _QA_CACHE = cleaned
        return len(_QA_CACHE)

    # 2) 否则加载 qa.txt（更适合从 Word 直接粘贴）
    if os.path.exists(QA_TXT_PATH):
        with open(QA_TXT_PATH, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n").strip() for ln in f.readlines()]
        # 去掉首尾空行
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()

        i = 0
        while i < len(lines):
            if not lines[i]:
                i += 1
                continue
            q = lines[i].strip()
            # 允许在问题前加 "Q1" / "Q1." / "Q1：" 等前缀
            q = re.sub(r"^\s*Q\d+\s*[\.\:：、\-]?\s*", "", q, flags=re.IGNORECASE)
            a = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if q and a:
                cleaned.append({"q": q, "a": a})
            i += 3  # 跳过：问题行、回答行、空行（空行不存在也没关系）

        _QA_CACHE = cleaned
        return len(_QA_CACHE)

    _QA_CACHE = []
    return 0


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
        # 1) 先命中 QA（命中则直接返回，不走模型，稳定且便宜）
        hit = qa_match(req.message)
        if hit:
            return ChatResponse(reply=hit)

        # 2) 未命中则走模型兜底
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=req.message),
        ]
        result = model.invoke(messages)
        reply = result.content if hasattr(result, "content") else str(result)
        return ChatResponse(reply=reply)
    except Exception as e:
        # 统一把下游模型错误包装成 502，让前端给出清晰提示
        raise HTTPException(status_code=502, detail=f"调用模型失败：{e}")


@app.post("/api/reload_qa")
async def reload_qa():
    """
    重新加载 qa.json（你更新文件后，不想重启服务时用）。
    """
    count = load_qa()
    source = QA_JSON_PATH if os.path.exists(QA_JSON_PATH) else QA_TXT_PATH
    return {"ok": True, "count": count, "source": source}


if __name__ == "__main__":
    # 方便直接 python graph.py 启动开发服务器
    import uvicorn

    uvicorn.run("graph:app", host="0.0.0.0", port=8000, reload=True)
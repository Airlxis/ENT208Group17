# CampusAI 🤖🎓

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-API-1e3a8a.svg)](https://deepseek.com)

An intelligent conversational assistant for university students, featuring a responsive web interface powered by Large Language Models (DeepSeek) with intelligent caching for instant responses.

[Demo](#) • [Documentation](#api-documentation) • [Deployment](#deployment)

![4e5bc22cc90754448bcc8f290668b85f](https://github.com/user-attachments/assets/f6ccd32c-a2eb-4cc4-b15d-93af38e91939)
---

## ✨ Features

### 💬 Natural Language Interface
- **Responsive Web Chat**: Clean, mobile-friendly interface served via FastAPI
- **Context-Aware Conversations**: Maintains dialogue flow for follow-up questions
- **Sub-100ms Cached Responses**: Instant answers for common queries via local knowledge base
- **AI-Powered Fallback**: DeepSeek LLM integration for complex, unstructured questions

### ⚡ Hybrid Response Architecture
| Layer | Response Time | Use Case |
|-------|--------------|----------|
| **Exact Match** | <50ms | Direct FAQ lookups (policies, procedures) |
| **Fuzzy Match** | <100ms | Similar question detection (difflib, 72% similarity) |
| **LLM API** | 1-2s | Complex reasoning, personalized guidance |

### 🔧 Production-Ready Backend
- **FastAPI (ASGI)**: High-performance async API with automatic OpenAPI documentation
- **CORS Enabled**: Ready for cross-domain deployment (web apps, mobile clients)
- **Hot Reload**: Update knowledge base without server restart (`/api/reload_qa`)
- **Error Resilience**: Graceful degradation to cached content during API outages

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Git
- DeepSeek API key ([Get one here](https://platform.deepseek.com))

### Installation

```bash
# Clone repository
git clone https://github.com/Airlxis/ENT208Group17.git
cd ENT208Group17

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and add your DEEPSEEK_API_KEY
```

### Run Development Server

```bash
python graph.py
# OR
uvicorn graph:app --host 0.0.0.0 --port 8000 --reload
```

Access the chat interface at `http://localhost:8000`

### Environment Variables

```bash
# Required
DEEPSEEK_API_KEY=your_actual_api_key_here

# Optional: Campus network proxy
HTTP_PROXY=http://proxy.university.edu:8080
```

### Verify Installation

Open browser to:
- Chat interface: `http://localhost:8000`
- API docs: `http://localhost:8000/docs`

Test with query: "library hours"

### Troubleshooting

| Problem | Likely Cause | Solution |
|---------|-------------|----------|
| `ModuleNotFoundError: No module named 'fastapi'` | Virtual environment not activated or dependencies not installed | Run `pip install -r requirements.txt` and ensure you see `(venv)` in your terminal prompt before starting the server |
| `KeyError: 'DEEPSEEK_API_KEY'` or server fails to start | Environment variables not loaded | Check that `.env` file exists in project root (not `.env.example`) and contains `DEEPSEEK_API_KEY=sk-...`. Restart terminal after editing |
| `Connection timeout` when asking questions | DeepSeek API blocked by campus firewall or missing proxy | Add `HTTP_PROXY=http://your-campus-proxy:port` to `.env` file, or switch to mobile hotspot to verify API connectivity |
| Frontend loads but shows "Connection Error" | Backend not running or CORS misconfiguration | Verify backend is running on port 8000 with `uvicorn graph:app --host 0.0.0.0`. Check browser console for CORS errors—ensure you're accessing via `localhost:8000`, not `127.0.0.1:8000` |


---

## 🏗️ Architecture

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   Web Client    │      │   FastAPI Server │      │   DeepSeek API  │
│  (Browser/App)  │◄────►│   (Python 3.11)  │◄────►│   (LLM Cloud)   │
└─────────────────┘      └──────────────────┘      └─────────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │   Knowledge Base │
                       │  (JSON/TXT Cache)│
                       └──────────────────┘
```

**Key Components:**
- **Intent Router**: Smart query classification (exact → fuzzy → LLM)
- **Dual-Mode Deployment**: Supports both campus intranet (proxied) and public cloud hosting
- **Modular Data Layer**: Pluggable connectors for official campus systems (academic databases, facility booking APIs)

---

## 📚 API Documentation

Once running, view interactive docs at `http://localhost:8000/docs` (Swagger UI)

### Core Endpoints

#### `POST /api/chat`
Main conversation endpoint.

**Request:**
```json
{
  "message": "How do I book the badminton court?"
}
```

**Response:**
```json
{
  "reply": "You can book sports facilities through the XJTLU Sports Center website. Would you like me to walk you through the steps?"
}
```

**Logic Flow:**
1. Checks QA cache for exact/fuzzy matches (cutoff: 0.72)
2. Returns cached response if found
3. Falls back to DeepSeek API with system prompt context
4. Returns structured error (502) if upstream fails

#### `POST /api/reload_qa`
Reload knowledge base without restarting server.

**Response:**
```json
{
  "ok": true,
  "count": 100,
  "source": "qa.json"
}
```

---

## ⚙️ Configuration

### Environment Variables
```env
DEEPSEEK_API_KEY=your_api_key_here
# Optional: Proxy settings for campus network
HTTP_PROXY=http://proxy.university.edu:8080
```

### Knowledge Base Format

Supports two formats for flexibility:

**Structured (qa.json):**
```json
[
  {
    "q": "When is the library open?",
    "a": "The library is open 8AM-10PM on weekdays..."
  }
]
```

**Human-readable (qa.txt):**
```text
Q1 When is the library open?
The library is open 8AM-10PM on weekdays...

Q2 How do I reset my password?
Visit the IT portal and click...
```

---

## 🌐 Deployment

### Docker (Recommended)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "graph:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Cloud Platforms
- **AWS/Azure/Aliyun**: Containerized deployment with environment variable injection
- **Campus Edge Server**: Run within university intranet for direct system integration
- **Vercel/Railway**: Serverless-compatible with external database for QA cache

---

## 🗺️ Roadmap

- [ ] **Vector Database Integration**: Semantic search via embeddings (RAG)
- [ ] **Authentication**: JWT-based student login integration
- [ ] **Real-time Data**: Connectors for live academic schedules and facility availability
- [ ] **Mobile App**: React Native client with push notifications
- [ ] **Local LLM Fallback**: Qwen/ChatGLM support for offline intranet deployment

---

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with ❤️ for XJTLU students**
=======
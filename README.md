<<<<<<< HEAD
# CampusAI 🤖🎓

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-API-1e3a8a.svg)](https://deepseek.com)

An intelligent conversational assistant for university students, featuring a responsive web interface powered by Large Language Models (DeepSeek) with intelligent caching for instant responses.

[Demo](#) • [Documentation](#api-documentation) • [Deployment](#deployment)

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
- DeepSeek API key ([Get one here](https://platform.deepseek.com))

### Installation

```bash
# Clone repository
git clone https://github.com/yourusername/campusai.git
cd campusai

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

# ENT208Group17

A comprehensive project for data collection and web model development, combining Python backend processing with web interface components.

![4e5bc22cc90754448bcc8f290668b85f](https://github.com/user-attachments/assets/f6ccd32c-a2eb-4cc4-b15d-93af38e91939)

## 📋 Overview

This project is part of the ENT208 course group assignment (Group 17). It focuses on data collection, processing, and web-based interaction through an integrated system with large language model capabilities.

## 🏗️ Project Structure


ENT208Group17/
├── Data_Collection/     # Data collection scripts and utilities
├── Web_Model/           # Web application and API interfaces
├── LICENSE              # Project license
└── README.md            # Project documentation
```

## 🛠️ Technology Stack

- **Backend**: Python (Data processing, API integration)
- **Frontend**: HTML/CSS/JavaScript (Web interface)
- **Data Source**: Structured web content collection
- **AI Integration**: LLM API for intelligent dialogue

## 📦 Installation

1. Clone the repository:
```bash
git clone https://github.com/Airlxis/ENT208Group17.git
cd ENT208Group17
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## 🚀 Usage

### Data Collection
Navigate to the `Data_Collection` directory to access data collection utilities for structured content gathering and preprocessing.

### Web Model
Access the `Web_Model` directory for the web application. The interface supports:
- Real-time dialogue with AI models
- Local network deployment
- Interactive data visualization

## ✨ Key Features

- **Data Pipeline**: Automated content gathering with validation workflows
- **API Integration**: Local network LLM API calling capability
- **Web Interface**: Clean, responsive UI for user interaction
- **Modular Architecture**: Separated components for maintainability and scalability

## 📝 License

This project is licensed under the terms specified in the LICENSE file.

## 👥 Team

**Group 17** - ENT208 Course Project

## 📞 Contact

For questions or issues, please open an issue on the GitHub repository.

---

Last updated: 2026-04-02
```
>>>>>>> fbc3ee017610ab6ef1f090f4a3541e7a50565590

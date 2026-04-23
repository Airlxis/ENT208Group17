# ENT Netlify App

这是 `ENT` 项目的独立 Netlify 版本示例，定位是“西浦生活助手”的前端托管版。目标是：

- 前端可以独立部署到 Netlify，并长期在线访问；
- 后端通过运行策略接口控制是否允许使用；
- 即使本机 FastAPI / 本地后端关闭，部署到 Netlify 后网页仍可访问；
- 聊天接口可选接入 OpenAI；未配置密钥时返回本地兜底回复。

## 目录

- `public/index.html`：前端页面
- `netlify/functions/runtime_policy.js`：运行策略接口
- `netlify/functions/chat.js`：聊天接口，带运行开关控制
- `netlify.toml`：Netlify 发布目录、函数目录和 API 路由配置

## 本地联调

1. 安装 Netlify CLI，如未安装：

```bash
npm i -g netlify-cli
```

2. 进入目录并运行：

```bash
cd netlify_ent_app
netlify dev
```

3. 浏览器访问 Netlify CLI 输出的本地地址。

## 环境变量

在 Netlify Site Settings -> Environment Variables 中配置：

- `APP_ENABLED`：`true` / `false`，控制是否允许运行
- `APP_DISABLED_MESSAGE`：关闭服务时返回的提示文案
- `APP_VERSION`：策略版本号，可选
- `OPENAI_API_KEY`：可选；如果不填，`/api/chat` 会返回本地兜底回复
- `OPENAI_MODEL`：可选，默认 `gpt-4o-mini`

## 关键行为

- 前端启动时会请求 `/api/runtime_policy`，根据 `enabled` 决定是否禁用发送按钮；
- 前端每 15 秒轮询一次运行策略，方便远程暂停或恢复服务；
- 聊天请求发送到 `/api/chat`，后端会再次校验 `APP_ENABLED`，防止绕过前端；
- 你可以随时把 `APP_ENABLED` 改为 `false`，前端轮询后会自动进入“暂停服务”状态。

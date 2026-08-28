# Frontend — file upload

**English** | [中文](#中文)

React app for **offline file upload**. The gateway serves this build at `/` by default.

## Layout

- `src/app`: shell, sidebar, global state
- `src/features/upload`: upload, history, results
- `src/services`: HTTP / SSE
- `src/shared`: shared types and i18n
- `src/styles`: global CSS

## Requirements

- Node.js 18+ (20+ recommended)
- npm 9+

## Development

```bash
cd frontend
npm install
npm run dev
```

Default: `http://127.0.0.1:5173`

`vite.config.ts` proxies to gateway `http://127.0.0.1:7860`:

- HTTP: `/upload` `/stream_task/*` `/task_status/*` `/task_media_info/*` `/task_segment_results/*`
- Static: `/files` `/segments` `/cache` `/avatars`

## Lint and build

```bash
npm run lint
npx tsc -b
npm run build
```

Output: `frontend/dist/`

This directory uses the same [Apache License 2.0](../LICENSE) as the main repo.

## Deploy

`run_gateway.sh` builds the frontend before start (`ENABLE_FRONTEND_BUILD=1`).

- `ENABLE_FRONTEND_BUILD`: default `1`; `0` skips build but requires `frontend/dist/index.html`
- `FRONTEND_BUILD_INSTALL_DEPS=1`: run `npm install` before build
- `FRONTEND_DIR`: frontend directory, default `frontend`

---

## 中文

React 前端工程，仅包含**离线文件上传**页面。网关根路径 `/` 默认服务本应用构建产物。

### 目录说明

- `src/app`: 应用壳、侧边栏、全局状态
- `src/features/upload`: 文件上传、任务历史、结果展示
- `src/services`: HTTP / SSE 访问封装
- `src/shared`: 公共类型与 i18n
- `src/styles`: 全局样式

### 环境要求

- Node.js 18+（建议 20+）
- npm 9+

开发、校验与构建命令与英文部分相同。构建产物：`frontend/dist/`。本目录随主仓库采用 [Apache License 2.0](../LICENSE)。

`run_gateway.sh` 默认会在启动前构建本前端（`ENABLE_FRONTEND_BUILD=1`）。设为 `0` 时跳过构建，但要求 `frontend/dist/index.html` 已存在。

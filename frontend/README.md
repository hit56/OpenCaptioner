# Frontend — 文件上传

React 前端工程，仅包含**离线文件上传**页面。网关根路径 `/` 默认服务本应用构建产物。

## 目录说明

- `src/app`: 应用壳、侧边栏、全局状态
- `src/features/upload`: 文件上传、任务历史、结果展示
- `src/services`: HTTP / SSE 访问封装
- `src/shared`: 公共类型与 i18n
- `src/styles`: 全局样式

## 环境要求

- Node.js 18+（建议 20+）
- npm 9+

## 开发

```bash
cd frontend
npm install
npm run dev
```

默认地址：`http://127.0.0.1:5173`

`vite.config.ts` 已配置代理到网关 `http://127.0.0.1:7860`：

- HTTP: `/upload` `/stream_task/*` `/task_status/*` `/task_media_info/*` `/task_segment_results/*`
- 静态资源: `/files` `/segments` `/cache` `/avatars`

## 校验与构建

```bash
npm run lint
npx tsc -b
npm run build
```

构建产物：`frontend/dist/`

本目录随主仓库采用 [Apache License 2.0](../LICENSE)。

## 部署

`run_gateway.sh` 默认会在启动前构建本前端（`ENABLE_FRONTEND_BUILD=1`）。

常用环境变量：

- `ENABLE_FRONTEND_BUILD`：默认 `1`；设为 `0` 时跳过构建，但要求 `frontend/dist/index.html` 已存在
- `FRONTEND_BUILD_INSTALL_DEPS=1`：构建前先执行 `npm install`
- `FRONTEND_DIR`：前端目录，默认 `frontend`

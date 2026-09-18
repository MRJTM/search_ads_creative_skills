# QueryCraft — 面向搜索广告的创意优化 Skill 工作台

**QueryCraft** 是一个以 Skill 为核心的搜索广告创意优化工作台：输入一个搜索 query（如
“ivory lace wedding guest dress”），它会围绕 query 对广告创意进行**标题改写、封面优选、
轮播编排**，并把结果实时渲染成搜索广告预览（多图在上、标题在下、标题下信息框）。

项目由三部分组成：一套可独立运行、也可被任意 Agent Harness 加载的**创意优化 Skill**；
一个基于 tool-calling 的**对话式 Agent**（负责理解意图并调度 Skill）；一个用于展示与
体验的 **Web 工作台**（FastAPI + React）。

## 🌐 在线体验

> 🚧 体验站点正在部署中，Vercel 部署完成后入口链接将更新在此处。

<!-- TODO: Vercel 部署完成后，替换为实际访问地址，例如 https://querycraft.vercel.app -->

**体验入口**：`Coming soon`

## ✨ Skill 能力总览

每个 Skill 聚焦搜索广告创意的一个具体优化环节，输入结构化 JSON、输出可解释的结构化
结果，既能被本仓库的 Agent 调度，也能被 Codex 等第三方 Agent Harness 直接加载使用。

### 1. 标题改写 · `search-ads-query-title-rewrite`

让广告标题与用户搜索意图对齐，而不是一句泛泛的商品描述。

- **做什么**：把 query 中的意图词前置到标题开头，压缩“Summer New Arrival”式的
  无效修饰，只保留素材库中可验证的卖点事实，支持指定输出语言。
- **不做什么**：不虚构商品事实，不推测素材中不存在的属性。
- **输出**：创意诊断 → 改写策略 → 改写后标题 → 自检结论。

```json
{
  "query": "ivory lace wedding guest dress",
  "original_title": "Elegant Beautiful Dress For Women Summer New Arrival",
  "facts": ["Ivory lace midi dress", "Lined bodice", "Dry clean only"],
  "language": "en"
}
```

### 2. 封面优选 · `search-ads-query-cover-optimize`

在多张候选商品图中，选出与 query 意图最匹配的广告封面。

- **做什么**：优先匹配 query 中的显式意图（颜色 / 款式 / SKU / 角色），其次考虑
  点击吸引力，从现有素材中选出最佳封面。
- **输出**：选中的图片 id、逐图打分与理由、封面卖点，以及可选的轻度编辑 prompt
  （如背景统一化建议）。
- **边界**：只做单封面优选，不负责标题改写或多图轮播。

### 3. 轮播生成 · `search-ads-query-carousel-generate`

把一组商品素材编排成一条有叙事结构的图文轮播广告。

- **做什么**：先理解每张候选图（主体、场景、细节完整度、美观度），再按
  **封面钩子 → 上身/使用证明 → 风格与场景 → 细节收尾** 的故事线规划 3–5 页轮播，
  每页聚焦一个卖点。
- **输出**：逐页 slide 计划，并标注每页素材的动作类型（复用现有图 / 轻度编辑 /
  需要新图）。
- **边界**：只做多图轮播规划，不做单封面选择或标题改写。

## 🖥️ Web 工作台：对话式体验

你不需要手动调用 Skill——在工作台里直接对 Agent 说：

> “把标题优化一下” / “换成蓝色封面的那张” / “帮我生成一个轮播计划”

Agent 通过 OpenAI-compatible 原生 `tools` / `tool_calls` 协议理解意图、调度对应
Skill，并把更新后的创意实时渲染成搜索广告预览（多图在上、标题在下、标题下信息框）。

**工程上的几个关键设计：**

- **每轮真实模型调用**：普通聊天、工具决策、最终总结均来自模型，最终回复永不缓存、
  永不由模板兜底冒充；模型失败时明确返回 service error，如实说明工具执行状态。
- **工具白名单**：只执行 `rewrite_title` / `optimize_cover` / `generate_carousel`
  三个白名单内调用，未知工具与畸形 JSON arguments 一律拒绝。
- **图片理解三级缓存**：L1 catalog 离线预计算（跨会话复用、0 视觉调用）→
  L2 session 缓存（会话内复用）→ L3 并行视觉补缺（≤5 路并发）。命中缓存时封面/
  轮播 Skill 仅做离线排序，显著降低首字延迟。

## 🏗️ 架构

```
├── skills/        # 3 个创意 Skill（SKILL.md + agent 声明 + CLI 脚本）
│   └── search-ads-query-{title-rewrite,cover-optimize,carousel-generate}
├── agent/         # Agent 编排核心：skills_core / orchestrator / models / providers
├── scripts/       # 离线工具：precompute_material_analysis.py（图片理解预计算）
├── webs/
│   ├── backend/   # FastAPI 服务（materials / skills / agent chat 接口）
│   └── front/     # React 18 + TypeScript + Vite 前端
└── materials/     # 示例素材本地副本（不入库；线上从 Supabase Storage 读取）
```

**API 一览**（前端基址由 `VITE_API_BASE_URL` 配置；本地开发默认 `http://localhost:8000`，
Vercel 生产环境默认同源，无需额外配置）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/materials` | 商品与素材库 |
| `GET /api/skills` | 3 个 Skill 的摘要信息 |
| `POST /api/agent/chat` | 对话式调度 Skill，返回更新后的创意 |

## 🚀 本地运行

### 后端（Python 3.9+）

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r webs/backend/requirements.txt
uvicorn webs.backend.main:app --host 0.0.0.0 --port 8000
```

### 前端（Node 18+）

```bash
cd webs/front
npm install
npm run dev      # http://localhost:3000
```

### 环境变量

复制 `.env.example` 并填入真实配置（不要提交真实 key）：

```dotenv
TOPAPI_API_KEY=your-topapi-api-key       # ToAPIs 密钥
TOPAPI_BASE_URL=https://toapis.cn/v1
TOPAPI_TIMEOUT_SECONDS=90
AGENT_MODEL=qwen3.5-flash                # 对话 Agent（工具决策 + 最终总结）
VISION_MODEL=deepseek-v4-flash-vision-exp  # 图片理解（预计算 + 运行时补缺）
IMAGE_MODEL=gpt-image-2
```

完整配置项见 `.env.example`。

## ☁️ Vercel 部署（单项目，前后端同仓）

前后端部署为**同一个 Vercel 项目**：Vercel 构建 `webs/front` 静态资源，`/api/*` 与
`/materials/*` 由根目录的 `app.py`（FastAPI Serverless Function）处理，其余路径兜底
到 SPA 的 `index.html`，前后端同源、无 CORS 问题。路由与构建配置都在 `vercel.json`
中，导入仓库时 Framework Preset 选 **Other** 即可。

部署步骤：

1. GitHub 仓库导入 Vercel（New Project → Import），Preset 选 **Other**；
2. 在 Settings → Environment Variables 配置：
   - `TOPAPI_API_KEY`（必需，模型调用密钥）
   - `SUPABASE_URL`（必需，素材从 Supabase Storage bucket
     `search_ads_creative_skills` 公开读取；`/api/materials` 直接拉取 bucket 中的
     catalog.json，`/materials/{filename}` 302 到 bucket 公开 URL）
   - `SUPABASE_STORAGE_BUCKET`（可选，默认 `search_ads_creative_skills`）
   - `TOPAPI_BASE_URL`、`AGENT_MODEL`、`VISION_MODEL` 等可选，默认值见 `.env.example`
3. Deploy。

本地也可以用 Vercel CLI 部署：`vercel --prod`（环境变量仍需在 Dashboard 配置，
`.env` 不会上传）。

## 🔧 单独运行 Skill（CLI）

不启动服务，通过 stdin 传入 JSON 即可独立验证某个 Skill：

```bash
# 标题改写
printf '%s' '{"query":"ivory summer dress","original_title":"Women Elegant Ivory Midi Dress","facts":["Ivory A-line midi dress","Breathable lightweight fabric"],"language":"en"}' \
  | python skills/search-ads-query-title-rewrite/scripts/run.py

# 封面优选
printf '%s' '{"query":"蓝色封面","images":[{"id":3,"filename":"dress-blue-style.png","role":"style","colors":["blue"],"tags":["blue colorway"],"alt":"Blue dress"}],"current_cover_id":3}' \
  | python skills/search-ads-query-cover-optimize/scripts/run.py

# 轮播生成
python -c 'import json; c=json.load(open("materials/catalog.json")); print(json.dumps({"query":"生成轮播","images":c["images"],"current_cover_id":c["initial_cover_image_id"]}))' \
  | python skills/search-ads-query-carousel-generate/scripts/run.py
```

### 离线预计算（推荐先跑）

预计算脚本会用视觉模型并发分析素材图并原子写回 `catalog.json`，运行时即可跳过
视觉调用：

```bash
python scripts/precompute_material_analysis.py            # 只补缺失 analysis 的图
python scripts/precompute_material_analysis.py --force    # 全量重算
```

## 🧪 测试

```bash
# 后端
pytest webs/backend/tests

# 前端类型检查与构建
cd webs/front && npm run build
```

## 🗺️ Roadmap

- [ ] **Supabase provider**：商品/素材元数据存 Supabase，图片走对象存储，与本地
      `materials` provider 同接口、环境变量切换
- [ ] 会话与 trace 持久化，支持跨设备续聊
- [ ] 兼容自训练模型替换闭源 API 作为 Agent 大脑

## 📎 素材说明

仓库内示例图片均为 **AI 生成的测试素材**，仅用于演示与自动化测试，不代表任何真实商品。
素材（图片 + `catalog.json`）托管在 Supabase Storage 的公开 bucket
`search_ads_creative_skills` 中；仓库里的 `materials/` 只是本地开发副本，不进入
版本库。配置 `SUPABASE_URL` 后，网页的图片与 catalog 均来自 Supabase 暴露的公开
URL。

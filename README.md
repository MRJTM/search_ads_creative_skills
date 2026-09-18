# QueryCraft — 以 Skills 为核心的搜索广告创意优化工作台

**QueryCraft** is a skills-first workbench that rewrites, re-covers, and re-arranges
search ad creatives from a real product material library.

QueryCraft 把「商品素材库 + 三个专注的创意 skill + 一个 Agent」组合起来：你对 Agent 说
“优化标题”“换成蓝色封面”“生成轮播”，它会调用对应 skill，并把结果实时渲染成一个
搜索广告预览（多图在上、标题在下、标题下信息框）。

## 三个 Skill

| Skill | 目录 | 做什么 |
| --- | --- | --- |
| 标题重写 | `skills/search-ads-query-title-rewrite` | 按搜索意图重写广告标题，前置意图词、挑选相关卖点，不虚构事实 |
| 封面优化 | `skills/search-ads-query-cover-optimize` | 按颜色 / 角色 / 标签从素材库选择最匹配查询的封面图 |
| 轮播生成 | `skills/search-ads-query-carousel-generate` | 把现有素材编排成多 slide 轮播计划，每页一个卖点 |

每个 skill 都有 `SKILL.md`（输入、决策逻辑、输出、边界）、`agents/openai.yaml`
（Agent 声明）和 `scripts/run.py`（可独立运行的 CLI）。

## 架构

```
├── skills/      # 3 个创意 skill（SKILL.md + agent 声明 + CLI 脚本）
├── agent/       # Skill 编排核心：skills_core / orchestrator / models / providers
├── scripts/     # 离线工具：precompute_material_analysis.py（图片理解预计算）
├── webs/
│   ├── backend/ # FastAPI 服务（materials / skills / agent chat 接口）
│   └── front/   # React 18 + TypeScript + Vite 前端（本工作台）
└── tmp/materials/ # AI 生成的示例素材与 catalog.json（含可选 analysis 字段）
```

### Agent 架构（多轮 tool calling + 真实模型每轮必达）

Agent 走 OpenAI-compatible 原生 `tools` / `tool_calls` 协议，**每一轮都调用真实模型**：

1. **紧凑 System prompt**（`agent/orchestrator.build_system_prompt`）：只放角色、
   “寒暄 1-3 句、不复述商品清单/能力列表”的聊天策略、三个工具名（完整 schema 走
   `tools` 参数，不在 system 里大段重复）、必要商品事实和每张素材一行的紧凑摘要。
   相比旧版完整 tool prompt（实测约 55s），普通聊天改用实测工具调用稳定的
   `qwen3.5-flash`；同条件对照约 2.1s，`glm-5.3-flash` 曾出现 60s 超时。
2. **消息组装**：system + 会话历史（按条数 ≤12 条且总字符 ≤6000 双重截断）+ 当前
   用户消息，经 `ToApisClient.chat(messages, tools=..., max_tokens=256)` 发给模型。
3. **无 tool call** → 模型 content 直接作为闲聊回复，creative 不变，不调用视觉。
4. **有 tool call** → 只执行白名单内的第一个有效调用（`rewrite_title` /
   `optimize_cover` / `generate_carousel`；未知工具与畸形 JSON arguments 一律拒绝且
   不执行），运行对应 skill 并更新 CreativeState；随后把 assistant `tool_calls` 与
   `role=tool` 结果（含图片理解 `material_insights`）回传，再请求一次自然语言总结
   （不带 tools、`max_tokens=256`）。总结失败时先用更短的 prompt 重试一次；仍失败则
   返回**明确的错误消息**（如实说明工具已执行的状态），绝不把本地摘要伪装成模型回答。
5. **无兜底冒充**：首轮模型调用失败/超时/畸形时，不执行任何 skill、不改 creative，
   直接返回明确的 service error，trace 标记 `provider=error`。旧的确定性关键词路由
   （`detect_intent` / `_fallback_handle`）已删除——模板永远不能冒充 Agent 回答。

### 图片理解缓存层级（延迟优化核心）

| 层级 | 内容 | 生命周期 |
| --- | --- | --- |
| L1 catalog 预计算 | 每张图的 `analysis` 结构化结果（description/product/colors/scene/composition/visible_details/selling_points/risks + model/analyzed_at/source 元数据） | 离线脚本写入 `catalog.json`，跨会话复用，运行时 0 视觉调用 |
| L2 session cache | 后端为每个 session 维护的素材理解缓存，创建时从 catalog `analysis` 初始化 | 会话内复用；单会话最多 64 条；session 总数 LRU 上限 100 |
| L3 parallel provider | 只有 L1/L2 都缺失的图才触发：`analyze_materials_parallel` 一次并行分析（≤5 路），结果写入 L2 | 首次访问新图时一次 |
| 传输层 | 进程级线程安全 `httpx.Client` 连接池复用（不再每请求 TLS 握手） | 进程生命周期 |

- 视觉模型默认 `deepseek-v4-flash-vision-exp`（单图实测约 17.8s、`max_tokens=3000`；
  旧 glm-5.3-flash 约 37.2s）；对话 Agent 默认 `qwen3.5-flash`（同一份 prompt + tools
  实测闲聊约 2.1s、工具决策约 2.4s）。
- 并行分析按 image id 返回；分析 prompt 与 query 无关，结果可复用；trace 的
  `material_analysis` 步骤区分 `precomputed` / `session_cache` / `parallel` 并给出
  hit/miss 数，且绝不含 base64。
- 图片理解结果真实参与后续：紧凑摘要进入 system prompt，`material_insights`
  （摘要/卖点/风险）进入 tool result 并回传给最终模型；cover/carousel 的离线排序保留，
  但同一轮不会为了 trace 重复调用视觉。
- 明确不做的事：最终聊天回复不做语义缓存（每轮必须真实模型调用）；允许缓存的只有
  预计算图片理解、session 图片理解、紧凑 system prompt、HTTP 连接。

### 离线预计算（推荐先跑）

```bash
python scripts/precompute_material_analysis.py            # 只补缺失 analysis 的图
python scripts/precompute_material_analysis.py --force    # 全量重算
python scripts/precompute_material_analysis.py --workers 3
```

脚本用 `VISION_MODEL` 并发（最多 5 路）分析图片并**原子写回** catalog（临时文件 +
`os.replace`）；单图失败保留原有有效 analysis，全部失败则完全不写文件；绝不打印
key 或 base64。命令本身与 query 无关，产出可被所有会话复用。

前端通过以下接口与后端通信（基址由 `VITE_API_BASE_URL` 配置，默认 `http://localhost:8000`）：

- `GET /api/materials` — 商品与素材
- `GET /api/skills` — 3 个 skill 摘要
- `POST /api/agent/chat` — 对话并返回更新后的 creative

## 本地运行

### 1. 后端（Python 3.9+）

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r webs/backend/requirements.txt
uvicorn webs.backend.main:app --host 0.0.0.0 --port 8000
```

### 2. 前端（Node 18+）

```bash
cd webs/front
npm install
npm run dev      # http://localhost:3000
```

## 环境变量

参考 `.env.example`（不要提交真实 key）：

```dotenv
# ToAPIs
TOPAPI_API_KEY=your-topapi-api-key
TOPAPI_BASE_URL=https://toapis.cn/v1
# 可选：后端需要显式走本地代理时配置，例如 http://127.0.0.1:7890
TOPAPI_PROXY_URL=
TOPAPI_TIMEOUT_SECONDS=90
# 对话 Agent（决策 + 最终总结）
AGENT_MODEL=qwen3.5-flash
AGENT_MAX_TOKENS=256
FINAL_MAX_TOKENS=256
# 视觉理解（预计算 + 运行时并行补缺，结果按图缓存）
VISION_MODEL=deepseek-v4-flash-vision-exp
VISION_TIMEOUT_SECONDS=60
VISION_MAX_TOKENS=3000
MATERIAL_ANALYSIS_WORKERS=5
IMAGE_MODEL=gpt-image-2
```

前端支持：

```dotenv
VITE_API_BASE_URL=http://localhost:8000
```

## 无模型兜底政策

- **每轮必须真实模型调用**：普通聊天、工具决策、最终总结都来自 Agent 模型；
  最终回复永不缓存、永不由模板生成。
- 首轮模型调用失败/超时/畸形：不执行任何 skill、不修改 creative，返回明确的
  service error（中文/英文），trace 标记 `provider=error`。
- 最终总结失败：先用更短的 prompt 重试一次；仍失败则明确报“模型最终回复失败”，
  只如实说明工具已执行的状态，不伪装成模型回答。
- 确定性能力只作为 skill 内部实现存在（例如封面/轮播的离线排序），由模型自主决定
  调用，并在 trace/tool result 中如实标注 `provider=offline`。Agent 标题改写的二次
  模型调用失败时返回 `title_provider_failed`，不会静默采用本地标题冒充成功。
- 未配置 `TOPAPI_API_KEY` 时同样遵循上述政策：返回 service error 而不是假装回答。

## 预期延迟（实测参考值）

| 链路 | 旧 | 新 |
| --- | --- | --- |
| 普通聊天 | 完整 tool prompt 约 55s；glm 偶发 60s 超时 | 紧凑 prompt + qwen3.5-flash + `max_tokens=256`，同条件实测约 2.1s |
| cover/carousel 首次（无预计算） | 单次多图视觉约 37s（glm） | 5 图并行 deepseek 视觉，墙钟约等于最慢单图（单图约 17.8s） |
| 预计算命中 | — | 0 次视觉调用，仅离线排序 |
| 同会话第二次访问新图 | 重复视觉调用 | session cache 命中，0 次视觉调用 |
| 最终总结失败 | 本地模板兜底 | 一次短 prompt 重试；仍失败则明确报错 |

## 直接运行 Skill CLI

不启动服务，单独验证某个 skill：

```bash
printf '%s' '{"query":"ivory summer dress","original_title":"Women Elegant Ivory Midi Dress","facts":["Ivory A-line midi dress","Breathable lightweight fabric"],"language":"en"}' \
  | python skills/search-ads-query-title-rewrite/scripts/run.py

printf '%s' '{"query":"蓝色封面","images":[{"id":3,"filename":"dress-blue-style.png","role":"style","colors":["blue"],"tags":["blue colorway"],"alt":"Blue dress"}],"current_cover_id":3}' \
  | python skills/search-ads-query-cover-optimize/scripts/run.py

python -c 'import json; c=json.load(open("tmp/materials/catalog.json")); print(json.dumps({"query":"生成轮播","images":c["images"],"current_cover_id":c["initial_cover_image_id"]}))' \
  | python skills/search-ads-query-carousel-generate/scripts/run.py
```

## 测试

```bash
# 后端
pytest webs/backend/tests

# 前端类型检查与构建
cd webs/front && npm run build
```

## Roadmap：Supabase provider

计划新增以 Supabase 为存储的素材 provider：

- 商品 / 素材元数据存 Supabase 表，图片走对象存储；
- `agent/providers.py` 增加 `SupabaseMaterialsProvider`，与本地 `tmp/materials`
  实现同一接口，通过环境变量切换；
- 会话与 trace 持久化，支持跨设备续聊。

## 素材说明

仓库内示例图片均为 **AI 生成的测试素材**，仅用于演示与自动化测试，不代表任何真实商品。

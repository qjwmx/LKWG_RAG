# 洛克王国：世界 · PvP 阵容攻略 Agent

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Vue-3.x-green.svg)](https://vuejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.x-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![TailwindCSS](https://img.shields.io/badge/Tailwind-4.x-38bdf8.svg)](https://tailwindcss.com/)
[![daisyUI](https://img.shields.io/badge/daisyUI-5.x-5a0ef8.svg)](https://daisyui.com/)

选一只精灵 → 查出**真实玩家投稿里包含它的阵容**（精确反查，不是模糊相似）→
大模型结合队友统计与属性克制给出推荐。

**不配任何外部服务也能启动**：仓库自带精灵图鉴、技能、阵容种子数据，
嵌入可切本地伪向量模式，界面与检索链路完整。

## 核心特性

- **五 Agent 深度研究** — Planner → Researcher → Analyst → Writer → Reviewer，带复核回环（证据不足回检索、表达问题回撰写），超限强制放行而不是崩溃
- **精确反查，不是向量相似** — 「哪些阵容里有寂灭骨龙」走 SQL 索引，
  答得出「有 32 套」；「什么阵容克制水系」走向量检索。**两类问题两条路**
- **多轮追问记忆** — 攻略模式挂 LangGraph Checkpointer，「那换成水系呢」这种没有主语的追问也答得出来
- **联网检索（可选）** — 对话与攻略都有开关，**高亮 = 本轮会联网**。三道提示注入防线，外部内容与本地资料在界面上分组展示
- **流式输出** — SSE 逐字推送，五 Agent 进度实时可见
- **Redis 四块共享状态（可选）** — 令牌吊销 / 登录限流 / 查询向量缓存 / 检索结果缓存，带语料版本号失效；不配则走进程内实现，**功能不缺失**
- **可观测性（仅管理员）** — 请求轨迹 + 按 Agent 的 token 统计，明确**不采集**口令、token、query string、请求体
- **注册需审批** — 新用户 `pending` 状态登录失败，管理员通过后才能用；root 不落库

## 快速开始

### 前置条件

只需要装这两样，其余全部可选：

- **Python 3.11+**（[下载](https://www.python.org/downloads/)，安装时勾选 `Add Python to PATH`）
- **Node.js 20+**（[下载](https://nodejs.org/)，选 LTS 版）

以下**都不是必需的**，不装也能把界面完整跑起来：

| 服务 | 作用 | 不装的后果 |
| --- | --- | --- |
| LLM API Key | 问答与攻略的对话模型 | 界面能用，提问返回一条可读的错误提示 |
| Ollama | 本地嵌入模型 | 检索没有语义（可先用 `hash` 模式跑通） |
| Redis | 多实例共享状态 | 单进程部署完全够用 |
| Tavily Key | 联网检索 | 界面上的联网开关显示「未配置」并禁用 |

### 第 1 步：后端

```bash
cd backend

# 建虚拟环境
python -m venv .venv

# 激活（Windows）
.venv\Scripts\activate
# 激活（macOS / Linux）
source .venv/bin/activate

# 装依赖
pip install -r requirements.txt

# 导入领域数据（精灵/技能/阵容/属性克制）—— 必需，否则库是空的
python -m app.scraper.seed_loader
```

看到下面这样的输出就成功了：

```
{'type_matchup': 361, 'pets': 465, 'skills': 469, 'lineups': 151, 'lineup_members': 906}
```

### 第 2 步：前端

```bash
cd frontend
pnpm install          # 或 npm install，两者都可以
```

没装 pnpm 的话，用 Node 自带的 npm 就行（会多几条无害的配置警告，可忽略）。

### 第 3 步：启动

开**两个终端**，分别运行：

**终端 1 —— 后端：**

```bash
cd backend
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

**终端 2 —— 前端：**

```bash
cd frontend
pnpm dev              # 或 npm run dev
```

浏览器打开 **http://localhost:5173** 即可使用。

> 后端的接口文档在 http://127.0.0.1:8000/docs 。

### 第 4 步：登录

```
用户名：admin
密码：  admin
```

> ⚠️ **对外提供服务前务必修改默认口令**（改 `backend/.env` 里的
> `ADMIN_USERNAME` / `ADMIN_PASSWORD`，改完要重启后端）。

到这里界面就完整可用了：上传、切分、入库、检索、流式渲染都是真的。
但问答要给出有意义的回答，还需要配置对话模型与嵌入模型（见下一节）。

## 让问答真正可用

上面三步能让**界面完全跑通**，但问答需要两样东西：一个对话模型、一个嵌入模型。

### 对话模型（必配）

复制 `backend/.env.example` 为 `backend/.env`，填上你的 key：

```env
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-你的key
LLM_MODEL=deepseek-chat
```

任何 **OpenAI 兼容**端点都能用，不限于 DeepSeek：

| 服务 | `LLM_BASE_URL` | `LLM_MODEL` |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

> **DeepSeek 用户注意**：`STRUCTURED_OUTPUT_METHOD` 必须保持 `function_calling`。
> DeepSeek 不支持 `json_schema`，而这个错误**不会中断流程**——只会让
> Planner 永远只拆一个单元、检索结论变成原文摘要，很容易被误判成模型能力不行。

### 嵌入模型（三选一）

**方式 A：本地 Ollama（推荐，免费）**

```bash
ollama serve
ollama pull qwen3-embedding:4b
```

```env
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_DIM=2560
```

> **ollama 必须处于运行状态**。它没启动时检索会失败，页面表现为
> 「未找到明确依据」——这是排查检索问题时最常见的原因。
> `/system/health` 会单独探测它，前端顶部也会给出提示。

**方式 B：云端嵌入服务**

```env
EMBEDDING_PROVIDER=openai
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-你的key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
```

### 验证配置

访问 http://127.0.0.1:8000/api/v1/system/health （需先登录，可在 `/docs` 里点 Authorize），
会返回各依赖的状态：

```json
{
  "status": "ok",
  "postgres": { "ok": true, "detail": "连接正常" },
  "embedding": { "ok": true, "provider": "ollama" },
  "llm": { "ok": true },
  "redis": { "ok": true, "configured": false }
}
```

`status` 为 `degraded` 表示有组件降级（如用了 hash 伪嵌入、没配 LLM key），
功能仍可用，具体原因看各字段的 `detail`。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| **阵容查询** | 按属性/种族值筛选精灵，选一只反查含它的阵容，看 6 只成员的头像、性格、血脉、个体值、技能图标与六维图 |
| **攻略研究** | 五 Agent 协作：Planner → Researcher → Analyst → Writer → Reviewer，产出带引用的报告 |
| **知识库 RAG** | 上传文档 → 解析 → 切分 → 向量入库 → 语义检索，带引用高亮 |
| **智能问答** | 流式输出，引用来源卡片可点开看原文片段 |
| **联网检索** | Tavily，对话与攻略都支持，默认关闭，用户显式开启 |
| **多轮追问** | 攻略模式带会话记忆，追问不用重复主语 |
| **管理后台** | 用户审批、文档管理、问答记录、统计、观测 |
| **注册审批** | 新用户 `pending` 状态，管理员通过后才能登录 |

## 系统架构

```
┌───────────────────────────────────────────────────────────────┐
│                    前端 (Vue 3 + Vite)                          │
│          Tailwind CSS 4 + DaisyUI 5 + Pinia + TypeScript       │
├───────────────────────────────────────────────────────────────┤
│                    SSE / REST (JSON)                           │
├───────────────────────────────────────────────────────────────┤
│                     后端 (FastAPI)                              │
├──────────────────┬──────────────────┬─────────────────────────┤
│   对话模式        │    攻略模式       │      知识库              │
│  rag_graph.py    │ strategy_graph.py│  documents / vector_store│
│  向量库取证       │  结构化表取证      │  上传→切分→入库→检索     │
├──────────────────┴──────────────────┴─────────────────────────┤
│                  LangGraph (五 Agent 编排)                      │
├────────────────────┬────────────────────┬─────────────────────┤
│ SQLite / PostgreSQL│  向量(应用层扫描)    │  Redis (可选)        │
│ 业务表 + 领域表     │  document_chunks   │  吊销/限流/缓存       │
└────────────────────┴────────────────────┴─────────────────────┘
```

## 技术栈

| 层级 | 技术 |
| --- | --- |
| **后端框架** | FastAPI + Uvicorn |
| **AI 编排** | LangGraph + LangChain |
| **LLM** | 任意 OpenAI 兼容接口 |
| **嵌入** | Ollama / OpenAI 兼容 / hash 伪嵌入（三选一） |
| **数据库** | SQLAlchemy 2.0 + SQLite（可切 PostgreSQL） |
| **缓存** | Redis（可选：令牌吊销 / 登录限流 / 向量与检索缓存） |
| **向量检索** | 应用层精确扫描（`document_chunks` 存二进制向量） |
| **联网搜索** | Tavily REST API |
| **鉴权** | JWT (PyJWT) + pbkdf2 口令哈希 |
| **前端** | Vue 3 + Vite + Pinia + Tailwind CSS 4 + DaisyUI 5 + TypeScript |
| **可观测性** | 内置轻量实现 |

## 五 Agent 流水线

**对话模式与攻略模式跑的是同一套骨架**，区别只在 Researcher 的取证手段：

| | 对话模式（机制问答） | 攻略模式（阵容研究） |
| --- | --- | --- |
| 证据来源 | **向量库** `document_chunks` | **结构化表** `lineups` / `pets` |
| 入口 | `POST /api/v1/chat/chat` | `POST /api/v1/strategy/research` |
| 图定义 | `services/rag_graph.py` | `services/strategy_graph.py` |
| 检索单元 | Planner 拆子问题 → 每单元独立向量检索 | Planner 拆子问题 → 查阵容/队友/克制 |

```
START → classify → retrieve ─┐
                             │  预检为空
                             └────────────► no_evidence → persist → END
                             │
                             ▼
          planner → [Send 扇出] → researcher ×N → analyst
                                       ▲              │
                                       │              ▼
                          证据不足 ────┤           generate
                                       │              │
                                       └──────────  reviewer
                                                      │
                                     表达问题 → generate │
                                                      ▼
                                              persist → END
```

三个非线性的地方，链式写法都会退化成难追踪的嵌套 `if`：

- **扇出** — Planner 拆出的研究单元用 `Send` API 并行研究，
  结果通过 `operator.add` 归约（少了归约，并行结果会互相覆盖，只剩一条且不报错）
- **回环** — Reviewer 打回**分两个方向**：证据不足回 Researcher，
  表达问题回 Writer。混成一个方向会让文字问题触发无谓的重新检索
- **护栏** — 超限时返回 `END` **而不是**撞 `recursion_limit` 崩溃，
  用户永远拿到产出，报告里注明「未定稿」

**成本护栏**：对话模式在 `retrieve` 节点先做一次 `COUNT`，
知识库里一条可检索的切片都没有时**直接短路**，不启动 Planner + N 次 Researcher。
实测空分类下整轮耗时 **0.02 秒、0 次 LLM 调用**。

## 为什么结构化表和向量检索都要

它们回答的是**两类不同问题**：

| 问题 | 走哪条路 | 为什么 |
| --- | --- | --- |
| 「哪些阵容里有寂灭骨龙？」 | `lineup_members` 索引 | 这是**精确关系查询**。向量检索答不了——它只能给"相关"，给不出"有 32 套"，还会漏 |
| 「什么阵容克制水系？」 | 向量检索 `document_chunks` | 这是语义问题，没有确定的关键词可匹配 |

所以导入阵容时**两边都写**：结构化行 + 渲染成文本灌进向量库。
这不是冗余，是两种查询模式各自的必需品。

## 知识库

### 支持的文档

| 项 | 说明 |
| --- | --- |
| 文件类型 | `.txt` `.md` `.markdown` `.pdf` `.docx` `.xlsx` `.csv` |
| 单文件上限 | 50 MB（`UPLOAD_MAX_MB` 可调） |
| 分类 | 精灵图鉴 / 技能图鉴 / 属性克制 / 阵容攻略 / 版本公告 / 未分类 |

`.md` / `.markdown` 走纯文本路径，Markdown 语法**刻意保留不清洗**：
标题层级天然是切分边界，能让 chunk 语义更完整。

### 检索流程

```
用户提问
  → 预检 COUNT（库空则直接短路，不花模型调用）
  → 查询向量缓存（Redis，命中则跳过 embedding）
  → 向量召回 Top-K（默认 4）
  → 可见性过滤下推 SQL（普通用户拿不到他人私有文档）
  → 检索结果缓存
  → 送进模型
```

### 文档处理

- 上传：拖拽或选择文件，支持多选。**部分失败不阻断整批**，逐项显示结果
- 同内容文档（按 sha256 判定）重复上传会被识别为「已存在」并跳过
- 删除会**同时移除原始文件与向量索引**（外键级联），历史问答记录保留
- 写入失败时有补偿清理，且原始异常不会被吞

## 攻略模式的多轮追问

带 `thread_id` 的 checkpointer 让同一会话的后续轮次能读到上一轮的结论，
追问（「那换成水系呢」）才有主语可指。

```env
CHECKPOINT_ENABLED=true
CHECKPOINT_MAX_THREADS=500     # 表只增不减，必须能裁
```

**只给攻略模式加，对话模式不加**：对话模式已经从 `qa_logs` 重建历史，
它天然就是多轮的。实测给它也加 checkpointer 会让消息二次增长。

## Redis 缓存设计

```bash
docker run -d --name myrag-redis -p 6379:6379 redis:7-alpine
```

```env
REDIS_URL=redis://127.0.0.1:6379/0
```

| 功能 | Key | TTL | 说明 |
| --- | --- | --- | --- |
| 令牌吊销 | `myrag:revoked:{jti}` | 与 JWT 一致 | 登出后旧 token 立刻 401，只吊销当前令牌 |
| 登录限流 | `myrag:login_fail:{指纹}` + `myrag:login_lock:{指纹}` | 300 秒 | 防口令爆破，锁定期间正确口令也被拒 |
| 查询向量缓存 | `myrag:embed:{模型}:{文本哈希}` | 7 天 | 查询向量是纯函数结果，缓存无正确性风险 |
| 检索结果缓存 | `myrag:search:{语料版本}:{可见性}:{分类}:{top_k}:{查询哈希}` | 120 秒 | 实测 3000 切片时 **118ms → 1.1ms（约 100 倍）** |

## 可观测性（仅管理员）

管理后台侧栏有一个**「观测」**页，展示请求轨迹、模型用量与缓存状态。

**普通用户看不到这个入口，也调不到接口**：前端侧栏只在 `/admin/*` 下渲染，
后端在 admin router 上挂了 `require_admin`，非管理员调接口拿到 **403**、
未登录拿到 **401**。

| 维度 | 内容 |
| --- | --- |
| 请求轨迹 | 路由模板、方法、状态码、耗时、用户名、错误摘要 |
| 模型用量 | 按 Agent 分组的调用次数、token、耗时 |
| 缓存状态 | Redis 是否启用、语料版本号、键数、降级情况 |
| 采集器自身 | 队列占用、已落库数、**丢弃数** |

## 两个角色的可见范围

| | 普通用户 | 管理员（root） |
| --- | --- | --- |
| 上传的文档进哪 | **自己的私有库** | **公共库** |
| 能检索到 | 公共库 + 自己的私有 | **全部**（含他人私有） |
| 能删什么 | 只有自己上传的私有文档 | **任何文档** |
| 历史会话 | 只有自己的 | **全部** |
| 管理后台 | — | ✅ |

## 注册与审批

注册后账号处于 **`pending`**，**登录会被拒绝**。必须由管理员在
**管理后台 → 用户审批** 里点「通过」才能登录。

登录失败时前端统一提示：

这句对「用户不存在」「口令错误」「待审批」三种情况都成立，**既不泄露账号是否存在，
也不让待审批的用户干等**。真正的失败原因只写进服务端日志。

被拒绝的账号**不会被删除**，只置为 `rejected`；对方重新注册时复用同一行。

## 项目结构

```
LKWG_RAG/
├── backend/
│   ├── app/
│   │   ├── main.py               # create_app() 总装 + CORS + 建表
│   │   ├── config.py             # pydantic-settings 读 .env
│   │   ├── security.py           # JWT 签发/校验 · get_current_user · require_admin
│   │   ├── schemas.py            # BaseResponse[T] + 请求模型
│   │   ├── db/                   # models / domain_models / session
│   │   ├── cache/                # Redis：令牌吊销 / 登录限流 / 向量与检索缓存
│   │   ├── domain/
│   │   │   └── roco.py           # ★ 领域常量：19 属性克制表 / 性格修正 / 名称归一
│   │   ├── services/
│   │   │   ├── parser.py         # txt/md/pdf/docx/xlsx/csv → 纯文本
│   │   │   ├── chunker.py        # LangChain 递归切分
│   │   │   ├── embeddings.py     # ollama | openai | hash
│   │   │   ├── vector_store.py   # 检索（可见性下推 SQL）+ 切片读写
│   │   │   ├── documents.py      # 入库唯一写入口（去重 + 失败补偿清理）
│   │   │   ├── rag_graph.py      # ★ 对话：五 Agent 检索图
│   │   │   ├── strategy_graph.py # ★ 攻略：五 Agent 研究图
│   │   │   ├── checkpoint.py     # ★ 多轮记忆：sentinel reducer / thread_id 隔离
│   │   │   ├── web_search.py     # ★ 联网检索（三道注入防线，两模式共用）
│   │   │   ├── roco_store.py     # 精灵/技能/阵容查询 + 克制矩阵计算
│   │   │   └── users.py          # 注册 / 审批 / pbkdf2 校验
│   │   ├── scraper/              # 数据获取层（抓取与入库分离）
│   │   │   ├── seed_loader.py    # 仓库内种子文件 → 领域表（默认路径，离线可用）
│   │   │   ├── bwiki.py          # BWIKI 爬虫（EdgeOne 反爬，HTTP 567）
│   │   │   ├── bilibili.py       # ★ B 站专栏爬虫（阵容码格式）
│   │   │   ├── importer.py       # ★ 源无关导入（字段继承 + 来源并列）
│   │   │   ├── images.py         # 精灵头像 / 技能图标 URL 同步（只存 URL）
│   │   │   └── run_scrape.py     # 多源命令行入口
│   │   ├── observability/        # ★ 请求轨迹 + token 统计（仅管理员可见）
│   │   └── routes/               # auth / system / chat / knowledge / admin / roco / public
│   ├── scripts/                  # 截图 / 视觉探针 / 冒烟 / 诊断脚本
│   ├── sample_docs/              # 示例机制文档
│   ├── data/
│   │   ├── _seed/                # ★ 种子数据（必需）：精灵图鉴 / 技能 / 阵容
│   │   └── _scrape_cache/        # 图片 URL 缓存
│   ├── tests/                    # pytest（259 项）
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── style.css             # 主题 + 属性色板 + 壁纸/玻璃令牌（唯一来源）
    │   ├── api/                  # http(拦截) / sse(手写流解析) / 各资源模块
    │   ├── stores/               # auth / chat / knowledge / admin / roco
    │   ├── composables/          # useMarkdown / useTheme / useWallpaper / wallpapers
    │   ├── components/           # chat / knowledge / layout / roco
    │   └── views/                # 登录注册 / 问答 / 阵容攻略 / 知识库 / admin
    ├── vite.config.ts            # @tailwindcss/vite 插件 + /api 代理
    └── package.json
```

## API 路由

统一响应格式：

```json
{ "code": 200, "msg": "success", "data": {} }
```

**HTTP 状态码恒为 200，业务状态放在 `code` 字段**。**唯二的例外是鉴权**：
401（未登录 / 凭据无效）与 403（已登录但权限不够）走真实状态码。

**身份一律从 JWT 解出，请求体与 query 里没有 `username` / `role` / `owner` / `scope`。**

### 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册（待审批） |
| POST | `/api/v1/auth/login` | 登录并签发 JWT（带失败限流） |
| POST | `/api/v1/auth/logout` | 登出：吊销当前 token（立即失效） |
| GET | `/api/v1/auth/me` | 当前用户 |

### 系统

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/system/health` | 数据库 / 嵌入 / 模型 / Redis 健康检查 |
| GET | `/api/v1/system/stats` | 统计 |
| GET | `/api/v1/system/categories` | 分类与支持的文件类型 |

### 对话

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/chat/chat` | 提问（`stream=true` → SSE；`use_web` 控制联网） |
| GET | `/api/v1/chat/sessions` | 会话列表 |
| GET | `/api/v1/chat/history` | 按会话取历史消息（含 `source_docs` / `web_sources`） |
| POST | `/api/v1/chat/delete_session` | 删除会话 |
| POST | `/api/v1/chat/feedback` | 提交反馈 |

### 知识库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/knowledge_base/list_files` | 文档列表 |
| GET | `/api/v1/knowledge_base/chunks` | 文档切片（引用高亮用） |
| POST | `/api/v1/knowledge_base/upload_docs` | 上传（多文件） |
| POST | `/api/v1/knowledge_base/delete_docs` | 删除 |
| POST | `/api/v1/knowledge_base/seed` | 导入示例文档（**管理员**） |

### 洛克王国领域

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/pokedex/pets` | 精灵图鉴（`?keyword=&attributes=火,水&min_total=600&sort=total_desc`） |
| GET | `/api/v1/pokedex/pet` | 精灵详情（含六维、头像、可学技能） |
| GET | `/api/v1/pokedex/skills` | 技能图鉴 |
| GET | `/api/v1/pokedex/types` | 属性克制表 |
| GET | `/api/v1/pokedex/stats` | 领域数据概览（含数据口径声明） |
| GET | `/api/v1/lineups/by_pet` | ★ 按精灵反查阵容 + 队友统计 |
| GET | `/api/v1/lineups/list` | 阵容列表 |
| GET | `/api/v1/lineups/detail` | 阵容详情（含成员与属性分析） |
| GET | `/api/v1/lineups/top_pets` | 投稿中出现最多的精灵 |
| POST | `/api/v1/strategy/research` | ★ 五 Agent 攻略研究（SSE） |
| POST | `/api/v1/strategy/research_sync` | 同上，非流式（调试用） |
| GET | `/api/v1/strategy/web_status` | 联网可用性（**不回显 key**） |

### 管理（仅管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/admin/users` | 用户列表 |
| POST | `/api/v1/admin/users/review` | 审批（通过 / 拒绝 / 重置） |
| POST | `/api/v1/admin/users/role` | 改角色 |
| POST | `/api/v1/admin/users/delete` | 删除用户 |
| GET | `/api/v1/admin/qa_logs` | 全部问答 |
| GET | `/api/v1/admin/feedback` | 全部反馈 |
| GET | `/api/v1/admin/observability/overview` | 总览：QPS / 错误率 / 耗时百分位 / token |
| GET | `/api/v1/admin/observability/requests` | 请求轨迹 |
| GET | `/api/v1/admin/observability/llm` | 按 Agent 分组的模型用量 |
| GET | `/api/v1/admin/observability/cache` | 缓存状态 |

### 公开（无需登录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/public/showcase` | 登录页展示用的精灵列表 |

### SSE 事件格式

仅用 `data:` 字段，客户端一次 `json.loads` 即可：

```
data: {"type":"agent_status","agent":"Planner","stage":"planning","percent":8}
data: {"type":"artifact","agent":"Planner","kind":"plan","payload":{...}}
data: {"type":"delta","content":"..."}          ← 只有 Writer 的正文
data: {"type":"done","answer":"...","source_docs":[...]}
data: {"type":"web_unavailable","message":"..."}  ← 开了联网但服务端没配
data: {"type":"error","message":"..."}
```

## 环境变量

下表列出常用的：

### 必填

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `LLM_API_KEY` | 对话模型 key | 空（问答返回可读错误） |
| `LLM_BASE_URL` | 对话模型端点 | `https://api.deepseek.com` |
| `LLM_MODEL` | 模型名 | `deepseek-chat` |

### 鉴权与管理员

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `JWT_SECRET` | JWT 密钥。留空则每次启动随机生成，**重启后所有 token 失效** | 空 |
| `JWT_EXPIRE_MINUTES` | token 有效期（分钟） | `1440` |
| `ADMIN_USERNAME` | root 账号 | `admin` |
| `ADMIN_PASSWORD` | root 口令（**上线前必改**） | `admin` |
| `ALLOW_ADMIN_PROMOTION` | 是否允许 root 提升其他用户为管理员 | `true` |

生成一个随机密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

### 嵌入

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `ollama` / `openai` / `hash` | `ollama` |
| `OLLAMA_BASE_URL` | Ollama 地址 | `http://localhost:11434` |
| `EMBEDDING_MODEL` | 嵌入模型名 | `qwen3-embedding:4b` |
| `EMBEDDING_DIM` | 向量维度，**必须与模型一致** | `2560` |
| `EMBEDDING_BASE_URL` | 仅 `openai` 时使用 | 空 |
| `EMBEDDING_API_KEY` | 仅 `openai` 时使用 | 空 |

### Redis

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `REDIS_URL` | 留空 = 用进程内实现 | 空 |
| `REDIS_KEY_PREFIX` | 多环境隔离用的键前缀 | `myrag` |
| `REDIS_SOCKET_TIMEOUT` | 单次操作超时（秒） | `1.0` |
| `EMBED_CACHE_TTL` | 查询向量缓存（秒） | `604800` |
| `SEARCH_CACHE_TTL` | 检索结果缓存（秒） | `120` |

### 检索与上传

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `TOP_K` | 向量检索取回条数 | `4` |
| `MAX_HISTORY_ROUNDS` | 对话重建历史轮数 | `4` |
| `UPLOAD_MAX_MB` | 单文件上传上限（MB） | `50` |
| `STRUCTURED_OUTPUT_METHOD` | `function_calling`（DeepSeek 必须用这个） | `function_calling` |

### 联网检索

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `TAVILY_API_KEY` | 留空 = 界面按钮显示「未配置」并禁用 | 空 |
| `WEB_SEARCH_ENABLED` | 服务端总开关 | `true` |
| `TAVILY_MAX_RESULTS` | 每次取回条数（上限 10） | `5` |
| `TAVILY_TIMEOUT` | 单次超时（秒） | `8.0` |
| `WEB_SNIPPET_MAX_CHARS` | 网页摘要截断长度 | `1200` |

### 多轮记忆

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `CHECKPOINT_ENABLED` | 攻略模式会话记忆 | `true` |
| `CHECKPOINT_DB` | 留空 = 与业务库同目录的 `checkpoints.db` | 空 |
| `CHECKPOINT_MAX_THREADS` | 保留的会话线程上限 | `500` |

### 可观测性

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `OBSERVABILITY_ENABLED` | 关掉时零开销 | `true` |
| `OBSERVABILITY_RETENTION_DAYS` | 轨迹保留天数 | `7` |
| `OBSERVABILITY_MAX_ROWS` | 轨迹表行数上限 | `20000` |
| `OBSERVABILITY_QUEUE_SIZE` | 写入队列容量（满了丢弃并计数） | `2000` |

### 数据库

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DATABASE_URL` | 默认 SQLite，落 `backend/data/rag.db`。切 PostgreSQL 需另装 `psycopg[binary]` | SQLite 绝对路径 |

## 数据模型

### 业务表

| 表 | 说明 | 关键字段 |
| --- | --- | --- |
| `users` | 用户 | username(PK), password_hash, display_name, role, status |
| `documents` | 文档 | id, file_hash, title, category, owner, scope, chunk_count, status |
| `document_chunks` | 文档切片 | document_id, chunk_index, content, embedding |
| `qa_logs` | 问答记录 | id, session_id, username, question, answer, source_docs, web_sources |
| `feedback_logs` | 反馈 | id, qa_log_id, rating, comment |
| `request_traces` | 请求轨迹 | id, route, method, status, duration_ms, username |
| `llm_call_records` | 模型用量 | 按 Agent 分组的调用次数与 token |

### 领域表

| 表 | 说明 | 规模（种子数据） |
| --- | --- | --- |
| `pets` | 精灵图鉴 | 465 只 |
| `skills` | 技能图鉴 | 469 个 |
| `type_matchup` | 属性克制表 | 361 条 |
| `lineups` | 阵容 | 151 套 |
| `lineup_members` | 阵容成员 | 906 条 |

> 领域表由 `python -m app.scraper.seed_loader` 从 `backend/data/_seed/` 导入。

## 更新数据

种子数据会过时。爬虫支持多个数据源，抓取与入库分离：

```bash
cd backend

# 看有哪些源
python -m app.scraper.run_scrape --list-sources

# BWIKI：先抓 30 套试水（确认网络没被拦）
python -m app.scraper.run_scrape --source bwiki --limit 30

# B 站：抓当赛季的「阵容码」配队
python -m app.scraper.run_scrape --source bilibili --limit 10

# 继续上次中断的抓取（跳过已抓的）
python -m app.scraper.run_scrape --source bwiki --resume

# 确认抓完整了，再写进数据库
python -m app.scraper.run_scrape --import all
```

**同步精灵头像与技能图标 URL**：

```bash
python -c "from app.scraper.images import sync_all; print(sync_all(force=True))"
python -m app.scraper.seed_loader        # 再重新导入，把 URL 写进数据库
```

## 数据来源与许可

### 阵容数据

| 来源 | 许可 | 规模 | 时效 |
| --- | --- | --- | --- |
| [justeHe/roco-battle-simulator](https://github.com/justeHe/roco-battle-simulator) `pvp_lineups.json` | **MIT** | 151 套 PvP / 906 成员槽位 | 2026-05 前后 |
| BWIKI [阵容一览](https://wiki.biligame.com/rocom/阵容一览) | CC BY-NC-SA（投稿） | 187 套 | 2026-06 |
| B 站专栏「阵容码」配队 | 玩家投稿，非商业引用 | 当赛季配队 | S4 赛季 |

### 精灵与技能数据

| 项 | 说明 |
| --- | --- |
| 来源 | [AofeiLi-code/rocom-data](https://github.com/AofeiLi-code/rocom-data) 的 `sprites.json` + `skills.csv` |
| 许可 | ⚠️ **该仓库没有 LICENSE 文件**，README 声明「禁止商业使用」 |
| 规模 | 465 只精灵、469 个技能 |
| 说明 | 只取**图鉴类事实数据**（属性、种族值、技能数值），**不取它的阵容数据** |

**若你要商用**：请自行替换这份图鉴数据，或先联系原作者取得授权。

### 游戏数据的版权

MIT / 无许可覆盖的是**代码仓库**，**不覆盖游戏本身的数据**。
精灵、技能、阵容的权利归腾讯 / 魔方工作室及 BWIKI 贡献者。

BWIKI 的内容是 **CC BY-NC-SA 4.0**：**禁止商业使用**、需署名、衍生需同协议。
本项目按**非商业用途**交付。若你要对外提供服务，请自行评估法律风险。

## 许可证

本仓库**尚未添加 LICENSE 文件**，因此不声明任何开源许可证，默认保留所有权利。
在补充 LICENSE 之前，他人不可直接复用本仓库代码。

第三方数据的许可情况见上一节。

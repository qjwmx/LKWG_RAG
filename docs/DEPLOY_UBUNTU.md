# Ubuntu 部署手册：从 GitHub 拉取到前后端启动

本文是**从零开始**的完整部署手册。假设你手上是一台刚装好的 Ubuntu 服务器，
除了能 SSH 登录之外什么都没有。照着做完就能在浏览器里打开项目。

- 目标系统：**Ubuntu 22.04 LTS**（24.04 同样适用，差异处会单独标注）
- 部署方式：宿主机直接装依赖（另有 Docker 方式，见[第 8 节](#8-docker-方式可选)）
- **AI 相关配置全部留空也能跑起来**：图鉴、阵容查询、知识库上传/切分/检索、
  登录注册、管理后台都是真的可用，只有"问答要模型生成答案"那一步需要你填 key。

> 全文命令都是**在服务器上**执行的。`$` 开头的行表示普通用户命令，
> `#` 开头的行表示需要 root（用 `sudo`）。

---

## 目录

1. [准备：确认服务器信息](#1-准备确认服务器信息)
2. [安装系统依赖（Git / Python 3.11+ / Node.js 22）](#2-安装系统依赖)
3. [从 GitHub 拉取代码](#3-从-github-拉取代码)
4. [一键安装（推荐）](#4-一键安装推荐)
5. [启动并验证](#5-启动并验证) —— 含**一键自检脚本**
6. [配置 AI（可以留空）](#6-配置-ai可以留空)
7. [生产部署：systemd + Nginx](#7-生产部署systemd--nginx)
8. [Docker 方式（可选）](#8-docker-方式可选)
9. [日常运维](#9-日常运维)
10. [常见问题排查](#10-常见问题排查)
11. [附录：手动安装逐步说明](#11-附录手动安装逐步说明)

---

## 1. 准备：确认服务器信息

### 1.1 确认系统版本

```bash
lsb_release -a
# 或
cat /etc/os-release | head -3
```

应该看到 `Ubuntu 22.04.x LTS`。**如果是 24.04 也没问题**，脚本会自动识别
（24.04 自带 Python 3.12，会少装一步）。

### 1.2 确认内存与磁盘

```bash
free -h          # 内存建议 >= 2 GB
df -h /          # 磁盘建议 >= 10 GB 可用
```

项目本身很轻（代码 8 MB、依赖约 600 MB、前端产物 1 MB），
但 pip/npm 的缓存会占空间。**1 GB 内存的机器建议先加 swap**，否则
`npm run build` 有被 OOM 杀掉的可能：

```bash
# 仅当内存 < 2GB 时执行：加 2GB swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 1.3 确认能访问外网

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://github.com
curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/
curl -sS -o /dev/null -w '%{http_code}\n' https://registry.npmjs.org/
```

三个都应该返回 `200` 或 `301`。**如果有哪个不通**，先解决网络/代理问题，
否则后面装依赖一定卡住。国内服务器常见情况见
[第 10.7 节](#107-国内网络装依赖很慢或失败)。

### 1.4 拿到服务器 IP

```bash
hostname -I | awk '{print $1}'
```

记下这个 IP，最后浏览器要用它访问。

---

## 2. 安装系统依赖

**这一步可以跳过**：第 4 节的 `install.sh` 会自动做。
想手动一步步来、或者想先看清楚装了什么，看[第 11 节附录](#11-附录手动安装逐步说明)。

简单说需要三样东西：

| 依赖 | 版本要求 | 用途 | Ubuntu 22.04 自带 |
| --- | --- | --- | --- |
| Git | 任意 | 拉代码 | 通常已装 |
| Python | **3.11+** | 后端 | 只有 3.10 → **需要装** |
| Node.js | **20.19+ 或 22.12+** | 构建前端 | 没有 → **需要装** |

> **为什么 Python 必须 3.11+**：代码里用了 `X | None` 的联合类型标注、
> `datetime.UTC` 等 3.11 才稳定的特性。用 3.10 会在 import 阶段就报
> `TypeError`，而且报错位置看着和业务代码无关。

---

## 3. 从 GitHub 拉取代码

### 3.1 选择存放目录

推荐放在 `/opt` 下（系统服务的惯例位置）：

```bash
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
cd /opt
```

### 3.2 克隆仓库

```bash
git clone https://github.com/qjwmx/LKWG_RAG.git
cd LKWG_RAG
```

看到这样的输出就成功了：

```
Cloning into 'LKWG_RAG'...
remote: Enumerating objects: ...
Receiving objects: 100% ...
```

### 3.3 确认目录结构

```bash
ls -1
```

应该看到：

```
backend/          # FastAPI 后端
frontend/         # Vue 3 前端
deploy/           # 部署脚本与配置（本文用到的都在这里）
docs/             # 文档
README.md
```

> **如果 clone 很慢或失败**，见 [10.7 节](#107-国内网络装依赖很慢或失败) 的 GitHub 加速方案。

---

## 4. 一键安装（推荐）

### 4.1 执行安装脚本

```bash
cd /opt/LKWG_RAG
bash deploy/install.sh
```

脚本会依次做完这些事，**每步都会打印进度**：

| 步骤 | 做什么 | 耗时（参考） |
| --- | --- | --- |
| 1 | 装系统依赖（build-essential、curl、git、libsqlite3-dev…） | 1~3 分钟 |
| 2 | 装 Python 3.11+（22.04 走 deadsnakes，失败自动回退到 uv） | 1~5 分钟 |
| 3 | 装 Node.js 22（NodeSource 源） | 1~2 分钟 |
| 4 | 建 `backend/.venv` 并装 Python 依赖 | 2~5 分钟 |
| 5 | 装前端依赖并构建 `frontend/dist` | 1~3 分钟 |
| 6 | 生成 `backend/.env`（自动生成 JWT 密钥） | 秒级 |
| 7 | 导入精灵/技能/阵容种子数据 | 10~60 秒 |

全程 **5~15 分钟**（取决于网速）。

### 4.2 成功的标志

最后会看到：

```
==> 安装完成

  依赖都装好了，用下面任一方式启动：
  ...
  初始管理员账号：admin / admin  ⚠️ 请尽快修改
```

以及中间这几行关键输出：

```
  ✓ Python：python3.11 (Python 3.11.18)
  ✓ Node.js：v22.x.x / npm：10.x.x
  ✓ 后端依赖已安装
  ✓ 前端已构建：frontend/dist
  ✓ 已生成 backend/.env（JWT_SECRET 随机生成，AI 相关项留空）
  ✓ 种子数据已导入
```

种子数据那步会打印具体条数，**数字对得上就说明数据完整**：

```
{'type_matchup': 361, 'pets': 465, 'skills': 469, 'lineups': 151, 'lineup_members': 906}
```

### 4.3 脚本可选参数

| 参数 | 作用 |
| --- | --- |
| `--with-systemd` | 装成系统服务并立即启动（开机自启），见[第 7 节](#7-生产部署systemd--nginx) |
| `--with-nginx` | 额外装 Nginx 配置（通常和 `--with-systemd` 一起用） |
| `--skip-seed` | 跳过种子数据导入 |
| `--skip-apt` | 跳过 apt（已手动装好依赖时用） |
| `--no-build` | 跳过前端构建（复用已有 `frontend/dist`） |

可以用环境变量覆盖端口与域名：

```bash
APP_PORT=9000 SERVER_NAME=rag.example.com bash deploy/install.sh --with-systemd --with-nginx
```

### 4.4 脚本可以重复执行

**放心重跑**。每一步都是幂等的：

- 已装好的依赖不会重装
- `backend/.env` 已存在时**不会覆盖**（避免把你填的 key 冲掉）
- 种子数据是"先清表再写"，重复执行结果一致

> ⚠️ 唯一的注意点：如果之后用爬虫抓了新阵容，重跑 `install.sh` 会重新导入种子
> 数据，把抓来的阵容清掉。那种情况下用 `--skip-seed`。

---

## 5. 启动并验证

### 5.1 前台启动（第一次推荐这样）

前台跑能看到完整日志，出问题最好排查：

```bash
cd /opt/LKWG_RAG/backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

看到这几行就成功了：

```
INFO app.main: 已托管前端产物：/opt/LKWG_RAG/frontend/dist
INFO app.main: 已开启前端托管：打开 http://<本机地址>:端口/ 即为前端页面
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

> **为什么只需要一条命令**：`backend/.env` 里 `SERVE_FRONTEND=true`，
> 后端会把前端页面一起发出来。**不需要另外起前端服务，也不需要 Nginx。**
> 整个站点就一个端口（8000）。

> **`--host 0.0.0.0` 不能省**。默认只监听 `127.0.0.1`，那只允许服务器本机访问，
> 你的浏览器会连不上（表现为"连接被拒绝"，很容易误以为服务没起来）。

### 5.2 验证

**在服务器上**先自测：

```bash
# 1) 前端页面
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/
# 期望：HTTP 200

# 2) 后端接口（公开接口，不需要登录）
curl -sS http://127.0.0.1:8000/api/v1/public/showcase | head -c 200
# 期望：{"code":200,"msg":"success","data":{"pets":[...

# 3) 数据是否导进来了
curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'
# 期望：{"code":200,...,"access_token":"eyJ..."}
```

**在你自己电脑的浏览器**里打开：

```
http://<服务器IP>:8000/
```

用 `admin` / `admin` 登录。登录后应该看到：

- **阵容攻略**页：能按属性筛选精灵、点开看六维图、反查包含它的阵容
- **知识库**页：能上传文档、切分、看切片
- 右上角有主题切换、壁纸选择

### 5.3 云服务器打不开？先查安全组

如果服务器本机 `curl` 通、但浏览器打不开，**九成是云厂商的安全组/防火墙**。

**云厂商控制台**（阿里云/腾讯云/AWS/华为云）：
在实例的「安全组」里加一条入方向规则，放行 TCP **8000** 端口。

**服务器本机的 ufw**（Ubuntu 默认是关闭的，但如果开过就要放行）：

```bash
sudo ufw status                    # 看状态
sudo ufw allow 8000/tcp            # 放行 8000
sudo ufw reload
```

验证端口是否真的在监听：

```bash
sudo ss -tlnp | grep 8000
# 期望：LISTEN 0 2048 0.0.0.0:8000 ...
# 如果是 127.0.0.1:8000，说明启动时没加 --host 0.0.0.0
```

### 5.4 一键自检（推荐）

启动之后跑一遍自检脚本，它会逐项探测部署是否真的完整：

```bash
cd /opt/LKWG_RAG
bash deploy/check.sh
```

输出长这样：

```
1. 服务连通性（http://127.0.0.1:8000）
  ✓ 根路径可访问（HTTP 200）——前端页面正常
  ✓ 接口文档可访问（/docs）

2. 后端接口
  ✓ 公开接口正常（登录页展示数据：12 只精灵）

3. 登录与鉴权
  ✓ 默认管理员 admin/admin 可登录
  ! 管理员口令仍是默认值 —— 对外提供服务前务必修改

4. 领域数据（精灵 / 阵容）
  ✓ 精灵图鉴：465 条
  ✓ 阵容数据：151 套
  ✓ 阵容反查正常（迪莫出现在 9 套阵容里）

5. 依赖健康（数据库 / 嵌入 / 模型 / Redis）
  ✓ 数据库连接正常
  ! 嵌入用的是 hash 伪嵌入 —— 链路能跑通，但检索**没有语义意义**
  ! 没配 LLM_API_KEY —— 问答会返回可读的错误提示（其余功能不受影响）

6. 前端产物
  ✓ 前端静态资源可加载（assets/index-B_fVnb6j.js）
  ✓ SPA 路由刷新正常（/strategy 返回 200）
  ✓ 接口 404 是 JSON 而不是前端页面

检查结论
  通过 11 / 警告 4 / 失败 0（共 15 项）
```

**看结论那行就够了**：

- `失败 0` → 部署完整，可以用了
- 有 `!` 警告 → 基本可用，那些通常是 AI 配置没填（正常）
- 有 `✗` 失败 → 按提示修，或者查[第 10 节](#10-常见问题排查)

换地址/域名时把 URL 传进去：

```bash
bash deploy/check.sh http://your-domain.com
```

脚本**只读不写**，不改配置、不建数据、不触发模型调用（不花钱），可以随便跑。

### 5.5 停止服务

前台运行时按 `Ctrl + C`。

---

## 6. 配置 AI（可以留空）

**这一节是可选的。** 不做任何配置，下面这些功能已经是**真的可用**：

| 功能 | 留空时状态 |
| --- | --- |
| 精灵图鉴 / 属性筛选 / 六维图 | ✅ 完全可用 |
| 阵容反查（哪些阵容里有某只精灵） | ✅ 完全可用 |
| 队友统计、属性克制矩阵 | ✅ 完全可用 |
| 知识库上传 / 解析 / 切分 / 入库 | ✅ 完全可用 |
| 向量检索（能召回，但无语义质量） | ⚠️ 可用但"搜得不准" |
| 注册 / 审批 / 登录 / 管理后台 | ✅ 完全可用 |
| **问答与攻略生成** | ❌ 返回一条可读的错误提示 |

编辑配置文件：

```bash
nano /opt/LKWG_RAG/backend/.env
```

改完**必须重启后端**才生效（前台跑就 `Ctrl+C` 再起；systemd 就
`sudo systemctl restart lkwg-rag`）。

### 6.1 对话模型（要让问答出答案，这一项必须配）

任选一个 **OpenAI 兼容**的服务，填三行：

```env
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-你的key
LLM_MODEL=deepseek-chat
```

常见服务对照表：

| 服务 | `LLM_BASE_URL` | `LLM_MODEL` |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

> ⚠️ **DeepSeek 用户注意**：`STRUCTURED_OUTPUT_METHOD` 必须保持
> `function_calling`（默认值，别改）。DeepSeek 不支持 `json_schema`，
> 而这个错误**不会中断流程**——只会让 Planner 永远只拆一个单元、
> 检索结论退化成原文摘要，很容易被误判成"模型能力不行"。

### 6.2 嵌入模型（影响检索准不准）

当前 `backend/.env` 里是：

```env
EMBEDDING_PROVIDER=hash
EMBEDDING_DIM=2560
```

`hash` 是**确定性伪嵌入**：上传/切分/入库/检索的链路全是真的，只有"语义相似度"
是假的。它让你**不装任何外部服务**就能把整条链路跑通，`/system/health`
会把它标成 `degraded`，前端顶部也会提示。

三种升级方式，任选其一：

#### 方式 A：本地 Ollama（免费，推荐）

```bash
# 装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 拉嵌入模型（约 2.5 GB）
ollama pull qwen3-embedding:4b

# 确认在跑
curl -s http://localhost:11434/api/tags | head -c 200
```

改 `backend/.env`：

```env
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_DIM=2560
```

> **`EMBEDDING_DIM` 必须与模型实际输出维度一致**。`qwen3-embedding:4b`
> 就是 2560，所以默认值不用改。
>
> **换嵌入模型必须清空已有切片重新入库**：不同模型的向量不可比，
> 混在一张表里会让检索结果完全错乱，而且**不报错**。清空方式：
> `rm /opt/LKWG_RAG/backend/data/rag.db` 然后重启（会丢文档记录，
> 但种子数据会自动重新导入，重新上传文档即可）。
>
> **Ollama 必须保持运行**。它没启动时检索会失败，页面表现为
> 「未找到明确依据」——这是排查检索问题时最常见的原因。

#### 方式 B：云端嵌入服务

```env
EMBEDDING_PROVIDER=openai
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-你的key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
```

#### 方式 C：继续用 hash

什么都不用做。适合演示、跑通链路、只关心图鉴与阵容的场景。

### 6.3 联网检索（可选）

填上 Tavily 的 key，对话与攻略页的「允许联网」开关才会可用：

```env
TAVILY_API_KEY=tvly-你的key
```

申请地址：<https://tavily.com>。不填时开关显示「未配置」并禁用。

### 6.4 验证配置是否生效

登录后访问（`/docs` 里点 Authorize 也能测）：

```bash
TOKEN=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')

curl -sS http://127.0.0.1:8000/api/v1/system/health -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

期望（配好之后）：

```json
{
  "code": 200,
  "data": {
    "status": "ok",
    "postgres": { "ok": true, "detail": "连接正常" },
    "embedding": { "ok": true, "provider": "ollama", "detail": "ollama · qwen3-embedding:4b" },
    "llm": { "ok": true, "detail": "已配置 LLM_API_KEY" },
    "redis": { "ok": true, "configured": false, "detail": "未配置 REDIS_URL..." }
  }
}
```

`status` 为 `degraded` 表示有组件降级（比如还在用 hash 伪嵌入、没填 LLM key），
**功能仍然可用**，具体原因看各字段的 `detail`。

---

## 7. 生产部署：systemd + Nginx

第 5 节的前台启动适合验证，但关掉 SSH 就没了。正式对外提供服务用这一节。

### 7.1 方式一：只用 systemd（最简单，推荐）

后端同时托管前端，**一个端口搞定**，不需要 Nginx。

```bash
cd /opt/LKWG_RAG
bash deploy/install.sh --with-systemd
```

如果前面已经跑过 `install.sh`，这一步只会装服务并启动（几秒钟）。

### 7.2 方式二：systemd + Nginx（80 端口 / 域名 / HTTPS）

适合需要 `http://your-domain.com`（不带端口号）或要配 HTTPS 的场景。

```bash
cd /opt/LKWG_RAG
bash deploy/install.sh --with-systemd --with-nginx
```

装完 Nginx 在 80 端口，前端静态文件由 Nginx 直接发，`/api` 反代到后端 8000。

**建议同时改一个配置**（避免两套前端入口，排查问题时容易混淆）：

```bash
nano /opt/LKWG_RAG/backend/.env
# 把 SERVE_FRONTEND=true 改成 false
sudo systemctl restart lkwg-rag
```

> 两者都开也能用（Nginx 会优先命中静态文件），但职责不清晰。

### 7.3 常用命令

```bash
sudo systemctl status lkwg-rag       # 看状态（运行中会显示 active (running)）
sudo systemctl restart lkwg-rag      # 重启（改完 .env 必须重启）
sudo systemctl stop lkwg-rag         # 停止
sudo systemctl enable lkwg-rag       # 开机自启（install.sh 已自动做）
sudo journalctl -u lkwg-rag -f       # 实时看日志
sudo journalctl -u lkwg-rag -n 100   # 看最近 100 行
```

### 7.4 服务起不来的排查顺序

```bash
# 1) 看日志，90% 的问题这里直接有答案
sudo journalctl -u lkwg-rag -n 80 --no-pager

# 2) 手工前台跑一遍，看是否有更详细的报错
cd /opt/LKWG_RAG/backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3) 检查 .env 是否有语法错误（等号两边不能有空格，值不要加引号）
grep -vE '^\s*#|^\s*$' /opt/LKWG_RAG/backend/.env

# 4) 检查端口被占用
sudo ss -tlnp | grep 8000
```

### 7.5 配 HTTPS（有域名的话）

用 Let's Encrypt 免费证书，两条命令：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

`certbot` 会自动改 Nginx 配置并加上自动续期。**SSE 流式在 HTTPS 下照常工作**，
不需要额外配置。

### 7.6 安全加固清单（对外提供服务前必须做）

```bash
nano /opt/LKWG_RAG/backend/.env
```

- [ ] **改掉默认管理员口令**
      `ADMIN_PASSWORD=admin` → 换成强口令，然后 `sudo systemctl restart lkwg-rag`
- [ ] **确认 `JWT_SECRET` 非空**
      `install.sh` 已自动生成。如果手动部署过且它是空的，用这个生成：
      `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`
- [ ] **`.env` 权限**
      `sudo chmod 600 /opt/LKWG_RAG/backend/.env`
      （里面有 LLM key 和 JWT 密钥，不能让别人读到）
- [ ] **考虑关掉接口文档**
      对外提供服务时，`/docs` 暴露了全部接口结构。用 Nginx 方式部署时，
      删掉 `deploy/nginx/lkwg-rag.conf` 里 `location ~ ^/(docs|redoc|openapi\.json)$`
      那一段即可
- [ ] **多进程部署必须配 Redis**
      单进程（默认）用进程内缓存完全够用。但如果你改成 `--workers 4`，
      **必须**配 Redis，否则登录限流与令牌吊销不跨进程生效——
      这是"看起来正常、实则有安全缺口"的情况：

      ```bash
      sudo apt install -y redis-server
      # 然后 backend/.env 里设：
      # REDIS_URL=redis://127.0.0.1:6379/0
      ```

### 7.7 备份

需要备份的只有 `backend/data/` 一个目录：

```bash
# 手动备份
sudo tar czf ~/lkwg-backup-$(date +%F).tar.gz -C /opt/LKWG_RAG/backend data/

# 每天凌晨 3 点自动备份（保留 7 天）
(crontab -l 2>/dev/null; echo '0 3 * * * cd /opt/LKWG_RAG/backend && tar czf ~/lkwg-backup-$(date +\%F).tar.gz data/ && find ~ -name "lkwg-backup-*.tar.gz" -mtime +7 -delete') | crontab -
```

里面包含：SQLite 业务库、checkpoint 库、上传的原始文档。

**代码本身不需要备份**——从 GitHub 重新 clone 即可。

---

## 8. Docker 方式（可选）

如果服务器已经装了 Docker，或者你更喜欢容器化，用这条路径。
**同样只需要一个端口**，不需要额外的 Nginx 容器。

### 8.1 前置条件

```bash
docker --version           # 需要 20.10+
docker compose version     # 需要 v2（注意是 `docker compose`，中间有空格）
```

没装 Docker 的话：

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"     # 免 sudo 用 docker
newgrp docker                       # 当前会话生效
```

### 8.2 准备配置

```bash
cd /opt/LKWG_RAG
cp backend/.env.example backend/.env

# 生成 JWT 密钥并写进去
python3 -c "import secrets;print('JWT_SECRET='+secrets.token_urlsafe(48))" >> backend/.env

# 改成后端托管前端（容器里就是这么用的）
sed -i 's/^SERVE_FRONTEND=.*/SERVE_FRONTEND=true/' backend/.env
```

### 8.3 构建并启动

```bash
cd /opt/LKWG_RAG
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

第一次构建约 **3~6 分钟**（要下基础镜像 + 装两套依赖 + 构建前端）。

看启动日志：

```bash
docker compose -f deploy/docker/docker-compose.yml logs -f
```

应该看到种子数据自动导入：

```
INFO entrypoint: 领域表为空，导入种子数据（首次启动，约需 10~60 秒）
INFO entrypoint: 种子数据导入完成：{'type_matchup': 361, 'pets': 465, ...}
INFO:     Application startup complete.
```

### 8.4 验证

```bash
# 用 compose 跑自检（容器名解析不进去，用宿主机地址）
bash deploy/check.sh http://127.0.0.1:8000
```

或者手动：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/
# 期望：HTTP 200
```

浏览器打开 `http://<服务器IP>:8000/`，`admin` / `admin` 登录。

### 8.5 常用命令

```bash
cd /opt/LKWG_RAG
COMPOSE="docker compose -f deploy/docker/docker-compose.yml"

$COMPOSE ps                    # 看状态
$COMPOSE logs -f               # 实时日志
$COMPOSE restart               # 重启（改完 .env 用这个）
$COMPOSE down                  # 停止（数据卷保留）
$COMPOSE down -v               # 停止**并删除数据**（谨慎）
```

### 8.6 数据持久化

数据在 Docker 卷 `lkwg-rag-data` 里，映射到容器的 `/app/backend/data`。
**容器重建不会丢数据**（除非 `down -v`）。

备份：

```bash
docker run --rm \
  -v lkwg-rag-data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/lkwg-backup-$(date +%F).tar.gz -C /data .
```

### 8.7 容器里连宿主机的 Ollama / Redis

容器里的 `127.0.0.1` 是**容器自己**，不是宿主机。要用特殊域名：

```yaml
# deploy/docker/docker-compose.yml 的 environment 段里取消注释并改：
environment:
  OLLAMA_BASE_URL: http://host.docker.internal:11434
  REDIS_URL: redis://redis:6379/0
```

宿主机侧 Ollama 要监听所有网卡（默认只监听 127.0.0.1）：

```bash
sudo systemctl edit ollama
# 加入：
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

### 8.8 Docker 方式改代码后怎么更新

```bash
cd /opt/LKWG_RAG
git pull
docker compose -f deploy/docker/docker-compose.yml up -d --build
```

---

## 9. 日常运维

### 9.1 更新代码

```bash
cd /opt/LKWG_RAG
git pull

# 如果依赖有变化（requirements.txt / package.json 改了）
bash deploy/install.sh --skip-seed --no-build   # 补装后端依赖
cd frontend && npm ci && npm run build && cd ..  # 重新构建前端

# 重启
sudo systemctl restart lkwg-rag        # systemd 方式
# 或
docker compose -f deploy/docker/docker-compose.yml up -d --build   # Docker 方式
```

### 9.2 更新领域数据（阵容/精灵/技能）

种子数据会过时（当前停留在 2026-05 前后，游戏已到 S4 赛季）。
爬虫支持多个数据源，**抓取与入库分离**：

```bash
cd /opt/LKWG_RAG/backend

# 看有哪些源
.venv/bin/python -m app.scraper.run_scrape --list-sources

# BWIKI：先抓 30 套试水（确认网络没被拦）
.venv/bin/python -m app.scraper.run_scrape --source bwiki --limit 30

# B 站：抓当赛季的「阵容码」配队
.venv/bin/python -m app.scraper.run_scrape --source bilibili --limit 10

# 继续上次中断的抓取（跳过已抓的）
.venv/bin/python -m app.scraper.run_scrape --source bwiki --resume

# 确认抓完整了，再写进数据库
.venv/bin/python -m app.scraper.run_scrape --import all
```

**同步精灵头像与技能图标**：

```bash
cd /opt/LKWG_RAG/backend
.venv/bin/python -c "from app.scraper.images import sync_all; print(sync_all(force=True))"
.venv/bin/python -m app.scraper.seed_loader    # 再重新导入，把 URL 写进数据库
```

> ⚠️ 爬虫抓的数据写库后，**不要再用 `install.sh` 跑种子导入**——
> 那会清表重写。要重跑脚本时加 `--skip-seed`。

### 9.3 查看日志

```bash
# systemd
sudo journalctl -u lkwg-rag -f
sudo journalctl -u lkwg-rag --since "1 hour ago"

# Docker
docker compose -f deploy/docker/docker-compose.yml logs -f --tail 100

# 只看错误
sudo journalctl -u lkwg-rag -p err --no-pager -n 50
```

### 9.4 用户管理

- **注册需审批**：新用户注册后状态是 `pending`，**登录会被拒绝**。
  管理员在 **管理后台 → 用户审批** 里点「通过」才能登录。
- **root 账号不落库**：由 `backend/.env` 的 `ADMIN_USERNAME` / `ADMIN_PASSWORD`
  决定，改完重启生效。
- **提升管理员**：管理后台 → 用户审批 → 改角色（受 `ALLOW_ADMIN_PROMOTION` 控制）。

### 9.5 知识库文档

- 支持 `.txt` `.md` `.markdown` `.pdf` `.docx` `.xlsx` `.csv`
- 单文件上限 50 MB（`UPLOAD_MAX_MB` 可调）
- 普通用户上传进**自己的私有库**，管理员上传进**公共库**
- 同内容文档（按 sha256）重复上传会被识别为「已存在」并跳过

---

## 10. 常见问题排查

### 10.1 浏览器打不开，但服务器上 `curl` 正常

按顺序查：

```bash
# 1) 端口监听地址对不对（必须是 0.0.0.0，不能是 127.0.0.1）
sudo ss -tlnp | grep 8000

# 2) 本机防火墙
sudo ufw status

# 3) 云厂商安全组（去控制台加 TCP 8000 入方向规则）
```

### 10.2 页面能打开，但所有接口都报「无法连接后端服务」

前端代码里用的是相对路径 `/api/v1/...`，所以只可能是**同源的反向代理没配好**：

- **后端托管前端**（`SERVE_FRONTEND=true`）：不应该出现这个错。
  检查是不是访问了别的端口（比如错误地访问了 5173）。
- **Nginx 方式**：检查 `/etc/nginx/sites-available/lkwg-rag` 里
  `location /api/` 的 `proxy_pass` 端口与后端实际端口是否一致。
  `sudo nginx -t && sudo systemctl reload nginx`
- **前后端分机部署**：需要在 `frontend/` 下建 `.env` 写
  `VITE_API_TARGET=http://后端IP:8000`，但**生产环境不推荐**——用同源代理更简单。

### 10.3 登录提示「用户名或口令错误」（但账号密码没错）

三种情况共用这一句提示（刻意不区分，防止用户名枚举）：

1. 用户不存在
2. 口令错误
3. **账号还在 `pending` 状态**

如果是刚注册的账号，让管理员在**管理后台 → 用户审批**里通过。

### 10.4 问答返回「资料不足」或「未找到明确依据」

这是**检索没拿到材料**，不是模型的问题。按顺序查：

```bash
# 1) 知识库里到底有没有可检索的切片？
TOKEN=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')
curl -sS http://127.0.0.1:8000/api/v1/knowledge_base/list_files \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30

# 2) 嵌入服务在不在？
curl -sS http://127.0.0.1:8000/api/v1/system/health \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# 3) 如果用了 Ollama，确认它真的在跑
curl -s http://localhost:11434/api/tags | head -c 300
```

最常见的两个原因：

- **Ollama 没启动**（`systemctl status ollama`）
- **知识库是空的**：先上传几份文档，或用管理员的「导入示例文档」按钮

### 10.5 问答报「LLM_API_KEY 未配置」或超时

```bash
# 检查 .env 里有没有填（注意不要有多余空格或引号）
grep '^LLM_' /opt/LKWG_RAG/backend/.env

# 检查能不能连上你的模型服务
curl -sS -o /dev/null -w '%{http_code}\n' https://api.deepseek.com
```

改完 `.env` **必须重启**：

```bash
sudo systemctl restart lkwg-rag
```

### 10.6 SSE 流式输出变成"一次性全出来"

说明中间有代理在缓冲响应。检查：

- **Nginx 方式**：确认配置里有 `proxy_buffering off;`
  （`deploy/nginx/lkwg-rag.conf` 已经写了，`sudo nginx -t && sudo systemctl reload nginx`）
- **又套了一层 CDN/负载均衡**：那层的缓冲也要关，或者直接绕过它
- **后端托管前端**（单端口）：不应该有这个问题，`X-Accel-Buffering: no` 已由后端发出

### 10.7 国内网络装依赖很慢或失败

#### pip 慢

```bash
# 全局换清华源
mkdir -p ~/.pip
cat > ~/.pip/pip.conf <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
EOF
```

#### npm 慢

```bash
npm config set registry https://registry.npmmirror.com
```

#### GitHub 慢或 clone 失败

```bash
# 方案一：用镜像站
git clone https://gitee.com/mirrors_trending/LKWG_RAG.git   # 如果存在镜像
# 或者用 ghproxy 类加速：
git clone https://ghproxy.net/https://github.com/qjwmx/LKWG_RAG.git

# 方案二：本地下载 zip，传到服务器
# 在能上网的机器上下载 https://github.com/qjwmx/LKWG_RAG/archive/refs/heads/main.zip
# 然后 scp 到服务器解压
```

#### NodeSource 源慢

`install.sh` 用的是 NodeSource。如果它装不上，用 Node 官方二进制包：

```bash
# 手动装 Node 22
cd /tmp
curl -fsSLO https://nodejs.org/dist/v22.20.0/node-v22.20.0-linux-x64.tar.xz
sudo tar -xJf node-v22.20.0-linux-x64.tar.xz -C /usr/local --strip-components=1
node -v && npm -v
```

#### deadsnakes PPA 装不上 Python 3.11

`install.sh` 已经内置了自动回退到 `uv` 的逻辑。如果两条都失败，手动装：

```bash
# 用 uv（推荐，不依赖 launchpad）
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv python install 3.12
PYTHON_BIN=$(~/.local/bin/uv python find 3.12) bash deploy/install.sh
```

### 10.8 `npm run build` 报 `EBUSY` 或内存不足

- **内存不足**：先加 swap（见 [1.2 节](#12-确认内存与磁盘)），
  或者用 `NODE_OPTIONS=--max-old-space-size=1024 npm run build`
- **`EBUSY` 文件锁**：说明有别的进程在写 `src/`（编辑器、同步盘）。
  停掉它再构建

### 10.9 端口 8000 被占用

```bash
sudo ss -tlnp | grep 8000      # 看是谁占的
```

换一个端口启动：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 9000
```

用 systemd 方式时改端口：

```bash
APP_PORT=9000 bash deploy/install.sh --with-systemd
```

### 10.10 重启后登录状态丢了

`backend/.env` 里 `JWT_SECRET` 是空的，每次启动随机生成，旧 token 全部失效。

```bash
# 生成一个固定的密钥
python3 -c "import secrets;print(secrets.token_urlsafe(48))"
# 填进 backend/.env：
# JWT_SECRET=上面输出的值
sudo systemctl restart lkwg-rag
```

`install.sh` 生成的 `.env` 已经自动填好了这一项。

### 10.11 想看后端接口文档

浏览器打开 `http://<服务器IP>:8000/docs`。点右上角 **Authorize**，
填登录拿到的 token 就能测所有接口。

> 对外提供服务时建议关掉（见 [7.6 节](#76-安全加固清单对外提供服务前必须做)）。

---

## 11. 附录：手动安装逐步说明

不想用一键脚本时，照着这里一步步来。**和 `install.sh` 做的事完全一致。**

### 11.1 装 Git

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
```

### 11.2 装 Python 3.11+

**Ubuntu 22.04（自带 3.10，需要装 3.11）**：

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
python3.11 --version        # 期望：Python 3.11.x
```

> 如果 `add-apt-repository` 卡住或报错（launchpad 不可达），改用 uv：
> ```bash
> curl -LsSf https://astral.sh/uv/install.sh | sh
> ~/.local/bin/uv python install 3.12
> ```

**Ubuntu 24.04（自带 3.12，直接跳过）**：

```bash
python3 --version           # 期望：Python 3.12.x
sudo apt install -y python3-venv python3-dev
```

### 11.3 装 Node.js 22

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource.sh
sudo bash /tmp/nodesource.sh
sudo apt install -y nodejs
node -v        # 期望：v22.x.x
npm -v
```

### 11.4 拉代码

```bash
sudo mkdir -p /opt && sudo chown "$USER":"$USER" /opt
cd /opt
git clone https://github.com/qjwmx/LKWG_RAG.git
cd LKWG_RAG
```

### 11.5 装后端依赖

```bash
cd /opt/LKWG_RAG/backend
python3.11 -m venv .venv                      # 24.04 用 python3
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

> 依赖约 60 个包，其中 `numpy` / `lxml` / `tiktoken` 有预编译轮子，
> 正常 2~5 分钟装完。如果卡在编译（看到 `Building wheel for ...`），
> 说明缺 `build-essential`：
> `sudo apt install -y build-essential libffi-dev libssl-dev libsqlite3-dev`

### 11.6 装前端依赖并构建

```bash
cd /opt/LKWG_RAG/frontend
npm ci                  # 没有 package-lock.json 时用 npm install
npm run build           # 产物在 frontend/dist
```

`npm run build` 会先跑 `vue-tsc --noEmit` 做类型检查，再 `vite build`。
看到 `✓ built in x.xxs` 就成功了。

### 11.7 生成配置

```bash
cd /opt/LKWG_RAG/backend
cp .env.example .env
chmod 600 .env

# 生成 JWT 密钥并写进 .env
SECRET=$(.venv/bin/python -c "import secrets;print(secrets.token_urlsafe(48))")
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$SECRET|" .env

# 打开后端托管前端
sed -i "s|^SERVE_FRONTEND=.*|SERVE_FRONTEND=true|" .env

# 改管理员口令（强烈建议）
sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=你的强口令|" .env
```

### 11.8 导入种子数据

```bash
cd /opt/LKWG_RAG/backend
.venv/bin/python -m app.scraper.seed_loader
```

期望输出：

```
{'type_matchup': 361, 'pets': 465, 'skills': 469, 'lineups': 151, 'lineup_members': 906}
```

### 11.9 启动

```bash
cd /opt/LKWG_RAG/backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://<服务器IP>:8000/`，`admin` / 你设的口令登录。

### 11.10 装成系统服务

```bash
# 生成 service 文件
sed -e "s|@APP_DIR@|/opt/LKWG_RAG|g" \
    -e "s|@RUN_USER@|$USER|g" \
    -e "s|@APP_PORT@|8000|g" \
    /opt/LKWG_RAG/deploy/systemd/lkwg-rag.service | sudo tee /etc/systemd/system/lkwg-rag.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now lkwg-rag
sudo systemctl status lkwg-rag
```

---

## 附：目录与文件速查

```
/opt/LKWG_RAG/
├── backend/
│   ├── .venv/                  # Python 虚拟环境（install.sh 创建）
│   ├── .env                    # ★ 配置文件（所有配置都在这）
│   ├── app/                    # 应用代码
│   ├── data/                   # ★ 运行时数据（备份这一个目录就够）
│   │   ├── rag.db              #   业务库（SQLite）
│   │   ├── checkpoints.db      #   多轮对话记忆
│   │   └── raw_documents/      #   上传的原始文档
│   ├── data/_seed/             # 种子数据（精灵/技能/阵容）
│   └── requirements.txt
├── frontend/
│   ├── dist/                   # ★ 构建产物（后端或 Nginx 托管它）
│   ├── node_modules/
│   └── package.json
├── deploy/
│   ├── install.sh              # 一键安装脚本
│   ├── check.sh                # ★ 部署自检（启动后跑一遍）
│   ├── systemd/lkwg-rag.service
│   ├── nginx/lkwg-rag.conf
│   └── docker/
│       ├── Dockerfile
│       ├── docker-compose.yml
│       └── entrypoint.py
└── docs/DEPLOY_UBUNTU.md       # 本文
```

| 我想改… | 改哪里 | 改完要做什么 |
| --- | --- | --- |
| 端口 | `--port` 参数 / systemd 的 `APP_PORT` | 重启服务 |
| 管理员账号口令 | `backend/.env` 的 `ADMIN_*` | 重启服务 |
| AI 模型 | `backend/.env` 的 `LLM_*` | 重启服务 |
| 嵌入方式 | `backend/.env` 的 `EMBEDDING_*` | 重启 + 清空切片重新入库 |
| 上传大小上限 | `backend/.env` 的 `UPLOAD_MAX_MB` | 重启；用 Nginx 时还要改 `client_max_body_size` |
| 前端界面 | `frontend/src/` | `cd frontend && npm run build` |

---

## 附：最短路径（TL;DR）

已经装好 Docker 的话：

```bash
git clone https://github.com/qjwmx/LKWG_RAG.git && cd LKWG_RAG
cp backend/.env.example backend/.env
sed -i 's/^SERVE_FRONTEND=.*/SERVE_FRONTEND=true/' backend/.env
docker compose -f deploy/docker/docker-compose.yml up -d --build
# 打开 http://<服务器IP>:8000/  admin / admin
```

没有 Docker 的话：

```bash
git clone https://github.com/qjwmx/LKWG_RAG.git && cd LKWG_RAG
bash deploy/install.sh
cd backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 打开 http://<服务器IP>:8000/  admin / admin
```

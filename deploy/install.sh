#!/usr/bin/env bash
#
# Ubuntu 一键部署脚本（22.04 / 24.04）
# =============================================================================
#
# 在**仓库根目录**执行：
#
#     bash deploy/install.sh
#
# 它会依次做完这些事（每一步都可重复执行，已完成的会跳过）：
#
#   1. 装系统依赖：build-essential / curl / git / libsqlite3-dev 等
#   2. 装 Python 3.11+（22.04 自带 3.10，会加 deadsnakes PPA；24.04 自带 3.12）
#   3. 装 Node.js 22 LTS（NodeSource 源）
#   4. 建后端虚拟环境 backend/.venv 并安装 requirements.txt
#   5. 安装前端依赖并构建 frontend/dist
#   6. 生成 backend/.env（已存在则不动），自动生成 JWT_SECRET
#   7. 导入精灵/技能/阵容种子数据（否则图鉴和阵容页是空的）
#
# 装完之后用下面任一方式启动（详见 docs/DEPLOY_UBUNTU.md）：
#
#     # 前台跑，看日志最方便
#     cd backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
#
#     # 或者装成系统服务（开机自启）
#     bash deploy/install.sh --with-systemd
#
# 可选参数：
#   --with-systemd     额外安装 systemd 服务并立即启动
#   --with-nginx       额外安装 Nginx 配置（需要 --with-systemd）
#   --skip-seed        跳过种子数据导入
#   --skip-apt         跳过 apt 安装（已经手动装好依赖时用）
#   --no-build         跳过前端构建（复用已有 frontend/dist）
#
# 可用环境变量：
#   APP_PORT=8000          后端监听端口
#   SERVER_NAME=_           Nginx server_name（默认 _ 表示任意域名/IP）
#   NODE_MAJOR=22          Node.js 主版本
#   PYTHON_BIN=python3.11  指定已有的 Python 解释器
#
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------- 基础变量

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

APP_PORT="${APP_PORT:-8000}"
SERVER_NAME="${SERVER_NAME:-_}"
NODE_MAJOR="${NODE_MAJOR:-22}"
PYTHON_BIN="${PYTHON_BIN:-}"

WITH_SYSTEMD=0
WITH_NGINX=0
SKIP_SEED=0
SKIP_APT=0
NO_BUILD=0

for arg in "$@"; do
  case "$arg" in
    --with-systemd) WITH_SYSTEMD=1 ;;
    --with-nginx)   WITH_NGINX=1 ;;
    --skip-seed)    SKIP_SEED=1 ;;
    --skip-apt)     SKIP_APT=1 ;;
    --no-build)     NO_BUILD=1 ;;
    -h|--help)      sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

# 颜色。不是 TTY 时自动关掉，避免日志里出现一堆转义字符。
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_INFO=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_INFO=""; C_OFF=""
fi

step()  { printf '\n%s==> %s%s\n' "$C_INFO" "$*" "$C_OFF"; }
ok()    { printf '%s  ✓ %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn()  { printf '%s  ! %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
fail()  { printf '%s  ✗ %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }

# --------------------------------------------------------------------------- 权限

# 用 root 跑就不加 sudo；普通用户跑就全程 sudo。
if [ "$(id -u)" -eq 0 ]; then
  RUN_USER="${SUDO_USER:-root}"
else
  command -v sudo >/dev/null 2>&1 || fail "当前不是 root，且没装 sudo。请用 root 运行，或先装 sudo。"
  RUN_USER="$(id -un)"
fi

# 以 root 身份执行命令。
#
# **必须用函数而不是 `SUDO="sudo"` 变量**：变量在 root 下是空串，
# 于是 `$SUDO DEBIAN_FRONTEND=noninteractive apt-get ...` 会被解析成
# 「执行名为 DEBIAN_FRONTEND=noninteractive 的命令」——报
# `command not found`，而且因为 set -e 的行为取决于位置，很容易看起来"跑完了"
# 其实什么都没装。函数写法在两种身份下都是合法的命令形式。
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

# 带重试的 apt install。
#
# **必须重试**：官方 Ubuntu 源（archive.ubuntu.com / security.ubuntu.com）
# 在部分网络下会间歇性返回 502 Bad Gateway 或连接重置——实测一次完整的
# 安装里就能碰到。直接失败会让用户以为脚本有问题，其实只是源抖了一下。
# 重试之间先 `apt-get update` 刷新索引（502 常常是索引与包状态不一致导致的）。
apt_install() {
  local attempt=1
  local max_attempts=3
  while [ "$attempt" -le "$max_attempts" ]; do
    if as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      warn "apt 安装失败（第 $attempt/$max_attempts 次，可能是源返回 502），5 秒后重试…"
      sleep 5
      as_root apt-get update -qq >/dev/null 2>&1 || true
    fi
    attempt=$((attempt + 1))
  done
  # 最后一次把真实输出放出来，否则用户只看到"失败"却不知道原因
  warn "apt 安装重试 $max_attempts 次仍失败，下面是真实报错："
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" || true
  return 1
}

# 带重试的 apt update。同样是为了抗住源的间歇性 502。
apt_update() {
  local attempt=1
  while [ "$attempt" -le 3 ]; do
    if as_root apt-get update -qq >/dev/null 2>&1; then
      return 0
    fi
    warn "apt-get update 失败（第 $attempt/3 次，可能是源返回 502），5 秒后重试…"
    sleep 5
    attempt=$((attempt + 1))
  done
  warn "apt-get update 连续失败，后续安装可能失败。检查网络或换镜像源（见文档 10.7 节）。"
  return 1
}

# 仓库所在目录要让运行用户可读写（运行时会在 backend/data/ 下建库和存原文）。
if [ "$RUN_USER" != "root" ] && [ -d "$BACKEND_DIR/data" ]; then
  chown -R "$RUN_USER":"$RUN_USER" "$BACKEND_DIR/data" 2>/dev/null || true
fi

# --------------------------------------------------------------------------- 系统检查

step "检查系统环境"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "系统：${PRETTY_NAME:-未知}"
  case "${ID:-}" in
    ubuntu|debian) ;;
    *) warn "当前系统是 ${ID:-未知}，本脚本按 Ubuntu 22.04/24.04 编写，其他发行版可能需要手动调整。" ;;
  esac
else
  warn "读不到 /etc/os-release，无法确认发行版，继续尝试。"
fi

if [ ! -f "$BACKEND_DIR/app/main.py" ] || [ ! -f "$FRONTEND_DIR/package.json" ]; then
  fail "没在仓库根目录找到 backend/app/main.py 与 frontend/package.json。
       请确认已经把完整仓库 clone 下来，并从这个仓库里运行本脚本。
       当前识别到的仓库根目录：$REPO_ROOT"
fi
ok "仓库根目录：$REPO_ROOT"

# 磁盘空间：装依赖 + 构建大约需要 2~3 GB。
AVAIL_MB=$(df -Pm "$REPO_ROOT" | awk 'NR==2 {print $4}')
if [ -n "${AVAIL_MB:-}" ] && [ "$AVAIL_MB" -lt 2048 ]; then
  warn "磁盘剩余 ${AVAIL_MB}MB，低于建议的 2048MB。装依赖 + 构建可能失败。"
fi

# --------------------------------------------------------------------------- apt 依赖

if [ "$SKIP_APT" -eq 0 ]; then
  step "安装系统依赖（apt）"

  apt_update
  apt_install build-essential curl ca-certificates gnupg git \
    libsqlite3-dev sqlite3 libffi-dev libssl-dev \
    software-properties-common \
    || fail "系统依赖装不上。检查网络/换镜像源（见文档 10.7 节），或手动装：
             sudo apt update && sudo apt install -y build-essential curl git libsqlite3-dev"
  ok "系统依赖已就绪"
else
  warn "跳过 apt（--skip-apt）"
fi

# --------------------------------------------------------------------------- Python 3.11+

step "准备 Python 3.11+"

# 判断一个解释器是否够用（要求 >= 3.11）
py_ok() {
  local bin="$1"
  [ -n "$bin" ] || return 1
  command -v "$bin" >/dev/null 2>&1 || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

# 用 deadsnakes PPA 装 Python 3.11（Ubuntu 22.04 专用路径）。
#
# **必须容忍失败**：add-apt-repository 会去 launchpad.net 拉 PPA 元数据，
# 国内网络下经常超时或被重置（实测报 IncompleteRead）。失败不能直接退出，
# 下面还有 uv 这条备用路径。
try_deadsnakes() {
  warn "尝试 deadsnakes PPA 安装 python3.11（需要能访问 launchpad.net）"

  if ! as_root add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1; then
    warn "deadsnakes PPA 添加失败（launchpad.net 不可达），改用备用方案"
    return 1
  fi
  if ! apt_update; then
    warn "添加 PPA 后 apt-get update 失败，改用备用方案"
    return 1
  fi
  if ! apt_install python3.11 python3.11-venv python3.11-dev; then
    warn "python3.11 安装失败，改用备用方案"
    return 1
  fi
  PYTHON_BIN="python3.11"
  py_ok "$PYTHON_BIN" || return 1
  return 0
}

# 用 uv 装一个独立 Python（不碰系统包管理器）。
#
# 这条路径在"launchpad 不通但 pypi 通"的机器上能成功，而国内这种情况很常见。
# uv 会把 Python 装到 ~/.local/share/uv/python/ 下，完全不影响系统自带的 3.10。
try_uv() {
  warn "尝试用 uv 安装独立 Python 3.12（不依赖 launchpad）"

  local uv_bin=""
  if command -v uv >/dev/null 2>&1; then
    uv_bin="uv"
  else
    # 官方安装脚本。同样先下载再执行，理由见下面 Node.js 那段。
    local uv_sh
    uv_sh="$(mktemp)"
    if curl -fsSL https://astral.sh/uv/install.sh -o "$uv_sh" 2>/dev/null; then
      sh "$uv_sh" >/dev/null 2>&1 || true
    fi
    rm -f "$uv_sh"

    # 安装脚本会把 uv 放到 ~/.local/bin 或 ~/.cargo/bin
    for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
      [ -x "$cand" ] && { uv_bin="$cand"; break; }
    done
    command -v uv >/dev/null 2>&1 && uv_bin="$(command -v uv)"
  fi

  if [ -z "$uv_bin" ]; then
    warn "uv 安装失败（pypi/astral 也不可达？）"
    return 1
  fi

  if ! "$uv_bin" python install 3.12 >/dev/null 2>&1; then
    warn "uv python install 3.12 失败"
    return 1
  fi

  local uv_py
  uv_py="$("$uv_bin" python find 3.12 2>/dev/null || true)"
  if [ -z "$uv_py" ] || [ ! -x "$uv_py" ]; then
    warn "uv 装完 Python 但找不到解释器路径"
    return 1
  fi

  PYTHON_BIN="$uv_py"
  ok "uv 提供的 Python：$("$PYTHON_BIN" -V 2>&1)（$PYTHON_BIN）"
  return 0
}

if [ -n "$PYTHON_BIN" ] && py_ok "$PYTHON_BIN"; then
  ok "使用指定的 Python：$PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"
else
  PYTHON_BIN=""
  # 从高到低找一个已经装好的
  for cand in python3.13 python3.12 python3.11 python3; do
    if py_ok "$cand"; then PYTHON_BIN="$cand"; break; fi
  done

  if [ -z "$PYTHON_BIN" ]; then
    if [ "$SKIP_APT" -eq 1 ]; then
      fail "跳过了 apt（--skip-apt），但系统里没有 Python 3.11+。
           请先手动安装 Python 3.11+，或用 PYTHON_BIN=/path/to/python3.11 指定。"
    fi

    # 两条路径依次尝试：先 apt（22.04 的标准做法），失败再 uv。
    # 两条都失败才报错，并在错误信息里写清楚手动怎么装——
    # 这一步卡住时，用户最需要的是"照做就能过"的指令。
    try_deadsnakes || try_uv || fail "装不上 Python 3.11+：deadsnakes PPA 与 uv 两条路都失败了。
           手动安装任选其一：
             · 装 python3.11：sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv
             · 用 uv：curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
           装好后重新运行本脚本，或指定解释器：
             PYTHON_BIN=/usr/bin/python3.11 bash deploy/install.sh"
  fi
  ok "Python：$PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"
fi

# venv 模块单独检查：有些精简系统装了 python3 但没装 python3-venv。
# uv 装出来的 Python 自带 venv，所以这里失败也不一定真缺——先试再说。
if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  warn "缺少 venv 模块，尝试安装 python3-venv"
  apt_install python3-venv || warn "python3-venv 安装失败，稍后创建虚拟环境时可能报错"
fi

# --------------------------------------------------------------------------- Node.js

step "准备 Node.js"

node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  # 版本要求来自 Vite 7 的 engines 字段：^20.19.0 || >=22.12.0。
  # 用 node 自己算，避免解析 `node -v` 的字符串（v20.19.0 的 'v' 前缀等）。
  node -e '
    const [maj, min] = process.versions.node.split(".").map(Number);
    const ok = (maj === 20 && min >= 19) || maj > 20;
    process.exit(ok ? 0 : 1);
  ' 2>/dev/null
}

if node_ok; then
  ok "Node.js 已满足要求：$(node -v)"
else
  if [ "$SKIP_APT" -eq 1 ]; then
    fail "跳过了 apt（--skip-apt），但没找到满足要求的 Node.js（需要 20.19+ 或 22.12+）。"
  fi
  warn "安装 Node.js ${NODE_MAJOR}.x（NodeSource 源）"

  # 先把 setup 脚本下载到临时文件再执行。
  # **不能直接 `curl | sudo bash`**：管道会把脚本喂给 sudo 的 stdin，
  # 而 sudo 默认要读终端输入口令，两者会互相抢——表现为脚本内容被当成
  # 口令读走、然后报"安装失败但没有原因"。
  NODESOURCE_SH="$(mktemp)"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$NODESOURCE_SH" \
    || { rm -f "$NODESOURCE_SH"; fail "下载 NodeSource 安装脚本失败，请检查网络/代理。"; }
  as_root bash "$NODESOURCE_SH" >/dev/null
  rm -f "$NODESOURCE_SH"

  apt_install nodejs
  node_ok || fail "Node.js 安装后版本仍不满足要求（当前：$(node -v 2>/dev/null || echo 未安装)）"
  ok "Node.js：$(node -v) / npm：$(npm -v)"
fi

# --------------------------------------------------------------------------- 后端

step "安装后端依赖（backend/.venv）"

# 已有的虚拟环境**必须验证可用**再复用。
#
# 为什么不能只看 `.venv/bin/python` 存不存在：venv 里的 python 是**符号链接**，
# 指向创建它的那个解释器。如果那个解释器后来没了（典型场景：用 uv 装的
# Python 在 ~/.local/share/uv/ 下，被清理掉了；或者换了用户/机器），
# 文件还在但一执行就报 "No such file or directory"，而报错信息完全看不出
# 是"解释器丢了"。所以这里真的执行一次。
venv_ok() {
  [ -x "$BACKEND_DIR/.venv/bin/python" ] || return 1
  "$BACKEND_DIR/.venv/bin/python" -c 'import sys' >/dev/null 2>&1 || return 1
  # 顺带确认版本够用（旧 venv 可能是用 3.10 建的）
  "$BACKEND_DIR/.venv/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null
}

if venv_ok; then
  ok "复用已有虚拟环境：backend/.venv ($("$BACKEND_DIR/.venv/bin/python" -V 2>&1))"
else
  if [ -e "$BACKEND_DIR/.venv" ]; then
    warn "已有 backend/.venv 但不可用（解释器丢失或版本过低），重建它"
    rm -rf "$BACKEND_DIR/.venv"
  fi
  "$PYTHON_BIN" -m venv "$BACKEND_DIR/.venv" \
    || fail "创建虚拟环境失败。手动试试：$PYTHON_BIN -m venv $BACKEND_DIR/.venv"
  venv_ok || fail "虚拟环境创建后仍不可用（$BACKEND_DIR/.venv/bin/python 无法执行）。
           这通常说明 $PYTHON_BIN 是个符号链接，指向的解释器实际不存在。
           用 PYTHON_BIN=/usr/bin/python3.11 指定一个真实的解释器再试。"
  ok "已创建虚拟环境：backend/.venv ($("$BACKEND_DIR/.venv/bin/python" -V 2>&1))"
fi

VENV_PY="$BACKEND_DIR/.venv/bin/python"

# 记录解释器来源，方便排查（写进 venv 目录下一个不参与运行的说明文件）
{
  echo "# 由 deploy/install.sh 生成，仅供排查用"
  echo "created_at=$(date '+%Y-%m-%d %H:%M:%S')"
  echo "python_bin=$PYTHON_BIN"
  echo "python_version=$("$VENV_PY" -V 2>&1)"
} > "$BACKEND_DIR/.venv/PYTHON_SOURCE" 2>/dev/null || true

# pip 本身也升级一下：老 pip 解析不了某些新版本的依赖约束。
"$VENV_PY" -m pip install --upgrade pip -q \
  || warn "升级 pip 失败（继续用现有版本）"

# --upgrade 保证重复执行时也能补齐新增依赖，而不是因为"已安装"而跳过。
"$VENV_PY" -m pip install -q --upgrade -r "$BACKEND_DIR/requirements.txt" \
  || fail "后端依赖安装失败。常见原因：
           1) 网络问题 —— 换 pip 源，见 docs/DEPLOY_UBUNTU.md 第 10.7 节
           2) 缺编译工具 —— sudo apt install -y build-essential libffi-dev libssl-dev libsqlite3-dev"
ok "后端依赖已安装"

# --------------------------------------------------------------------------- 前端

step "安装前端依赖"

cd "$FRONTEND_DIR"

if [ -f package-lock.json ]; then
  # npm ci 严格按 lock 文件装，版本可复现；失败时回退到 npm install
  npm ci --no-audit --no-fund 2>/dev/null || {
    warn "npm ci 失败（lock 文件与 package.json 可能不同步），改用 npm install"
    npm install --no-audit --no-fund
  }
else
  npm install --no-audit --no-fund
fi
ok "前端依赖已安装"

if [ "$NO_BUILD" -eq 0 ]; then
  step "构建前端（frontend/dist）"
  npm run build
  ok "前端已构建：frontend/dist"
else
  warn "跳过前端构建（--no-build）"
  [ -f "$FRONTEND_DIR/dist/index.html" ] || warn "frontend/dist 不存在，SERVE_FRONTEND=true 时会没有页面可托管"
fi

cd "$REPO_ROOT"

# --------------------------------------------------------------------------- .env

step "生成后端配置（backend/.env）"

ENV_FILE="$BACKEND_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  ok "backend/.env 已存在，保持不动（如需重建请先删除它）"
else
  JWT_SECRET="$("$VENV_PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"

  cat > "$ENV_FILE" <<EOF
# 由 deploy/install.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 各项含义见 backend/.env.example 与 docs/DEPLOY_UBUNTU.md。

# --------------------------------------------------------------------------- 前端托管
# true = 后端直接把前端页面也发出去，整个站点只需要一个端口（推荐，最省事）。
# 改成 false 的话要自己用 Nginx 托管 frontend/dist，见文档的「生产部署」一节。
SERVE_FRONTEND=true

# --------------------------------------------------------------------------- 鉴权
# 自动生成的随机密钥。**不要清空**：清空后每次重启都会让已登录用户掉线。
JWT_SECRET=$JWT_SECRET
JWT_EXPIRE_MINUTES=1440

# --------------------------------------------------------------------------- 管理员（root）
# ⚠️ 对外提供服务前务必改掉这个口令，改完要重启后端。
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
ADMIN_DISPLAY_NAME=管理员

# --------------------------------------------------------------------------- 对话模型（AI 部分，留空）
# 留空时问答会返回一条可读的错误提示，其余功能（图鉴、阵容、上传、检索）都正常。
# 申请到 key 后把下面三行填上并重启后端即可，不需要改代码。
#   DeepSeek:   LLM_BASE_URL=https://api.deepseek.com          LLM_MODEL=deepseek-chat
#   通义千问:   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1  LLM_MODEL=qwen-plus
#   月之暗面:   LLM_BASE_URL=https://api.moonshot.cn/v1        LLM_MODEL=moonshot-v1-8k
#   本地 Ollama: LLM_BASE_URL=http://localhost:11434/v1        LLM_MODEL=qwen2.5:7b
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.2
# DeepSeek 不支持 json_schema，必须保持 function_calling。
STRUCTURED_OUTPUT_METHOD=function_calling

# --------------------------------------------------------------------------- 嵌入（AI 部分）
# 当前用 hash 伪嵌入：**不需要任何外部服务**，上传/切分/入库/检索的链路全部真实，
# 只有"检索得准不准"是假的（/system/health 会标 degraded，前端顶部也会提示）。
#
# 配好 AI 之后改成 ollama（免费、本地）或 openai（云端）：
#   ollama serve && ollama pull qwen3-embedding:4b
#   EMBEDDING_PROVIDER=ollama
#   EMBEDDING_MODEL=qwen3-embedding:4b
#   EMBEDDING_DIM=2560
#
# 注意 EMBEDDING_DIM 必须与模型实际输出维度一致。默认保持 2560 是为了将来切到
# qwen3-embedding:4b 时**维度不变**，已有切片不用重新入库。
EMBEDDING_PROVIDER=hash
EMBEDDING_DIM=2560
OLLAMA_BASE_URL=http://localhost:11434
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=

# --------------------------------------------------------------------------- Redis（可选）
# 留空 = 进程内实现，单进程部署完全够用。
# **多进程/多实例部署必须配**，否则登录限流与令牌吊销不跨进程生效。
REDIS_URL=
REDIS_KEY_PREFIX=myrag

# --------------------------------------------------------------------------- 联网检索（可选）
# 留空 = 界面上的联网开关显示"未配置"并禁用。
TAVILY_API_KEY=
WEB_SEARCH_ENABLED=true

# --------------------------------------------------------------------------- 检索与上传
TOP_K=4
MAX_HISTORY_ROUNDS=4
UPLOAD_MAX_MB=50

# --------------------------------------------------------------------------- 多轮记忆
CHECKPOINT_ENABLED=true
CHECKPOINT_MAX_THREADS=500

# --------------------------------------------------------------------------- 可观测性
OBSERVABILITY_ENABLED=true
EOF

  chmod 600 "$ENV_FILE"
  ok "已生成 backend/.env（JWT_SECRET 随机生成，AI 相关项留空）"
fi

# --------------------------------------------------------------------------- 种子数据

if [ "$SKIP_SEED" -eq 0 ]; then
  step "导入领域种子数据（精灵 / 技能 / 阵容 / 属性克制）"
  cd "$BACKEND_DIR"
  # 脚本自己会建表，所以新库也能直接跑。重复执行是幂等的（先清表再写）。
  "$VENV_PY" -m app.scraper.seed_loader
  ok "种子数据已导入"
  cd "$REPO_ROOT"
else
  warn "跳过种子数据导入（--skip-seed）"
fi

# --------------------------------------------------------------------------- systemd

if [ "$WITH_SYSTEMD" -eq 1 ]; then
  step "安装 systemd 服务（lkwg-rag）"

  SERVICE_SRC="$SCRIPT_DIR/systemd/lkwg-rag.service"
  [ -f "$SERVICE_SRC" ] || fail "找不到服务模板：$SERVICE_SRC"

  # 模板里的占位符替换成真实路径与用户
  TMP_UNIT="$(mktemp)"
  sed -e "s|@APP_DIR@|$REPO_ROOT|g" \
      -e "s|@RUN_USER@|$RUN_USER|g" \
      -e "s|@APP_PORT@|$APP_PORT|g" \
      "$SERVICE_SRC" > "$TMP_UNIT"

  as_root install -m 0644 "$TMP_UNIT" /etc/systemd/system/lkwg-rag.service
  rm -f "$TMP_UNIT"

  as_root systemctl daemon-reload
  as_root systemctl enable lkwg-rag >/dev/null 2>&1 || true
  as_root systemctl restart lkwg-rag
  ok "服务已启动并设为开机自启（systemctl status lkwg-rag 查看状态）"
fi

# --------------------------------------------------------------------------- nginx

if [ "$WITH_NGINX" -eq 1 ]; then
  step "安装 Nginx 配置"

  if [ "$WITH_SYSTEMD" -eq 0 ]; then
    warn "没有 --with-systemd，Nginx 反代的后端可能没在跑。继续安装配置。"
  fi

  NGINX_SRC="$SCRIPT_DIR/nginx/lkwg-rag.conf"
  [ -f "$NGINX_SRC" ] || fail "找不到 Nginx 模板：$NGINX_SRC"

  apt_install nginx

  TMP_CONF="$(mktemp)"
  sed -e "s|@APP_DIR@|$REPO_ROOT|g" \
      -e "s|@APP_PORT@|$APP_PORT|g" \
      -e "s|@SERVER_NAME@|$SERVER_NAME|g" \
      "$NGINX_SRC" > "$TMP_CONF"

  as_root install -m 0644 "$TMP_CONF" /etc/nginx/sites-available/lkwg-rag
  rm -f "$TMP_CONF"

  # Debian/Ubuntu 的默认站点会占用 80 端口，先摘掉
  as_root rm -f /etc/nginx/sites-enabled/default
  as_root ln -sf /etc/nginx/sites-available/lkwg-rag /etc/nginx/sites-enabled/lkwg-rag

  # 配置语法有问题时不要 reload，否则会把线上打挂
  if as_root nginx -t 2>/dev/null; then
    as_root systemctl reload nginx
    ok "Nginx 已配置并重载（80 端口 → 前端 dist + /api 反代）"
  else
    as_root nginx -t || true
    fail "Nginx 配置校验失败，未重载。请检查 /etc/nginx/sites-available/lkwg-rag"
  fi

  if [ "$WITH_SYSTEMD" -eq 1 ]; then
    # Nginx 托管 dist 时后端就不该再托管一遍，避免两套入口行为不一致
    warn "注意：用 Nginx 托管前端时，建议把 backend/.env 里的 SERVE_FRONTEND 改成 false，"
    warn "      让后端只做 API。两者都开也能用（Nginx 会优先命中静态文件），但排查问题时会混淆。"
  fi
fi

# --------------------------------------------------------------------------- 完成

step "安装完成"

if [ "$WITH_SYSTEMD" -eq 1 ]; then
  cat <<EOF

  服务已启动。访问地址：

      http://<这台机器的IP>:$APP_PORT/

  常用命令：

      sudo systemctl status lkwg-rag      # 看状态
      sudo journalctl -u lkwg-rag -f      # 实时看日志
      sudo systemctl restart lkwg-rag     # 改完 .env 后重启

  初始管理员账号：admin / admin  ⚠️ 请尽快修改（backend/.env 里 ADMIN_PASSWORD）

EOF
else
  cat <<EOF

  依赖都装好了，用下面任一方式启动：

    # 1) 前台运行（推荐先这样，能看到完整日志）
    cd "$BACKEND_DIR"
    .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT

    # 2) 装成系统服务（开机自启）
    bash deploy/install.sh --with-systemd

  启动后浏览器打开：

      http://<这台机器的IP>:$APP_PORT/

  初始管理员账号：admin / admin  ⚠️ 请尽快修改（backend/.env 里 ADMIN_PASSWORD）

EOF
fi

cat <<EOF
  接下来还要做的事（AI 部分，见 docs/DEPLOY_UBUNTU.md 第 6 节）：

    1. 填 LLM_API_KEY（问答要用），改完重启后端
    2. 想提高检索质量：装 Ollama 并把 EMBEDDING_PROVIDER 改成 ollama
    3. 想用联网检索：填 TAVILY_API_KEY
    4. 改掉默认管理员口令

  启动之后跑一遍自检，确认每个环节都通：

      bash deploy/check.sh

EOF

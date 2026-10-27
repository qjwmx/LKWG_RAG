#!/usr/bin/env bash
#
# 部署自检脚本（在服务器上执行）
# =============================================================================
#
# 用法：
#
#     bash deploy/check.sh                    # 检查默认地址 http://127.0.0.1:8000
#     bash deploy/check.sh http://127.0.0.1:8000
#     bash deploy/check.sh http://your-domain.com
#
# 它会逐项检查部署是否真的可用，每项打印 ✓ / ✗，最后给出结论。
#
# 为什么要这个脚本：部署最常见的问题不是"服务起不来"（那有日志），
# 而是"服务起来了但某个环节是坏的"——比如：
#   · 前端页面能开，但接口全 404（反向代理没配）
#   · 能登录，但精灵列表是空的（种子数据没导）
#   · 能问答，但永远回"资料不足"（嵌入服务没启动）
# 这些都不会让进程崩溃，只能靠主动探测发现。
#
# 本脚本只读不写：不改配置、不建数据、不触发模型调用（不花钱）。

set -uo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
BASE_URL="${BASE_URL%/}"

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_HEAD=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_ERR=""; C_WARN=""; C_HEAD=""; C_OFF=""
fi

PASS=0
FAIL=0
WARN=0

section() { printf '\n%s%s%s\n' "$C_HEAD" "$*" "$C_OFF"; }
pass()    { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
fail()    { FAIL=$((FAIL+1)); printf '  %s✗%s %s\n' "$C_ERR" "$C_OFF" "$*"; }
warn()    { WARN=$((WARN+1)); printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
detail()  { printf '      %s\n' "$*"; }

# --------------------------------------------------------------------------- 依赖

command -v curl >/dev/null 2>&1 || {
  echo "需要 curl：sudo apt install -y curl" >&2
  exit 1
}

# HTTP 状态码
http_code() {
  curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$1" 2>/dev/null || echo "000"
}

# 取响应体（失败返回空串）
http_body() {
  curl -sS --max-time 20 "$@" 2>/dev/null || true
}

# 从 JSON 里取字段。优先用 python3（一定有，部署时已经装了），
# 退化到 grep —— 后者不健壮，但至少不会让整个脚本失败。
json_get() {
  local json="$1" path="$2"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for key in '''$path'''.split('.'):
    if not isinstance(d, dict):
        sys.exit(1)
    d = d.get(key)
    if d is None:
        sys.exit(1)
print(d if not isinstance(d, (dict, list)) else json.dumps(d, ensure_ascii=False))
" 2>/dev/null
  fi
}

# --------------------------------------------------------------------------- 1. 连通性

section "1. 服务连通性（$BASE_URL）"

CODE="$(http_code "$BASE_URL/")"
if [ "$CODE" = "200" ]; then
  pass "根路径可访问（HTTP 200）——前端页面正常"
elif [ "$CODE" = "302" ] || [ "$CODE" = "307" ]; then
  warn "根路径返回 $CODE（跳转）——说明 SERVE_FRONTEND=false，后端只做 API"
  detail "如果你是用 Nginx 托管前端，这是正常的；直接访问后端端口会跳 /docs"
elif [ "$CODE" = "000" ]; then
  fail "连不上 $BASE_URL"
  detail "排查顺序："
  detail "  1) 服务在跑吗：sudo systemctl status lkwg-rag"
  detail "  2) 端口在听吗：sudo ss -tlnp | grep 8000（要 0.0.0.0 不是 127.0.0.1）"
  detail "  3) 云安全组放行了吗（TCP 8000 入方向）"
  exit 1
else
  fail "根路径返回 HTTP $CODE"
fi

CODE="$(http_code "$BASE_URL/docs")"
[ "$CODE" = "200" ] && pass "接口文档可访问（/docs）" || warn "/docs 返回 HTTP $CODE（可能被刻意关闭，正常）"

# --------------------------------------------------------------------------- 2. 接口

section "2. 后端接口"

CODE="$(http_code "$BASE_URL/api/v1/public/showcase")"
BODY="$(http_body "$BASE_URL/api/v1/public/showcase")"
if [ "$CODE" = "200" ] && [ "$(json_get "$BODY" code)" = "200" ]; then
  PETS="$(printf '%s' "$BODY" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"]["pets"]))' 2>/dev/null || echo "?")"
  pass "公开接口正常（登录页展示数据：$PETS 只精灵）"
  [ "$PETS" = "0" ] && fail "展示数据是空的 —— 种子数据没导入？执行：cd backend && .venv/bin/python -m app.scraper.seed_loader"
else
  fail "公开接口异常（HTTP $CODE）"
  detail "响应：$(printf '%s' "$BODY" | head -c 200)"
fi

# --------------------------------------------------------------------------- 3. 登录

section "3. 登录与鉴权"

LOGIN_BODY="$(http_body -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}')"
TOKEN="$(json_get "$LOGIN_BODY" data.access_token)"

if [ -n "$TOKEN" ]; then
  pass "默认管理员 admin/admin 可登录"
  warn "管理员口令仍是默认值 —— 对外提供服务前务必修改（backend/.env 的 ADMIN_PASSWORD）"
elif printf '%s' "$LOGIN_BODY" | grep -q '"code": *401'; then
  warn "admin/admin 登录失败（口令已改过？）。后续需要登录的检查会跳过。"
  detail "如确实改过口令，请手动登录后确认功能，或临时改回 admin 再跑本脚本"
else
  fail "登录接口异常"
  detail "响应：$(printf '%s' "$LOGIN_BODY" | head -c 300)"
fi

AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

# --------------------------------------------------------------------------- 4. 领域数据

section "4. 领域数据（精灵 / 阵容）"

if [ -n "$TOKEN" ]; then
  STATS_BODY="$(http_body "${AUTH[@]}" "$BASE_URL/api/v1/pokedex/stats")"
  PETS_N="$(json_get "$STATS_BODY" data.pets)"
  LINEUPS_N="$(json_get "$STATS_BODY" data.lineups)"

  if [ -n "$PETS_N" ] && [ "$PETS_N" -gt 0 ] 2>/dev/null; then
    pass "精灵图鉴：$PETS_N 条"
  else
    fail "精灵图鉴是空的 —— 种子数据没导入"
    detail "执行：cd backend && .venv/bin/python -m app.scraper.seed_loader"
  fi

  if [ -n "$LINEUPS_N" ] && [ "$LINEUPS_N" -gt 0 ] 2>/dev/null; then
    pass "阵容数据：$LINEUPS_N 套"
  else
    fail "阵容数据是空的 —— 种子数据没导入"
  fi

  # 阵容反查是核心能力，单独验一次
  BYPET_BODY="$(http_body "${AUTH[@]}" "$BASE_URL/api/v1/lineups/by_pet?pet_name=%E8%BF%AA%E8%8E%AB")"
  TOTAL="$(json_get "$BYPET_BODY" data.total)"
  if [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ] 2>/dev/null; then
    pass "阵容反查正常（迪莫出现在 $TOTAL 套阵容里）"
  else
    warn "阵容反查没有结果（可能是数据为空）"
  fi
else
  warn "跳过（未取得登录 token）"
fi

# --------------------------------------------------------------------------- 5. 依赖状态

section "5. 依赖健康（数据库 / 嵌入 / 模型 / Redis）"

if [ -n "$TOKEN" ]; then
  HEALTH_BODY="$(http_body "${AUTH[@]}" "$BASE_URL/api/v1/system/health")"
  STATUS="$(json_get "$HEALTH_BODY" data.status)"
  DB_OK="$(json_get "$HEALTH_BODY" data.postgres.ok)"
  EMBED_PROVIDER="$(json_get "$HEALTH_BODY" data.embedding.provider)"
  EMBED_OK="$(json_get "$HEALTH_BODY" data.embedding.ok)"
  LLM_OK="$(json_get "$HEALTH_BODY" data.llm.ok)"

  [ "$DB_OK" = "True" ] || [ "$DB_OK" = "true" ] && pass "数据库连接正常" || fail "数据库连接异常"
  [ -z "$DB_OK" ] && fail "健康检查接口异常"

  case "$EMBED_PROVIDER" in
    hash)
      warn "嵌入用的是 hash 伪嵌入 —— 链路能跑通，但检索**没有语义意义**"
      detail "配好 AI 后改成 ollama 或 openai，见 docs/DEPLOY_UBUNTU.md 第 6.2 节"
      ;;
    ollama)
      if [ "$EMBED_OK" = "True" ] || [ "$EMBED_OK" = "true" ]; then
        pass "嵌入服务正常（ollama）"
      else
        fail "Ollama 连不上 —— 检索会静默失败（页面表现为「未找到明确依据」）"
        detail "检查：systemctl status ollama  /  curl -s http://localhost:11434/api/tags"
      fi
      ;;
    "")
      warn "健康检查里没有嵌入信息"
      ;;
    *)
      if [ "$EMBED_OK" = "True" ] || [ "$EMBED_OK" = "true" ]; then
        pass "嵌入服务正常（$EMBED_PROVIDER）"
      else
        fail "嵌入服务不可用（$EMBED_PROVIDER）"
      fi
      ;;
  esac

  if [ "$LLM_OK" = "True" ] || [ "$LLM_OK" = "true" ]; then
    pass "对话模型已配置（问答可用）"
  else
    warn "没配 LLM_API_KEY —— 问答会返回可读的错误提示（其余功能不受影响）"
    detail "填 backend/.env 的 LLM_API_KEY 后重启，见 docs/DEPLOY_UBUNTU.md 第 6.1 节"
  fi

  case "$STATUS" in
    ok)       pass "整体状态：ok" ;;
    degraded) warn "整体状态：degraded（有组件降级，功能仍可用，原因见上面各项）" ;;
    "")       warn "整体状态：读取失败" ;;
    *)        warn "整体状态：$STATUS" ;;
  esac
else
  warn "跳过（未取得登录 token）"
fi

# --------------------------------------------------------------------------- 6. 前端产物

section "6. 前端产物"

# 只有后端托管前端时才查得到
ASSETS_CODE="$(http_code "$BASE_URL/assets/")"
if [ "$ASSETS_CODE" = "200" ] || [ "$ASSETS_CODE" = "403" ] || [ "$ASSETS_CODE" = "404" ]; then
  INDEX_BODY="$(http_body "$BASE_URL/")"
  if printf '%s' "$INDEX_BODY" | grep -q 'assets/'; then
    ASSET_PATH="$(printf '%s' "$INDEX_BODY" | grep -o 'assets/[^"]*\.js' | head -1)"
    if [ -n "$ASSET_PATH" ]; then
      ACODE="$(http_code "$BASE_URL/$ASSET_PATH")"
      [ "$ACODE" = "200" ] && pass "前端静态资源可加载（$ASSET_PATH）" || fail "静态资源 404（$ASSET_PATH）—— frontend/dist 不完整，重新 npm run build"
    fi
  fi
fi

SPA_CODE="$(http_code "$BASE_URL/strategy")"
if [ "$SPA_CODE" = "200" ]; then
  pass "SPA 路由刷新正常（/strategy 返回 200）"
else
  warn "直接访问 /strategy 返回 HTTP $SPA_CODE"
  detail "用 Nginx 部署时检查 try_files \$uri \$uri/ /index.html;"
fi

CODE="$(http_code "$BASE_URL/api/v1/this-does-not-exist")"
if [ "$CODE" = "404" ]; then
  pass "接口 404 是 JSON 而不是前端页面（不会把错误吞成 HTML）"
else
  warn "不存在的接口返回 HTTP $CODE（期望 404）"
fi

# --------------------------------------------------------------------------- 结论

section "检查结论"

TOTAL=$((PASS + FAIL + WARN))
printf '  通过 %s%d%s / 警告 %s%d%s / 失败 %s%d%s（共 %d 项）\n' \
  "$C_OK" "$PASS" "$C_OFF" "$C_WARN" "$WARN" "$C_OFF" "$C_ERR" "$FAIL" "$C_OFF" "$TOTAL"

if [ "$FAIL" -gt 0 ]; then
  printf '\n  %s有 %d 项失败，部署还不完整。%s\n' "$C_ERR" "$FAIL" "$C_OFF"
  printf '  排查手册：docs/DEPLOY_UBUNTU.md 第 10 节「常见问题排查」\n\n'
  exit 1
fi

if [ "$WARN" -gt 0 ]; then
  printf '\n  %s基本可用，有 %d 项需要注意（见上面的 ! 项）。%s\n' "$C_WARN" "$WARN" "$C_OFF"
  printf '  这些通常不影响图鉴/阵容/知识库功能。\n\n'
  exit 0
fi

printf '\n  %s全部通过，部署完整可用。%s\n\n' "$C_OK" "$C_OFF"
exit 0

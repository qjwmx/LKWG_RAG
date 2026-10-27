# 后端镜像：Python + FastAPI，**同时托管前端构建产物**。
#
# 为什么前端也在同一个镜像里构建（多阶段）：
# 部署只需要"一个容器、一个端口"，不必再维护 Nginx 容器与跨容器代理配置。
# 前端产物只是几百 KB 静态文件，放进同一个镜像的代价远小于多一个服务。
#
# 构建（在仓库根目录执行）：
#     docker build -f deploy/docker/Dockerfile -t lkwg-rag:latest .
#
# 注意：**构建上下文必须是仓库根目录**，因为要同时拷 backend/ 与 frontend/。

# =============================================================================
# 阶段 1：构建前端
# =============================================================================
FROM node:22-bookworm-slim AS frontend

WORKDIR /build/frontend

# 先只拷依赖清单：这样改业务代码不会让依赖层缓存失效，重建快很多。
COPY frontend/package.json frontend/package-lock.json* frontend/.npmrc* ./

# npm ci 严格按 lock 文件装。lock 不存在时回退到 install。
RUN if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    else \
      npm install --no-audit --no-fund; \
    fi

COPY frontend/ ./
RUN npm run build

# =============================================================================
# 阶段 2：运行时
# =============================================================================
FROM python:3.12-slim-bookworm

# 时区。日志时间、问答记录时间都按它走；不设的话容器里是 UTC，
# 排查问题时对不上用户说的"几点几分"。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata 让 TZ 生效；curl 用于健康检查；libgomp1 是 numpy 的运行时依赖。
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      tzdata curl libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# 非 root 用户运行。UID 固定，便于宿主机挂载卷时对齐权限。
RUN useradd --create-home --uid 10001 --shell /bin/bash appuser

WORKDIR /app/backend

# 依赖层单独一层：只改代码时不必重装依赖。
COPY backend/requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# 应用代码与数据
COPY backend/ ./

# 启动前初始化脚本（建表 + 首次启动导入种子数据）
COPY deploy/docker/entrypoint.py /app/deploy/docker/entrypoint.py

# 前端产物放在 /app/frontend/dist，与 SERVE_FRONTEND 的默认解析路径一致
COPY --from=frontend /build/frontend/dist /app/frontend/dist

# 运行时目录：SQLite 库、上传原文、checkpoint 都落在这里。
# 用卷挂载出去，容器重建才不会丢数据。
RUN mkdir -p /app/backend/data/raw_documents \
 && chown -R appuser:appuser /app

USER appuser

ENV SERVE_FRONTEND=true \
    DATABASE_URL=sqlite:////app/backend/data/rag.db \
    FRONTEND_DIST=/app/frontend/dist \
    PYTHONPATH=/app/backend

EXPOSE 8000

# 健康检查打的是**无需登录**的路径。
# /api/v1/system/health 需要登录，用它做健康检查会永远失败。
# 这里用根路径：SERVE_FRONTEND=true 时返回 index.html（200），
# 关闭前端托管时 302 到 /docs（curl -f 也算成功）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ >/dev/null || exit 1

# --proxy-headers + --forwarded-allow-ips：让后端认识反向代理传来的真实 IP，
# 否则登录限流会把所有用户当成同一个来源（都来自网关 IP）。
#
# 用 sh -c 先跑初始化脚本再 exec uvicorn：
#  - 初始化脚本幂等（建表 + 只在领域表为空时导种子数据），重启不会重复导入；
#  - ``exec`` 让 uvicorn 接管 PID 1，能正确收到 docker stop 的 SIGTERM
#    并优雅关闭（不 exec 的话信号到不了 uvicorn，只能等 10 秒后被 SIGKILL）。
CMD ["sh", "-c", "python /app/deploy/docker/entrypoint.py && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]

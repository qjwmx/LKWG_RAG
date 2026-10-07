"""全局配置。

所有配置项都有可用默认值，因此 ``backend/.env`` 可以不存在也能跑起来——
这是"空环境三条命令起服务"的前提。

关于路径：``BASE_DIR`` 取自本文件位置，所以 SQLite 的默认值是**绝对路径**。
用相对路径时，从不同目录启动会在别处凭空建出一个空库，且不报错——
排查起来非常费劲。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DOCUMENT_DIR = DATA_DIR / "raw_documents"
SAMPLE_DOC_DIR = BASE_DIR / "sample_docs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ 数据库
    database_url: str = f"sqlite:///{(DATA_DIR / 'rag.db').as_posix()}"

    # ------------------------------------------------------------------ 鉴权
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    admin_username: str = "admin"
    admin_password: str = "admin"
    admin_display_name: str = "管理员"
    allow_admin_promotion: bool = True

    # ------------------------------------------------------------------ 模型
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    embedding_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dim: int = 2560
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    # 结构化输出的实现方式。**必须可配，不能写死**：
    # ``json_schema`` 只有较新的 OpenAI 端点支持，DeepSeek 会直接返回
    # ``400 This response_format type is unavailable now``；
    # 而 ``function_calling`` 在两边都能用，所以默认用它。
    # 这个值写错时的症状非常隐蔽——结构化节点全部静默退化到兜底分支，
    # 图照常跑完，只是 Planner 永远只拆出一个单元、Researcher 的结论
    # 变成原文摘要，看起来像"模型能力不行"而不是配置错了。
    structured_output_method: str = "function_calling"

    # ------------------------------------------------------------------ Redis
    # 缓存与共享状态。**默认关闭（空串）**，理由与 SQLite 默认值同理：
    # 本项目的前提是"空环境三条命令起服务"，Redis 是可选增强而不是依赖。
    # 留空时所有用到 Redis 的功能都会退化成进程内实现（见 app/cache/），
    # 功能不缺失，只是不跨进程共享。
    redis_url: str = ""
    # 键前缀。同一台 Redis 上跑多个环境（dev/prod）时用来隔离，
    # 否则 `flushall` 或键名撞车会互相污染。
    redis_key_prefix: str = "myrag"
    # 单次 Redis 操作的超时（秒）。**必须短**：Redis 抖动时不能把
    # 请求拖慢——缓存的意义是加速，不是把故障传导给业务。
    redis_socket_timeout: float = 1.0

    # ------------------------------------------------------------------ 登录保护
    # 登录失败次数上限与统计窗口。超过后**锁定一段时间**再放行。
    # 这是对"统一失败文案"的补充：那个文案防的是**用户名枚举**，
    # 防不住**口令爆破**（攻击者不在乎提示，只在乎能不能试出来）。
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    # 触发限流后锁定的秒数
    login_lockout_seconds: int = 300

    # ------------------------------------------------------------------ 缓存 TTL
    # 查询向量的缓存时长。查询向量是**纯函数结果**（同样的文本+同样的模型
    # 必然得到同样的向量），所以 TTL 可以很长；设短了只是白白浪费命中率。
    embed_cache_ttl: int = 604800

    # 向量检索结果的缓存时长（秒）。
    #
    # **必须短，与查询向量相反**：检索结果依赖**库里的内容**，而内容会变
    # （上传 / 删除文档）。正确性主要由"语料版本号"保证
    # （见 cache/store.py 的 bump_corpus_version），TTL 只是**兜底**——
    # 万一版本号机制没覆盖到某条写入路径，最多脏这么久。
    search_cache_ttl: int = 120

    # ------------------------------------------------------------------ 可观测性
    # 请求轨迹 + LLM token 消耗采集。**只有管理员能看到数据**（见 routes/admin.py）。
    #
    # 关掉时中间件直接透传，零开销——排查性能问题时不希望埋点本身成为变量。
    observability_enabled: bool = True
    # 轨迹保留天数。超过就裁掉（约每 50 次写入触发一次清理）。
    observability_retention_days: int = 7
    # 硬上限：无论保留多久，轨迹表最多这么多行。
    # 为什么需要它：单日流量很大时，7 天的数据也可能撑爆单机 SQLite。
    observability_max_rows: int = 20000
    # 写入队列容量。满了就**丢弃**并计数——埋点绝不能反压业务。
    observability_queue_size: int = 2000

    # ------------------------------------------------------------------ 联网检索（Tavily）
    # 给 Researcher 用的**可选**联网能力。默认关闭有三个理由：
    #
    # 1. **按次计费**：Tavily 免费额度有限，跑一次攻略要花 1~4 次检索。
    #    开着不管的话，一次误点就可能烧掉一整天额度。
    # 2. **内容不可信**：抓回来的网页是**外部输入**，可能包含针对模型的
    #    指令（提示注入）。本地库的内容是我们自己导入的，可信度完全不同。
    # 3. **可复现性**：同一问题的答案会随网上内容变化，排查问题更难。
    #
    # 所以设计成**用户在界面上显式打开**（"允许联网"开关），而不是自动触发。
    web_search_enabled: bool = True
    tavily_api_key: str = ""
    tavily_max_results: int = 5
    # 单次联网检索超时（秒）。**必须短**：联网是"锦上添花"，
    # 一个卡住的搜索不该把整轮攻略拖到几十秒。
    tavily_timeout: float = 8.0
    # 每条网页摘要截断长度。网页正文动辄几万字，全塞进上下文会
    # 挤掉本地库资料（而本地库才是可信来源）。
    web_snippet_max_chars: int = 1200

    # ------------------------------------------------------------------ 检查点（多轮记忆）
    # 攻略模式的 LangGraph Checkpointer。**对话模式不用它**——
    # 对话模式已经从 qa_logs 重建历史（见 rag_graph.retrieve），
    # 再叠一层 checkpoint 会让消息**二次增长**（实测第 8 轮 56 条 vs 预期 14 条）。
    checkpoint_enabled: bool = True
    # checkpoint 库的路径。留空 = 与业务库同目录下的 checkpoints.db。
    #
    # **为什么单独一个文件而不是塞进业务库**：checkpoint 表只增不减、
    # 需要独立裁剪（prune_threads），和业务数据混在一张库里会让
    # 备份/清理/体积评估全部纠缠在一起。测试也要能把它指到临时目录，
    # 否则测试线程会跨用例残留。
    checkpoint_db: str = ""
    # 保留的会话线程数上限。checkpoint 表只增不减，
    # 不设上限的话长期运行会慢慢撑大 SQLite。
    checkpoint_max_threads: int = 500

    # ------------------------------------------------------------------ 上传与检索
    upload_max_mb: int = 50
    top_k: int = 4
    max_history_rounds: int = 4

    chunk_size: int = 800
    chunk_overlap: int = 120
    # 短于这个长度就不切分——切了反而把完整语义打碎
    min_chunk_length: int = 800
    max_reference_documents: int = 4
    # 生成温度。RAG 场景要的是忠实于材料，不是发挥，所以压低。
    llm_temperature: float = 0.2
    # LangSmith 追踪（可选）。留空则不启用。
    langchain_tracing: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "rag-kb"

    # ------------------------------------------------------------------ 派生
    @property
    def effective_jwt_secret(self) -> str:
        """空配置时生成随机密钥。

        不在这里直接改 ``self.jwt_secret``：那样每次访问都会重新生成，
        同一个进程里前后签发的 token 互不认账。调用方（security.py）在模块加载时
        取一次并缓存。
        """
        return self.jwt_secret or secrets.token_urlsafe(48)

    @property
    def jwt_secret_auto_generated(self) -> bool:
        return not self.jwt_secret


settings = Settings()

DOCUMENT_CATEGORIES = [
    "精灵图鉴",
    "技能图鉴",
    "属性克制",
    "阵容攻略",
    "版本公告",
    "未分类",
]
CATEGORY_FILTER_OPTIONS = ["全部"] + DOCUMENT_CATEGORIES

# 支持的扩展名。**不含点**，统一小写比较。
# .md / .markdown 走纯文本解码路径，语法刻意保留不清洗——标题层级天然是
# 切分边界，能让 chunk 语义更完整。
SUPPORTED_FILE_TYPES = ["txt", "md", "markdown", "pdf", "docx", "xlsx", "csv"]

DEFAULT_VERSION = "v1.0"

SCOPE_PUBLIC = "public"
SCOPE_PRIVATE = "private"

ROLE_ADMIN = "admin"
ROLE_USER = "user"

USER_STATUS_PENDING = "pending"
USER_STATUS_APPROVED = "approved"
USER_STATUS_REJECTED = "rejected"

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
PASSWORD_MIN_LENGTH = 6

# 题型 key 与关键词表的键一一对应（见 services/rag_graph._TYPE_KEYWORDS）。
# 键名沿用旧的企业 RAG 命名，是为了不改动 qa_logs 里已有的历史数据；
# 显示名换成洛克王国的语义。
QUESTION_TYPE_LABELS = {
    "policy_qa": "机制问答",
    "material_list": "阵容推荐",
    "process_guide": "针对克制",
    "notice_summary": "版本总结",
}

NO_EVIDENCE_MESSAGE = (
    "### 最终回答\n"
    "资料不足，当前知识库中没有足够材料回答这个问题。\n\n"
    "### 操作步骤/材料清单\n"
    "可以试试：换一个精灵名、在攻略模式里指定核心精灵，"
    "或先到知识库补充相关攻略文档。\n\n"
    "### 风险提示\n"
    "在没有明确资料依据前，不要直接照搬阵容，建议结合当前赛季环境判断。"
)

# 示例文档换成洛克王国的机制说明，而不是企业制度。
# 这些是**机制类**内容（不随版本变），阵容数据走结构化表导入，不在这里。
SAMPLE_DOCS = [
    {"file_name": "属性克制机制.md", "category": "属性克制", "title": "属性克制机制", "version": "v1.0"},
    {"file_name": "性格与个体值.md", "category": "属性克制", "title": "性格与个体值", "version": "v1.0"},
    {"file_name": "PvP基础规则.md", "category": "版本公告", "title": "PvP 基础规则", "version": "v1.0"},
]


def ensure_runtime_dirs() -> None:
    """建运行时目录。

    **不允许在这里碰数据库**：本模块被所有模块 import，一旦在 import 期连库，
    数据库抖动会让整个应用连页面都渲染不出来，且报错位置极具误导性。
    """
    RAW_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

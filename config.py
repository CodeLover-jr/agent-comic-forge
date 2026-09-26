# -*- coding: utf-8 -*-
"""全局配置：阿里云百炼（Model Studio / DashScope），所有密钥从环境变量读取。"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 阿里云百炼
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
# 原生接口基址（异步视频任务）
DASHSCOPE_API_BASE = os.getenv("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
# OpenAI 兼容基址（Function Calling 编排/编剧、文生图）
DASHSCOPE_COMPAT_BASE = os.getenv(
    "DASHSCOPE_COMPAT_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)

# 模型（ID 以百炼「模型广场」实际展示为准）
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.8-flash")
# 编排/编剧文本模型：必须支持 Function Calling。
# 若模型广场展示为 qwen3.8-omni-flash，请在 .env 中改成该 ID。
QWEN_VL_MODEL = os.getenv("QWEN_VL_MODEL", "qwen3.8-omni-flash")
# 画师自检：视觉理解（图生文）模型，必须能接收图片输入。
# qwen3.8-omni-flash 为全模态（图/视频/音频输入→文本），免费额度大；也可用 qwen-vl-max。
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "qwen-image-3.0")
# 文生图（DashScope multimodal-generation 端点）；追求极致可选 qwen-image-3.0-pro。
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "wan3.0-video-prime")
# 图生视频（异步任务）。万相 wan3.0-video-prime：2~30s（有免费额度）；
# MiniMax/MiniMax-H3：4~15s（需先在模型广场点「立即开通」）。
# 首帧不能用 Base64，需先上传临时存储，见 tools/i2v.py。
TTS_MODEL = os.getenv("TTS_MODEL", "qwen-audio-3.1-tts-flash")
# 非实时配音；该模型与 CosyVoice 系统音色通用。

# 全模态/视觉模型 OpenAI 兼容基址：qwen3.8-omni-flash 同样走「业务空间专属 host」。
# 获取方式：模型广场打开该模型 → API代码示例（OpenAI兼容）里复制 base_url。
OMNI_COMPAT_BASE = os.getenv(
    "DASHSCOPE_OMNI_BASE",
    "https://llm-bk62d2444ic30nz3.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)

# TTS 专用基址：qwen-audio-3.1 必须走「业务空间专属 host」，通用域名会报 411。
# 获取方式：百炼模型广场打开 qwen-audio-3.1-tts-flash → API代码示例 里复制 host。
TTS_BASE = os.getenv(
    "DASHSCOPE_TTS_BASE",
    "https://llm-bk62d2444ic30nz3.cn-beijing.maas.aliyuncs.com/api/v1",
)

# TTS 音色（qwen-audio-3.1 专属 v3.1 音色，完整列表见 MODELS.md / 官方音色列表）
TTS_VOICE_MALE = os.getenv("TTS_VOICE_MALE", "longanyang_v3.1")    # 龙安洋：阳光大男孩
TTS_VOICE_FEMALE = os.getenv("TTS_VOICE_FEMALE", "longanhuan_v3.1")  # 龙安欢：欢脱元气女
TTS_VOICE_NARRATOR = os.getenv("TTS_VOICE_NARRATOR", "anmingyuan_v3.1")  # 安明远：旁白

# ---------------------------------------------------------------------------
# 服务与产物
# ---------------------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
BGM_PATH = os.getenv("BGM_PATH", "")  # 可选背景音乐，留空不混 BGM

# ---------------------------------------------------------------------------
# 数据库 / 认证 / 队列
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# SQLite（本地文件零依赖）；生产可换 MySQL，仅改此连接串
DB_URL = os.getenv("DB_URL", f"sqlite:///{(DATA_DIR / 'animai.db').as_posix()}")

# JWT：生产环境务必在 .env 中设置随机 JWT_SECRET
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

# 同时执行的生成任务数（消费者协程数量），超出的任务在队列中排队
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))
# 单个任务内，同时生成的视频片段数（信号量限流，免费额度下建议 2~3）
MAX_CONCURRENT_CLIPS = int(os.getenv("MAX_CONCURRENT_CLIPS", "3"))
# 单个任务内，同时生成的关键帧数（含自检重绘；免费额度下建议 2~3）
MAX_CONCURRENT_KEYFRAMES = int(os.getenv("MAX_CONCURRENT_KEYFRAMES", "2"))
# 单个任务内，剪辑阶段（归一化 / TTS 配音）的并发数
MAX_CONCURRENT_EDITOR = int(os.getenv("MAX_CONCURRENT_EDITOR", "3"))

# 任务参数
MAX_SHOTS = int(os.getenv("MAX_SHOTS", "12"))
TASK_POLL_TIMEOUT = int(os.getenv("TASK_POLL_TIMEOUT", "900"))
TASK_POLL_INTERVAL = int(os.getenv("TASK_POLL_INTERVAL", "5"))

# 画幅 -> (宽, 高)
ASPECT_SIZES = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
}

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

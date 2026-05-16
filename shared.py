"""共享配置、日志、Web 控制台缓冲和 HTTP 会话工具。

这里集中放置各模块都会使用的全局对象，避免在业务代码中重复初始化。
"""

# -*- coding: utf-8 -*-
"""
================================================================================
SHUXUE BOT SYSTEM  - SXBS
================================================================================
"""

import asyncio
import websockets
import json
import os
import re
import aiosqlite
import aiohttp
import logging
import base64
import hashlib
import io
import traceback
import random
import time
import sys
import shutil
import threading
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import sqlite3
import uuid
from html import unescape

def _make_stdio_unicode_safe():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

_make_stdio_unicode_safe()

# Flask：Web 控制台（软降级，未安装时控制台功能关闭）
try:
    from flask import (
        Flask as _Flask,
        request as _flask_request,
        jsonify as _jsonify,
        session as _flask_session,
        redirect as _flask_redirect,
    )
    import threading as _threading
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False
    print("[WARN] flask 未安装，Web控制台已禁用。运行: pip install flask")


# ChromaDB：全局向量记忆大脑
try:
    import chromadb
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    print("[WARN] chromadb 未安装，向量记忆功能已降级。运行: pip install chromadb")

# [FIX v10.4] PIL 软降级：未安装时 GIF 帧提取功能静默降级，不影响其他功能
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    print("[WARN] Pillow 未安装，GIF 表情包帧提取已降级。运行: pip install Pillow")

# ==========================================
# 0. 控制台彩色 Formatter
# ==========================================
class _ConsoleFormatter(logging.Formatter):
    _COLORS = {
        "DEBUG":   "\033[90m",
        "INFO":    "\033[36m",
        "SUCCESS": "\033[32m",
        "WARN":    "\033[33m",
        "ERROR":   "\033[31m",
        "AI":      "\033[35m",
        "SOC":     "\033[34m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        level = getattr(record, "custom_level", "INFO")
        color = self._COLORS.get(level, "")
        ts = datetime.now().strftime("%H:%M:%S")
        return f"{color}[{ts}] {record.getMessage()}{self._RESET}"


# ==========================================
# 1. 全局配置中心
# ==========================================
def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


CONFIG_PATH = os.environ.get(
    "SHUXUE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
)

def _load_config_file() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"[WARN] 未找到配置文件: {CONFIG_PATH}，将只使用环境变量和安全默认值。")
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 配置文件读取失败: {CONFIG_PATH} | {e}")
        return {}


def save_config_patch(patch: dict):
    if not isinstance(patch, dict):
        raise ValueError("config patch must be a dict")
    merged = _deep_merge(_CONFIG, patch)
    folder = os.path.dirname(os.path.abspath(CONFIG_PATH))
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _CONFIG.clear()
    _CONFIG.update(merged)


def _env_or_config(env_name: str, config: dict, path: str, default=None):
    if env_name in os.environ:
        return os.environ[env_name]
    cur = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    if cur in ("", None):
        return default
    return cur


def _to_bool(value, default: bool = False) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开启")
    return default


_CONFIG = _load_config_file()


class GlobalConfig:
    VERSION = str(_env_or_config("SHUXUE_VERSION", _CONFIG, "bot.version", "10.5.2_BugFix"))
    # 运行数据默认放在当前项目目录；需要沿用旧目录时可设置 SHUXUE_BASE_DIR。
    BASE_DIR = str(_env_or_config(
        "SHUXUE_BASE_DIR", _CONFIG, "paths.base_dir", os.path.dirname(os.path.abspath(__file__))
    ))

    DB_PATH   = str(_env_or_config("SHUXUE_DB_PATH", _CONFIG, "paths.db_path", os.path.join(BASE_DIR, "shuxue_v10_core.db")))
    LOG_DIR   = str(_env_or_config("SHUXUE_LOG_DIR", _CONFIG, "paths.log_dir", os.path.join(BASE_DIR, "logs")))
    EMOTE_DIR = str(_env_or_config("SHUXUE_EMOTE_DIR", _CONFIG, "paths.emote_dir", os.path.join(BASE_DIR, "emotes")))
    CACHE_DIR = str(_env_or_config("SHUXUE_CACHE_DIR", _CONFIG, "paths.cache_dir", os.path.join(BASE_DIR, "cache")))

    # [NEW v10.2] 全局向量记忆大脑配置
    CHROMA_PATH     = str(_env_or_config("SHUXUE_CHROMA_PATH", _CONFIG, "paths.chroma_path", os.path.join(BASE_DIR, "chroma_global_brain")))
    EMBEDDING_MODEL = str(_env_or_config("EMBEDDING_MODEL", _CONFIG, "models.embedding", "text-embedding-v3"))
    EMBEDDING_DIM   = int(_env_or_config("EMBEDDING_DIM", _CONFIG, "memory.embedding_dim", 1024))
    MAX_HISTORY_BEFORE_CONSOLIDATE = int(_env_or_config("MAX_HISTORY_BEFORE_CONSOLIDATE", _CONFIG, "memory.max_history_before_consolidate", 120))

    DEEPSEEK_KEY         = str(_env_or_config("DEEPSEEK_KEY", _CONFIG, "api.deepseek_key", ""))
    DEEPSEEK_BASE_URL    = str(_env_or_config("DEEPSEEK_BASE_URL", _CONFIG, "api.deepseek_base_url", "https://api.deepseek.com"))
    DEEPSEEK_MODEL       = str(_env_or_config("DEEPSEEK_MODEL", _CONFIG, "models.deepseek", "deepseek-v4-pro"))
    DEEPSEEK_TEMPERATURE = float(_env_or_config("DEEPSEEK_TEMPERATURE", _CONFIG, "models.deepseek_temperature", 1.15))
    DASHSCOPE_KEY        = str(_env_or_config("DASHSCOPE_KEY", _CONFIG, "api.dashscope_key", ""))
    VISION_BASE_URL      = str(_env_or_config("VISION_BASE_URL", _CONFIG, "api.vision_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    DASHSCOPE_ENABLE_THINKING = _to_bool(_env_or_config("DASHSCOPE_ENABLE_THINKING", _CONFIG, "api.dashscope_enable_thinking", False))

    # Web 控制台
    CONSOLE_PORT = int(_env_or_config("CONSOLE_PORT", _CONFIG, "console.port", 7860))
    CONSOLE_USERNAME = str(_env_or_config("CONSOLE_USERNAME", _CONFIG, "console.username", ""))
    CONSOLE_PASSWORD = str(_env_or_config("CONSOLE_PASSWORD", _CONFIG, "console.password", ""))
    CONSOLE_SESSION_SECRET = str(_env_or_config("CONSOLE_SESSION_SECRET", _CONFIG, "console.session_secret", ""))

    VISION_MODEL       = str(_env_or_config("VISION_MODEL", _CONFIG, "models.vision", "qwen3.6-plus"))
    VISION_MAX_SIDE    = int(_env_or_config("VISION_MAX_SIDE", _CONFIG, "models.vision_max_side", 1024))
    VISION_JPEG_QUALITY = int(_env_or_config("VISION_JPEG_QUALITY", _CONFIG, "models.vision_jpeg_quality", 80))
    ASR_MODEL          = str(_env_or_config("ASR_MODEL", _CONFIG, "models.asr", "fun-asr"))
    ASR_BASE_URL       = str(_env_or_config("ASR_BASE_URL", _CONFIG, "api.asr_base_url", "https://dashscope.aliyuncs.com/api/v1"))
    JUDGE_MODEL        = str(_env_or_config("JUDGE_MODEL", _CONFIG, "models.judge", "qwen3.5-flash"))
    AD_REVIEW_MODEL    = str(_env_or_config("AD_REVIEW_MODEL", _CONFIG, "models.ad_review", "qwen3.6-plus"))
    STICKER_NAME_MODEL = str(_env_or_config("STICKER_NAME_MODEL", _CONFIG, "models.sticker_name", "qwen3.6-plus"))
    GIF_MAX_SIZE_KB    = int(_env_or_config("GIF_MAX_SIZE_KB", _CONFIG, "assets.gif_max_size_kb", 2048))
    GIF_MAX_FRAMES     = int(_env_or_config("GIF_MAX_FRAMES", _CONFIG, "assets.gif_max_frames", 8))
    ADMIN_UID          = str(_env_or_config("ADMIN_UID", _CONFIG, "bot.admin_uid", ""))

    NAPCAT_WS = str(_env_or_config("NAPCAT_WS", _CONFIG, "napcat.ws_url", ""))

    MAX_HISTORY_PER_USER = int(_env_or_config("MAX_HISTORY_PER_USER", _CONFIG, "memory.max_history_per_user", 50))
    REQUEST_TIMEOUT      = int(_env_or_config("REQUEST_TIMEOUT", _CONFIG, "api.request_timeout", 45))
    MAX_TOKENS           = int(_env_or_config("MAX_TOKENS", _CONFIG, "models.max_tokens", 1024))
    MAX_INPUT_CHARS      = int(_env_or_config("MAX_INPUT_CHARS", _CONFIG, "security.max_input_chars", 400))
    FILE_SCAN_ENABLED    = _to_bool(_env_or_config("FILE_SCAN_ENABLED", _CONFIG, "security.file_scan_enabled", True), True)
    FILE_SCAN_MAX_MB     = int(_env_or_config("FILE_SCAN_MAX_MB", _CONFIG, "security.file_scan_max_mb", 50))
    FILE_SCAN_TIMEOUT    = int(_env_or_config("FILE_SCAN_TIMEOUT", _CONFIG, "security.file_scan_timeout", 45))
    FILE_SCAN_APK_MAX_MB = int(_env_or_config("FILE_SCAN_APK_MAX_MB", _CONFIG, "security.file_scan_apk_max_mb", 150))
    FILE_SCAN_CLAMAV_ENABLED = _to_bool(
        _env_or_config("FILE_SCAN_CLAMAV_ENABLED", _CONFIG, "security.file_scan_clamav_enabled", True),
        True,
    )
    FILE_SCAN_CLAMSCAN_PATH = str(_env_or_config("FILE_SCAN_CLAMSCAN_PATH", _CONFIG, "security.file_scan_clamscan_path", ""))
    APK_REVIEW_MODEL = str(_env_or_config("APK_REVIEW_MODEL", _CONFIG, "models.apk_review", "qwen3.6-plus"))

    @classmethod
    def ensure_environment(cls):
        for folder in [cls.LOG_DIR, cls.EMOTE_DIR, cls.CACHE_DIR]:
            if not os.path.exists(folder):
                os.makedirs(folder)


# ==========================================
# 2. 增强型审计系统
# ==========================================
class AuditSystem:
    def __init__(self):
        GlobalConfig.ensure_environment()

        self.logger = logging.getLogger("ShuxueV10")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        if not self.logger.handlers:
            log_name = f"shuxue_{datetime.now().strftime('%Y-%m-%d')}.log"
            fh = logging.FileHandler(
                os.path.join(GlobalConfig.LOG_DIR, log_name), encoding="utf-8"
            )
            fh.setFormatter(
                logging.Formatter("[%(asctime)s] [%(levelname)s] - %(message)s")
            )
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(_ConsoleFormatter())
            self.logger.addHandler(fh)
            self.logger.addHandler(sh)

    def log(self, level: str, tag: str, message: str):
        py_level = (
            logging.ERROR   if level == "ERROR" else
            logging.WARNING if level == "WARN"  else
            logging.INFO
        )
        self.logger.log(
            py_level,
            f"[{tag}] {message}",
            extra={"custom_level": level}
        )

    def ai_raw(self, direction: str, content: str):
        prefix = ">>> [AI REQUEST]" if direction == "in" else "<<< [AI RESPONSE]"
        self.log("AI", "RAW", f"{prefix}\n{content}")

    def log_ai_final(self, content: str):
        self.log("AI", "FINAL", f"发送给AI的最终内容：\n{content}")


audit = AuditSystem()

# 挂载控制台日志 handler
# ── Web 控制台日志缓冲 ──
from collections import deque as _deque
_console_log_buffer: _deque = _deque(maxlen=1200)
_console_log_counter: int = 0
_console_log_lock = threading.RLock()

def _append_console_log(level: str, tag: str, message: str):
    global _console_log_counter
    body = str(message if message is not None else "")
    with _console_log_lock:
        _console_log_counter += 1
        _console_log_buffer.append({
            "id":    _console_log_counter,
            "time":  datetime.now().strftime("%H:%M:%S"),
            "level": str(level or "INFO"),
            "tag":   str(tag or "SYS"),
            "msg":   body,
        })

class _ConsoleLogHandler(logging.Handler):
    """把 audit 日志同步写入控制台日志缓冲，供前端轮询。"""
    def emit(self, record: logging.LogRecord):
        try:
            level = getattr(record, "custom_level", record.levelname)
            msg   = record.getMessage()
            import re as _re
            m = _re.match(r'^\[([A-Z_]+)\]\s*(.*)', msg, _re.DOTALL)
            tag, body = (m.group(1), m.group(2)) if m else ("SYS", msg)
            _append_console_log(level, tag, body)
        except Exception:
            pass

# 挂载控制台日志 handler（类定义在上方，实例化在下方，顺序正确）
class _ConsoleStreamProxy:
    """Mirror stdout/stderr into the Web console while preserving normal output."""
    def __init__(self, stream, level: str, tag: str):
        self._stream = stream
        self._level = level
        self._tag = tag
        self._pending = ""

    def write(self, text):
        try:
            written = self._stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "utf-8"
            safe_text = str(text).encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
            written = self._stream.write(safe_text)
        if text:
            self._pending += str(text).replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                if line.strip():
                    _append_console_log(self._level, self._tag, line)
        return written

    def flush(self):
        if self._pending.strip():
            _append_console_log(self._level, self._tag, self._pending)
            self._pending = ""
        return self._stream.flush()

    def isatty(self):
        return self._stream.isatty()

    def fileno(self):
        return self._stream.fileno()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", None)

    def __getattr__(self, name):
        return getattr(self._stream, name)

_console_log_handler = _ConsoleLogHandler()
_console_log_handler.setLevel(logging.DEBUG)
audit.logger.addHandler(_console_log_handler)

if not isinstance(sys.stdout, _ConsoleStreamProxy):
    sys.stdout = _ConsoleStreamProxy(sys.stdout, "INFO", "STDOUT")
if not isinstance(sys.stderr, _ConsoleStreamProxy):
    sys.stderr = _ConsoleStreamProxy(sys.stderr, "ERROR", "STDERR")

# 运行中的 asyncio 事件循环（由 ShuxueBotHost.listen() 注入，供 ConsoleServer 跨线程调用）
_bot_loop = None



# ==========================================
# 3. 全局异步 HTTP Session
# ==========================================
_http_session: aiohttp.ClientSession | None = None
_http_session_lock: asyncio.Lock | None = None

async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session, _http_session_lock
    if _http_session and not _http_session.closed:   # 无锁快路径
        return _http_session
    if _http_session_lock is None:
        _http_session_lock = asyncio.Lock()
    async with _http_session_lock:
        if _http_session and not _http_session.closed:   # 锁内二次检查
            return _http_session
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(connector=connector)
        audit.log("INFO", "HTTP", "全局 aiohttp.ClientSession 已创建。")
    return _http_session


async def close_http_session():
    """关闭全局 aiohttp Session。"""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        audit.log("INFO", "HTTP", "全局 aiohttp.ClientSession 已关闭。")
    _http_session = None


# ==========================================
# 4. 终极数据库记忆引擎
# ==========================================




def set_bot_loop(loop):
    """记录 Bot 主 asyncio 循环，供 Web 控制台跨线程调度。"""
    global _bot_loop
    _bot_loop = loop


def get_bot_loop():
    """获取 Bot 主 asyncio 循环；未启动时返回 None。"""
    return _bot_loop

"""项目入口。"""

import asyncio
import os
import sys
import traceback

from .host import ShuxueBotHost
from .shared import GlobalConfig, _CHROMA_AVAILABLE, _FLASK_AVAILABLE, audit, close_http_session


def main():
    """启动 Bot 主进程，并保留原脚本的启动输出。"""
    if os.name == "nt":
        os.system("cls")
    else:
        os.system("clear")

    print("\n" + "=" * 70)
    print(f"  SHUXUE BOT v{GlobalConfig.VERSION} - 已就绪")
    print(f"  运行环境: Python {sys.version.split()[0]} | PID: {os.getpid()}")
    print("  项目结构: 已从单脚本拆分为模块化目录")
    print(f"  全局向量记忆大脑: {'已激活' if _CHROMA_AVAILABLE else '未激活 (pip install chromadb)'}")
    print(f"  ADMIN UID: {GlobalConfig.ADMIN_UID}")
    print("  AI 后端: DEEPSEEK (DeepSeek HTTP)")
    print(f"  DEEPSEEK_KEY 来源: {'环境变量' if os.environ.get('DEEPSEEK_KEY') else '配置文件/未配置'}")
    print(
        f"  Web 控制台: "
        f"{'已启用 -> http://localhost:' + str(GlobalConfig.CONSOLE_PORT) + '/console/' if _FLASK_AVAILABLE else '未启用 (pip install flask)'}"
    )
    print(f"  DASHSCOPE_KEY 来源: {'环境变量' if os.environ.get('DASHSCOPE_KEY') else '配置文件/未配置'}")
    if not GlobalConfig.ADMIN_UID:
        print("  [WARN] ADMIN_UID 未配置，管理员命令将不可用。")
    if not GlobalConfig.NAPCAT_WS:
        print("  [WARN] NAPCAT_WS 未配置，无法连接 NapCat。")
    if not GlobalConfig.DEEPSEEK_KEY:
        print("  [WARN] DEEPSEEK_KEY 未配置，聊天模型不可用。")
    if not GlobalConfig.DASHSCOPE_KEY:
        print("  [WARN] DASHSCOPE_KEY 未配置，图片理解/判断/向量记忆不可用。")
    print("=" * 70 + "\n")

    async def _run_bot():
        try:
            await ShuxueBotHost().listen()
        finally:
            await close_http_session()

    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        audit.log("WARN", "SYS", "收到手动终止信号，系统已停止。")
    except Exception:
        audit.log("ERROR", "FATAL", f"系统由于致命异常崩溃:\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()

"""Administrator command handling."""

from .shared import *
from .memory import db
from .file_scanner import LocalFileScanner


class CommandHandler:
    COMMAND_ALIASES = {
        "help", "commands", "status", "clear", "fav",
        "enable_social", "social_on", "disable_social", "social_off",
        "social_status", "social",
        "ad_kick_on", "anti_ad_on", "ad_kick_off", "anti_ad_off",
        "ad_kick_status", "anti_ad",
        "file_hash_clear", "clear_file_hashes", "clear_file_hash", "file_scan_clear",
    }

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def command_help_text() -> str:
        return (
            "【淑雪可用指令】\n"
            "群聊里所有指令必须使用：/shuxuebot <指令>\n"
            "/shuxuebot help 或 commands - 查看所有可用指令\n"
            "/shuxuebot status - 查看运行状态\n"
            "/shuxuebot clear - 清空当前私聊/群聊上下文\n"
            "/shuxuebot fav <用户ID> <数值> - 调整指定用户好感度\n"
            "/shuxuebot social_on [群号] - 开启主动群聊\n"
            "/shuxuebot social_off [群号] - 关闭主动群聊\n"
            "/shuxuebot social [群号] - 查看主动群聊状态\n"
            "/shuxuebot ad_kick_on [群号] - 开启广告自动撤回+踢人\n"
            "/shuxuebot ad_kick_off [群号] - 关闭广告自动踢人\n"
            "/shuxuebot ad_kick_status [群号] 或 anti_ad [群号] - 查看广告自动踢人状态\n"
            "/shuxuebot file_hash_clear - 删除所有已检查文件哈希记录\n"
            "私聊可省略 /shuxuebot 前缀；跨群设置时在功能开关指令后直接加群号。"
        )

    def system_status_text(self) -> str:
        uptime = str(datetime.now() - self.bot.start_time).split(".")[0]
        qq_connected = self.bot.is_qq_connected() if hasattr(self.bot, "is_qq_connected") else bool(self.bot.ws)
        return (
            "【淑雪系统概况】\n"
            f"版本: {GlobalConfig.VERSION}\n"
            f"在线时长: {uptime}\n"
            f"QQ连接: {'已连接' if qq_connected else '未连接'}\n"
            f"DeepSeek模型: {GlobalConfig.DEEPSEEK_MODEL}\n"
            f"视觉模型: {GlobalConfig.VISION_MODEL}\n"
            f"判断模型: {GlobalConfig.JUDGE_MODEL}\n"
            f"广告复审模型: {GlobalConfig.AD_REVIEW_MODEL}\n"
            f"ASR模型: {GlobalConfig.ASR_MODEL}\n"
            f"数据库: {GlobalConfig.DB_PATH}\n"
            f"WebUI: http://localhost:{GlobalConfig.CONSOLE_PORT}/console/"
        )

    async def execute(self, uid, gid, raw_msg: str, group_name: str | None = None) -> bool:
        parsed = self._parse_command(raw_msg, in_group=gid is not None)
        if parsed is None:
            return False
        cmd, args = parsed
        reply_target = gid or uid
        is_group = gid is not None

        if str(uid) != GlobalConfig.ADMIN_UID:
            await self.bot.send_direct(reply_target, "【系统】权限不足。", is_group)
            return True

        if cmd in ("help", "commands"):
            await self.bot.send_direct(reply_target, self.command_help_text(), is_group)
            return True

        if cmd == "status":
            await self.bot.send_direct(reply_target, self.system_status_text(), is_group)
            return True

        if cmd == "fav":
            if len(args) < 2:
                await self._send_usage(reply_target, is_group, "fav <用户ID> <数字>")
                return True
            try:
                target_id, delta = args[0], int(args[1])
            except ValueError:
                await self._send_usage(reply_target, is_group, "fav <用户ID> <数字>")
                return True
            await db.sync_user(target_id, "用户", favor_delta=delta)
            await self.bot.send_direct(reply_target, f"【系统】已调整 ID:{target_id} 的好感度。", is_group)
            return True

        if cmd == "clear":
            await db.reset_memory(gid or uid, target_type="group" if gid else "private")
            await self.bot.send_direct(reply_target, "【系统】已清空当前上下文记忆。", is_group)
            return True

        if cmd in ("file_hash_clear", "clear_file_hashes", "clear_file_hash", "file_scan_clear"):
            count = await asyncio.to_thread(LocalFileScanner.clear_scan_cache)
            await self.bot.send_direct(reply_target, f"【系统】已删除所有已检查文件哈希记录，共 {count} 条。", is_group)
            return True

        if cmd in ("enable_social", "social_on"):
            target_gid, target_name = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "social_on <群号>")
                return True
            await db.enable_group_social(target_gid, group_name=target_name)
            await self.bot.send_direct(reply_target, f"【系统】群 {target_gid} 主动群聊已开启。", is_group)
            return True

        if cmd in ("disable_social", "social_off"):
            target_gid, target_name = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "social_off <群号>")
                return True
            await db.disable_group_social(target_gid, group_name=target_name)
            await self.bot.send_direct(reply_target, f"【系统】群 {target_gid} 主动群聊已关闭。", is_group)
            return True

        if cmd in ("social_status", "social"):
            target_gid, _ = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "social [群号]")
                return True
            status = await db.get_group_social_status(target_gid)
            last_ts = status["last_bubble"]
            last_desc = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "从未主动冒泡"
            mode = "开启" if status["enabled"] else "关闭"
            await self.bot.send_direct(reply_target, f"【系统】群 {target_gid} 主动群聊：{mode}\n最近主动尝试：{last_desc}", is_group)
            return True

        if cmd in ("ad_kick_on", "anti_ad_on"):
            target_gid, target_name = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "ad_kick_on <群号>")
                return True
            await db.set_group_ad_kick(target_gid, True, group_name=target_name)
            await self.bot.send_direct(
                reply_target,
                f"【系统】群 {target_gid} 广告自动撤回+踢人已开启。只有快速判断和高级复审都确认是广告时才会执行。",
                is_group,
            )
            return True

        if cmd in ("ad_kick_off", "anti_ad_off"):
            target_gid, target_name = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "ad_kick_off <群号>")
                return True
            await db.set_group_ad_kick(target_gid, False, group_name=target_name)
            await self.bot.send_direct(reply_target, f"【系统】群 {target_gid} 广告自动踢人已关闭。", is_group)
            return True

        if cmd in ("ad_kick_status", "anti_ad"):
            target_gid, _ = await self._resolve_group_target(args, gid, group_name)
            if not target_gid:
                await self._send_usage(reply_target, is_group, "ad_kick_status [群号]")
                return True
            status = await db.get_group_ad_kick_status(target_gid)
            mode = "开启" if status["enabled"] else "关闭"
            await self.bot.send_direct(reply_target, f"【系统】群 {target_gid} 广告自动撤回+踢人：{mode}", is_group)
            return True

        await self.bot.send_direct(reply_target, "【系统】指令错误。发送 /shuxuebot help 查看用法。", is_group)
        return True

    @staticmethod
    def _strip_leading_mentions(raw_msg: str) -> str:
        text = str(raw_msg or "").strip()
        text = re.sub(r"^(?:\[CQ:at,qq=\d+\]\s*)+", "", text).strip()
        return text

    def _parse_command(self, raw_msg: str, in_group: bool) -> tuple[str, list[str]] | None:
        text = self._strip_leading_mentions(raw_msg)
        if not text.startswith("/"):
            return None

        parts = text[1:].split()
        if not parts:
            return ("", [])

        head = parts[0].lower()
        if in_group:
            if head != "shuxuebot":
                return ("", []) if head in self.COMMAND_ALIASES else None
            if len(parts) < 2:
                return ("", [])
            return parts[1].lower(), parts[2:]

        if head == "shuxuebot":
            if len(parts) < 2:
                return ("", [])
            return parts[1].lower(), parts[2:]
        return head, parts[1:]

    async def _resolve_group_target(
        self, args: list[str], current_gid, current_group_name: str | None = None
    ) -> tuple[str | None, str | None]:
        if args and not re.fullmatch(r"\d{5,20}", str(args[0])):
            return None, None
        if args:
            target_gid = str(args[0])
            target_name = current_group_name if str(current_gid or "") == target_gid else await db.get_group_name(target_gid)
            return target_gid, target_name
        if current_gid:
            return str(current_gid), current_group_name
        return None, None

    async def _send_usage(self, target, is_group: bool, usage: str):
        prefix = "/shuxuebot " if is_group else "/"
        await self.bot.send_direct(target, f"【系统】指令错误。用法：{prefix}{usage}", is_group)

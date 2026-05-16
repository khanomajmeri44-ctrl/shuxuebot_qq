"""Bot 主机入口。

负责连接 NapCat WebSocket、接收 QQ 事件、调度指令、审核和回复流程。
"""

from . import shared
from .shared import *
from .shared import _get_http_session
from .memory import db
from .brain import BrainInterpreter
from .file_scanner import LocalFileScanner
from .scheduler import SocialScheduler
from .commands import CommandHandler
from .console import ConsoleServer
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

class ShuxueBotHost:
    def __init__(self):
        self.ws_uri      = GlobalConfig.NAPCAT_WS
        self.ws          = None
        self.self_id     = None
        self.start_time  = datetime.now()
        self.cmd_handler = CommandHandler(self)
        self._startup_notice_sent = False

        self._pending: dict = {}
        self.DEBOUNCE_SECONDS = 4
        self._console: ConsoleServer | None = None
        self._image_events: dict[str, deque] = {}
        self._real_image_events_by_user: dict[str, deque] = {}
        self._image_desc_cache: dict[str, tuple[float, str | None]] = {}
        self._image_desc_inflight: dict[str, asyncio.Task] = {}
        self._image_ocr_cache: dict[str, tuple[float, str | None]] = {}
        self._image_ocr_inflight: dict[str, asyncio.Task] = {}
        self._video_frame_cache: dict[str, tuple[float, list[str]]] = {}
        self._video_frame_inflight: dict[str, asyncio.Task] = {}
        self._sticker_collect_cache: dict[str, float] = {}
        self._api_pending: dict[str, asyncio.Future] = {}
        self._api_seq = 0
        self.IMAGE_SPAM_WINDOW = 8
        self.IMAGE_SPAM_LIMIT = 6
        self.REAL_IMAGE_USER_WINDOW = 60
        self.REAL_IMAGE_USER_LIMIT = 6
        self.IMAGE_DESC_CACHE_TTL = 86400
        self.VIDEO_FRAME_CACHE_TTL = 3600
        self.STICKER_COLLECT_CACHE_TTL = 86400

    def is_qq_connected(self) -> bool:
        ws = self.ws
        if not ws:
            return False
        closed = getattr(ws, "closed", None)
        if closed is not None:
            return not bool(closed)
        close_code = getattr(ws, "close_code", None)
        if close_code is not None:
            return False
        state = getattr(ws, "state", None)
        if state is not None:
            state_name = getattr(state, "name", str(state)).upper()
            if "CLOSED" in state_name or "CLOSING" in state_name:
                return False
            if "OPEN" in state_name or str(state) == "1":
                return True
        return True

    def build_startup_overview(self) -> str:
        return (
            f"{self.cmd_handler.system_status_text()}\n\n"
            f"{self.cmd_handler.command_help_text()}"
        )

    async def send_startup_overview(self):
        if self._startup_notice_sent or not GlobalConfig.ADMIN_UID:
            return
        self._startup_notice_sent = True
        try:
            await self.send_direct(GlobalConfig.ADMIN_UID, self.build_startup_overview(), False)
            audit.log("SUCCESS", "SYS", "已向管理员发送启动系统概况。")
        except Exception as e:
            audit.log("WARN", "SYS", f"启动系统概况发送失败: {e}")

    def _image_cache_key(self, url: str) -> str:
        try:
            clean = unescape(str(url or "")).strip()
            if clean and not re.match(r"https?://", clean, re.I):
                return f"file:{clean.lower()}"
            split = urlsplit(clean)
            if split.scheme and split.netloc:
                volatile = {
                    "rkey", "sign", "token", "ts", "t", "term", "b", "ek", "kp",
                    "vuin", "uin", "appid", "spec", "is_origin", "fileid"
                }
                kept = [(k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k.lower() not in volatile]
                query = urlencode(kept, doseq=True)
                return urlunsplit((split.scheme.lower(), split.netloc.lower(), split.path, query, ""))
            return re.sub(r"([?&](?:rkey|sign|token|ts|t)=)[^&]+", r"\1_", clean)
        except Exception:
            return str(url or "")

    @staticmethod
    def _parse_cq_attrs(attrs_str: str) -> dict:
        attrs = {}
        for match in re.finditer(r"([a-zA-Z_][\w-]*)=([^,\]]*)", str(attrs_str or "")):
            attrs[match.group(1)] = unescape(match.group(2).strip())
        return attrs

    def _extract_images_from_normalized(self, text: str) -> list[tuple[str, bool, str, dict]]:
        images = []
        for cq_type, attrs_str in re.findall(r"\[CQ:(image|mface),([^\]]*)\]", str(text or "")):
            attrs = self._parse_cq_attrs(attrs_str)
            url = attrs.get("url") or attrs.get("file") or ""
            if not url:
                continue
            file_id = attrs.get("file") or attrs.get("file_id") or ""
            sticker_meta = {
                "emoji_id": attrs.get("emoji_id"),
                "emoji_package_id": attrs.get("emoji_package_id"),
                "key": attrs.get("key"),
                "summary": attrs.get("summary"),
                "source_type": cq_type,
            }
            has_mface_meta = bool(sticker_meta.get("emoji_id") and sticker_meta.get("emoji_package_id"))
            is_sticker = cq_type == "mface" or attrs.get("sub_type") == "1" or has_mface_meta
            images.append((url, is_sticker, file_id, sticker_meta if is_sticker else {}))
        return images

    def _extract_videos_from_normalized(self, text: str) -> list[tuple[str, str]]:
        videos = []
        for attrs_str in re.findall(r"\[CQ:video(?:,([^\]]*))?\]", str(text or "")):
            attrs = self._parse_cq_attrs(attrs_str or "")
            url = attrs.get("url") or attrs.get("file") or ""
            file_id = attrs.get("file") or attrs.get("file_id") or ""
            if url:
                videos.append((url, file_id))
        return videos

    def _extract_files_from_normalized(self, text: str) -> list[dict]:
        files = []
        for attrs_str in re.findall(r"\[CQ:file(?:,([^\]]*))?\]", str(text or "")):
            attrs = self._parse_cq_attrs(attrs_str or "")
            name = attrs.get("name") or attrs.get("file") or attrs.get("file_id") or "群文件"
            file_id = attrs.get("id") or attrs.get("file_id") or attrs.get("file") or ""
            url = attrs.get("url") or attrs.get("path") or ""
            size = 0
            try:
                size = int(attrs.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            files.append({"name": name, "file_id": file_id, "url": url, "size": size})
        return files

    def _limit_user_input_text(self, text: str, label: str = "消息") -> str:
        text = str(text or "")
        limit = max(1, int(getattr(GlobalConfig, "MAX_INPUT_CHARS", 400)))
        if len(text) <= limit:
            return text
        audit.log("WARN", "SECURITY", f"{label}超过{limit}字，已截断: original={len(text)}")
        note = "\n[SYSTEM: input truncated.]"
        if limit > len(note):
            return text[:limit - len(note)].rstrip() + note
        return text[:limit]

    def _register_image_events(self, tid: str, image_count: int) -> int:
        now = time.time()
        events = self._image_events.setdefault(str(tid), deque())
        while events and now - events[0] > self.IMAGE_SPAM_WINDOW:
            events.popleft()
        for _ in range(image_count):
            events.append(now)
        return len(events)

    def _allow_real_image_for_user(self, uid: str) -> bool:
        now = time.time()
        events = self._real_image_events_by_user.setdefault(str(uid), deque())
        while events and now - events[0] > self.REAL_IMAGE_USER_WINDOW:
            events.popleft()
        if len(events) >= self.REAL_IMAGE_USER_LIMIT:
            return False
        events.append(now)
        return True

    def _cached_image_desc(self, key: str) -> str | None:
        cached = self._image_desc_cache.get(key)
        if not cached:
            return None
        ts, desc = cached
        if time.time() - ts > self.IMAGE_DESC_CACHE_TTL:
            self._image_desc_cache.pop(key, None)
            return None
        return desc

    def _remember_image_desc(self, key: str, desc: str | None):
        self._image_desc_cache[key] = (time.time(), desc)

    def _cached_image_ocr(self, key: str) -> str | None:
        cached = self._image_ocr_cache.get(key)
        if not cached:
            return None
        ts, text = cached
        if time.time() - ts > self.IMAGE_DESC_CACHE_TTL:
            self._image_ocr_cache.pop(key, None)
            return None
        return text

    def _remember_image_ocr(self, key: str, text: str | None):
        self._image_ocr_cache[key] = (time.time(), text)

    async def _ocr_image_once(self, cache_key: str, url: str, label: str) -> str | None:
        cached = self._cached_image_ocr(cache_key)
        if cached is not None:
            audit.log("INFO", "OCR", f"复用图片 OCR 缓存: {label} {url[:60]}...")
            return cached
        task = self._image_ocr_inflight.get(cache_key)
        if task and not task.done():
            audit.log("INFO", "OCR", f"等待同图 OCR 结果: {label} {url[:60]}...")
            return await task
        task = asyncio.create_task(BrainInterpreter.ocr_image(url))
        self._image_ocr_inflight[cache_key] = task
        try:
            text = await task
            self._remember_image_ocr(cache_key, text)
            return text
        finally:
            if self._image_ocr_inflight.get(cache_key) is task:
                self._image_ocr_inflight.pop(cache_key, None)

    def _cached_video_frames(self, key: str) -> list[str] | None:
        cached = self._video_frame_cache.get(key)
        if not cached:
            return None
        ts, frames = cached
        if time.time() - ts > self.VIDEO_FRAME_CACHE_TTL:
            self._video_frame_cache.pop(key, None)
            return None
        return frames

    def _remember_video_frames(self, key: str, frames: list[str]):
        self._video_frame_cache[key] = (time.time(), list(frames or []))

    async def _extract_video_frames_once(self, cache_key: str, url: str, label: str) -> list[str]:
        cached = self._cached_video_frames(cache_key)
        if cached is not None:
            audit.log("INFO", "VIDEO", f"复用视频抽帧缓存: {label} frames={len(cached)}")
            return cached
        task = self._video_frame_inflight.get(cache_key)
        if task and not task.done():
            audit.log("INFO", "VIDEO", f"等待同视频抽帧结果: {label}")
            return await task
        frame_count = random.randint(2, 3)
        task = asyncio.create_task(BrainInterpreter.extract_video_frames(url, count=frame_count))
        self._video_frame_inflight[cache_key] = task
        try:
            frames = await task
            self._remember_video_frames(cache_key, frames)
            return frames
        finally:
            if self._video_frame_inflight.get(cache_key) is task:
                self._video_frame_inflight.pop(cache_key, None)

    async def _describe_image_once(self, cache_key: str, url: str, label: str) -> str | None:
        desc = self._cached_image_desc(cache_key)
        if desc is not None:
            audit.log("INFO", "VISION", f"复用图片描述缓存: {label} {url[:60]}...")
            return desc

        task = self._image_desc_inflight.get(cache_key)
        if task and not task.done():
            audit.log("INFO", "VISION", f"等待同图识别结果: {label} {url[:60]}...")
            return await task

        audit.log("INFO", "VISION", f"检测到{label}，正在描述: {url[:60]}...")
        task = asyncio.create_task(BrainInterpreter.describe_image(url))
        self._image_desc_inflight[cache_key] = task
        try:
            desc = await task
            self._remember_image_desc(cache_key, desc)
            return desc
        finally:
            if self._image_desc_inflight.get(cache_key) is task:
                self._image_desc_inflight.pop(cache_key, None)

    async def _transcribe_audio(self, url: str) -> str | None:
        audit.log("INFO", "ASR", f"检测到语音，正在调用 fun-asr 转写: {url[:60]}...")
        return await BrainInterpreter.transcribe_audio(url)

    def _should_collect_sticker(self, key: str) -> bool:
        now = time.time()
        last = self._sticker_collect_cache.get(key)
        if last and now - last < self.STICKER_COLLECT_CACHE_TTL:
            return False
        self._sticker_collect_cache[key] = now
        return True

    async def _collect_non_ad_stickers(self, images: list, allow: bool):
        if not allow:
            sticker_count = sum(1 for img in images or [] if img.get("is_sticker"))
            if sticker_count:
                audit.log("INFO", "EMOTE", f"表情包仍在广告嫌疑链路中，暂不入库: {sticker_count} 个")
            return
        for img in images or []:
            if not img.get("is_sticker"):
                continue
            cache_key = img.get("cache_key") or self._image_cache_key(img.get("url", ""))
            if not self._should_collect_sticker(cache_key):
                continue
            desc = img.get("ocr_text") or img.get("label") or "群聊表情包"
            asyncio.create_task(
                BrainInterpreter.collect_sticker(
                    img.get("url", ""),
                    desc,
                    metadata=img.get("sticker_meta") or None,
                )
            )

    @staticmethod
    def _cq_escape(value) -> str:
        text = str(value if value is not None else "")
        return (
            text.replace("&", "&amp;")
                .replace("[", "&#91;")
                .replace("]", "&#93;")
                .replace(",", "&#44;")
        )

    @classmethod
    def _normalize_message(cls, message) -> str:
        if message is None:
            return ""
        if isinstance(message, str):
            return message.strip()
        if isinstance(message, list):
            parts = []
            for seg in message:
                if isinstance(seg, str):
                    parts.append(seg)
                    continue
                if not isinstance(seg, dict):
                    parts.append(str(seg))
                    continue
                seg_type = str(seg.get("type") or "").strip()
                seg_data = seg.get("data") or {}
                if not isinstance(seg_data, dict):
                    seg_data = {}
                if seg_type == "text":
                    parts.append(str(seg_data.get("text", "")))
                elif seg_type == "at":
                    qq = cls._cq_escape(seg_data.get("qq", ""))
                    parts.append(f"[CQ:at,qq={qq}]")
                elif seg_type == "image":
                    attrs = []
                    file_id = seg_data.get("file") or seg_data.get("file_id") or ""
                    url = seg_data.get("url") or file_id or ""
                    if file_id:
                        attrs.append(f"file={cls._cq_escape(file_id)}")
                    if url:
                        attrs.append(f"url={cls._cq_escape(url)}")
                    if "sub_type" in seg_data:
                        attrs.append(f"sub_type={cls._cq_escape(seg_data.get('sub_type'))}")
                    elif str(seg_data.get("type", "")).lower() in ("flash", "sticker"):
                        attrs.append("sub_type=1")
                    parts.append(f"[CQ:image,{','.join(attrs)}]" if attrs else "[CQ:image]")
                elif seg_type == "mface":
                    attrs = []
                    for key in ("emoji_id", "emoji_package_id", "key", "summary", "url", "file"):
                        if seg_data.get(key):
                            attrs.append(f"{key}={cls._cq_escape(seg_data.get(key))}")
                    parts.append(f"[CQ:mface,{','.join(attrs)}]" if attrs else "[CQ:mface]")
                elif seg_type == "record":
                    attrs = []
                    file_id = seg_data.get("file") or seg_data.get("file_id") or ""
                    url = seg_data.get("url") or ""
                    if file_id:
                        attrs.append(f"file={cls._cq_escape(file_id)}")
                    if url:
                        attrs.append(f"url={cls._cq_escape(url)}")
                    parts.append(f"[CQ:record,{','.join(attrs)}]" if attrs else "[CQ:record]")
                elif seg_type == "forward":
                    fid = seg_data.get("id") or seg_data.get("message_id") or seg_data.get("file") or ""
                    parts.append(f"[CQ:forward,id={cls._cq_escape(fid)}]" if fid else "[CQ:forward]")
                elif seg_type == "video":
                    attrs = []
                    file_id = seg_data.get("file") or seg_data.get("file_id") or ""
                    url = seg_data.get("url") or ""
                    if file_id:
                        attrs.append(f"file={cls._cq_escape(file_id)}")
                    if url:
                        attrs.append(f"url={cls._cq_escape(url)}")
                    parts.append(f"[CQ:video,{','.join(attrs)}]" if attrs else "[CQ:video]")
                elif seg_type == "file":
                    attrs = []
                    for key in ("id", "file_id", "file", "name", "url", "path", "size"):
                        if seg_data.get(key) not in ("", None):
                            attrs.append(f"{key}={cls._cq_escape(seg_data.get(key))}")
                    parts.append(f"[CQ:file,{','.join(attrs)}]" if attrs else "[CQ:file]")
                elif seg_type == "share":
                    parts.append(f"[CQ:{seg_type}]")
                else:
                    text = seg_data.get("text") or seg_data.get("content") or ""
                    parts.append(str(text) if text else f"[CQ:{cls._cq_escape(seg_type)}]")
            return "".join(parts).strip()
        return str(message).strip()

    def _render_for_judge(self, raw_msg: str) -> str:
        text = str(raw_msg or "")
        if self.self_id:
            text = re.sub(
                rf"\[CQ:at,qq={re.escape(str(self.self_id))}\]",
                "@淑雪",
                text
            )
        text = re.sub(r"\[CQ:at,qq=(\d+)\]", r"@QQ\1", text)
        text = re.sub(r"\[CQ:image[^\]]*\]", "[图片/表情]", text)
        text = re.sub(r"\[CQ:mface[^\]]*\]", "[表情]", text)
        text = re.sub(r"\[CQ:record[^\]]*\]", "[语音消息]", text)
        text = re.sub(r"\[CQ:video[^\]]*\]", "[视频消息]", text)
        text = re.sub(r"\[CQ:file[^\]]*\]", "[文件消息]", text)
        text = re.sub(r"\[CQ:share[^\]]*\]", "[分享消息]", text)
        text = re.sub(r"\[CQ:[^\]]*\]", "[消息元素]", text)
        return text.strip()

    async def send_direct(self, target, content: str, is_group: bool = False):
        if not self.ws:
            return False
        try:
            content = re.sub(r"\[表情\]", "", content).strip()
            if not content:
                return False
            payload = {
                "action": "send_msg",
                "params": {
                    "message_type": "group" if is_group else "private",
                    "group_id" if is_group else "user_id": int(target),
                    "message": content
                }
            }
            await self.ws.send(json.dumps(payload))
            return True
        except Exception as e:
            audit.log("ERROR", "SEND", f"WebSocket 发送失败: {e}")
            return False

    async def call_api(self, action: str, params: dict | None = None, timeout: float = 5.0) -> dict | None:
        if not self.ws:
            return None
        loop = asyncio.get_running_loop()
        self._api_seq += 1
        echo = f"shuxue-api-{int(time.time() * 1000)}-{self._api_seq}"
        fut = loop.create_future()
        self._api_pending[echo] = fut
        try:
            await self.ws.send(json.dumps({
                "action": action,
                "params": params or {},
                "echo": echo,
            }))
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception as e:
            audit.log("WARN", "API", f"调用 NapCat API 失败 action={action}: {e}")
            return None
        finally:
            self._api_pending.pop(echo, None)

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return "未知"
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}小时{minutes}分{secs}秒"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    async def get_group_member_join_age(self, gid, uid, nickname: str = "", group_name: str = "") -> tuple[float, float | None]:
        saved = await db.get_group_member_join_time(str(gid), str(uid))
        join_ts = saved
        source = "db" if saved > 0 else "unknown"
        if join_ts <= 0:
            resp = await self.call_api(
                "get_group_member_info",
                {"group_id": int(gid), "user_id": int(uid), "no_cache": True},
                timeout=5.0,
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                try:
                    join_ts = float(data.get("join_time") or 0)
                except (TypeError, ValueError):
                    join_ts = 0.0
                if join_ts > 0:
                    source = "napcat"
                    await db.save_group_member_join_time(
                        str(gid), str(uid), join_ts,
                        nickname=nickname or str(data.get("nickname") or ""),
                        group_name=group_name,
                    )
        now = time.time()
        age = (now - join_ts) if join_ts > 0 and join_ts <= now + 60 else None
        audit.log(
            "INFO", "ADKICK",
            f"入群时长检查 group={gid} user={uid} join_time={int(join_ts) if join_ts else '未知'} "
            f"source={source} age={self._format_duration(age)}"
        )
        return join_ts, age

    async def fetch_forward_message(self, forward_id: str) -> dict | None:
        fid = str(forward_id or "").strip()
        if not fid:
            return None
        for params in ({"id": fid}, {"message_id": fid}):
            resp = await self.call_api("get_forward_msg", params, timeout=8.0)
            data = resp.get("data") if isinstance(resp, dict) else None
            if data:
                return data
        return None

    def _file_url_with_fname(self, url: str, name: str) -> str:
        try:
            if not url or not name:
                return url
            split = urlsplit(str(url))
            if split.scheme not in ("http", "https"):
                return url
            query = parse_qsl(split.query, keep_blank_values=True)
            changed = False
            replaced = []
            saw_fname = False
            for key, value in query:
                if key.lower() == "fname":
                    saw_fname = True
                    if not value:
                        value = str(name)
                        changed = True
                replaced.append((key, value))
            if not saw_fname:
                replaced.append(("fname", str(name)))
                changed = True
            if not changed:
                return url
            return urlunsplit((split.scheme, split.netloc, split.path, urlencode(replaced, doseq=True), split.fragment))
        except Exception:
            return url

    async def resolve_file_url(self, file_info: dict, force_api: bool = False) -> dict:
        info = dict(file_info or {})
        if not force_api and (info.get("url") or info.get("path")):
            return info
        file_id = str(info.get("file_id") or "").strip()
        if not file_id:
            return info
        clean_file_id = file_id.lstrip("/")
        api_candidates = [
            ("get_file", {"file_id": file_id}),
            ("get_file", {"file": file_id}),
            ("get_file", {"file_id": clean_file_id}),
            ("get_file", {"file": clean_file_id}),
        ]
        for action, params in api_candidates:
            audit.log("INFO", "FILESCAN", f"尝试通过NapCat解析文件: action={action} params={params}")
            resp = await self.call_api(action, params, timeout=8.0)
            data = resp.get("data") if isinstance(resp, dict) else None
            if not isinstance(data, dict):
                audit.log("WARN", "FILESCAN", f"NapCat文件解析无有效data: resp={str(resp)[:300]}")
                continue
            url = data.get("url") or data.get("file_url") or data.get("download_url")
            path = data.get("path") or data.get("file")
            name = data.get("name") or data.get("file_name") or data.get("filename")
            size = data.get("size") or data.get("file_size")
            audit.log(
                "INFO", "FILESCAN",
                f"NapCat文件解析返回: has_url={bool(url)} has_path={bool(path)} "
                f"name={name or ''} size={size or ''}"
            )
            if url:
                info["url"] = str(url)
            if path and not info.get("url"):
                info["url"] = str(path)
            if name and (not info.get("name") or info.get("name") == file_id):
                info["name"] = str(name)
            try:
                if size and not info.get("size"):
                    info["size"] = int(size)
            except (TypeError, ValueError):
                pass
            if info.get("url"):
                return info
        return info

    async def scan_file_messages(self, files: list[dict]) -> list:
        results = []
        for idx, file_info in enumerate((files or [])[:5], 1):
            resolved = await self.resolve_file_url(file_info)
            name = resolved.get("name") or f"群文件{idx}"
            url = resolved.get("url") or ""
            size = resolved.get("size") or 0
            name_has_apk = LocalFileScanner._filename_has_apk_suffix(name)
            if name_has_apk:
                fixed_url = self._file_url_with_fname(url, name)
                if fixed_url != url:
                    audit.log("INFO", "FILESCAN", f"APK下载URL补全fname参数: name={name}")
                    url = fixed_url
            audit.log("INFO", "FILESCAN", f"开始本地扫描文件{idx}: name={name} size={size or '未知'} url={'yes' if url else 'no'}")
            result = await LocalFileScanner.download_and_scan(url, name=name, size_hint=size or None)
            if name_has_apk and (
                result.size and result.size < 512 * 1024
                and not (result.apk_info and result.apk_info.get("is_apk"))
            ):
                audit.log(
                    "WARN", "FILESCAN",
                    f"APK下载结果过小且未识别为APK，疑似QQ下载占位/错误页，尝试通过NapCat get_file重新解析: "
                    f"name={name} downloaded_size={result.size}B"
                )
                forced = await self.resolve_file_url(file_info, force_api=True)
                forced_name = forced.get("name") or name
                forced_url = self._file_url_with_fname(forced.get("url") or forced.get("path") or "", forced_name)
                if forced_url and forced_url != url:
                    retry_result = await LocalFileScanner.download_and_scan(
                        forced_url,
                        name=forced_name,
                        size_hint=forced.get("size") or None,
                    )
                    audit.log(
                        "INFO", "FILESCAN",
                        f"APK重试下载扫描结果: name={forced_name} size={retry_result.size or '未知'} "
                        f"type={retry_result.file_type or 'unknown'} apk={bool(retry_result.apk_info.get('is_apk')) if retry_result.apk_info else False}"
                    )
                    result = retry_result
                    name = forced_name
            if name_has_apk and not (result.apk_info and result.apk_info.get("is_apk")):
                audit.log(
                    "WARN", "FILESCAN",
                    f"文件名疑似APK但扫描器未提取APK信息，启动host侧强制APK兜底: name={name} path={result.path or '无'}"
                )
                if result.path and os.path.exists(result.path) and LocalFileScanner._zip_looks_like_apk(result.path):
                    fallback_info = await asyncio.to_thread(LocalFileScanner._extract_apk_info, result.path, result.name or name)
                    fallback_info["is_apk"] = True
                    fallback_info["file_name"] = result.name or name
                    fallback_info["source"] = f"host-forced-{fallback_info.get('source', 'fallback')}"
                    result.apk_info = fallback_info
                else:
                    result.action = "suspect"
                    original_reason = f" (原扫描结果: {result.reason})" if result.reason else ""
                    result.reason = (
                        f"文件名疑似APK，但下载到的内容不是有效APK或下载不完整，无法提取名称/图标。"
                        f" 实际大小={result.size or '未知'}B 类型={result.file_type or 'unknown'}。{original_reason}"
                    )
            if getattr(result, "is_cached", False):
                pass
            elif result.apk_info and result.apk_info.get("is_apk"):
                audit.log(
                    "INFO", "APKREVIEW",
                    f"准备进行APK身份审核: file={result.name or name} app={result.apk_info.get('app_name') or '未知'} "
                    f"pkg={result.apk_info.get('package') or '未知'} icon={'yes' if result.apk_info.get('icon_data_url') else 'no'} "
                    f"source={result.apk_info.get('source') or 'unknown'}"
                )
                apk_review = await BrainInterpreter.review_apk_identity(result.apk_info, result.reason)
                
                result.apk_review = apk_review
                if apk_review.get("kick"):
                    result.action = "kick"
                    result.reason = (
                        f"APK身份审核判定为非正规/高风险应用，直接踢出。"
                        f" 应用名={result.apk_info.get('app_name') or '未知'} "
                        f"包名={result.apk_info.get('package') or '未知'} "
                        f"理由={apk_review.get('reason') or '无'}"
                    )
                else:
                    result.reason = (
                        f"{result.reason}；APK身份审核通过："
                        f"{apk_review.get('reason') or '看起来是正规游戏/软件'}"
                    )
            
            if not getattr(result, "is_cached", False):
                LocalFileScanner.save_cached_scan(result)

            audit.log(
                "WARN" if result.action in ("suspect", "kick") else "INFO",
                "FILESCAN",
                f"文件{idx}扫描结果 action={result.action} name={result.name} size={result.size or size or '未知'} "
                f"type={result.file_type or 'unknown'} reason={result.reason}"
            )
            results.append(result)
        return results

    def _forward_nodes_from_data(self, data) -> list:
        if not data:
            return []
        if isinstance(data, dict):
            if isinstance(data.get("message"), list):
                return data.get("message") or []
            if isinstance(data.get("messages"), list):
                return data.get("messages") or []
            if isinstance(data.get("content"), list):
                return data.get("content") or []
        if isinstance(data, list):
            return data
        return []

    def _node_sender_and_content(self, node) -> tuple[str, str, str]:
        if isinstance(node, dict):
            data = node.get("data") if isinstance(node.get("data"), dict) else node
            nickname = str(data.get("nickname") or data.get("name") or data.get("sender_name") or "未知")
            user_id = str(data.get("user_id") or data.get("uin") or data.get("sender_id") or "")
            content = data.get("content", data.get("message", ""))
            return nickname, user_id, self._normalize_message(content)
        return "未知", "", self._normalize_message(node)

    async def expand_forward_records(self, raw_msg: str) -> tuple[list[str], list[tuple[str, bool, str, dict]]]:
        forward_ids = []
        for attrs_str in re.findall(r"\[CQ:forward(?:,([^\]]*))?\]", str(raw_msg or "")):
            attrs = self._parse_cq_attrs(attrs_str or "")
            fid = attrs.get("id") or attrs.get("message_id") or attrs.get("file") or ""
            if fid:
                forward_ids.append(fid)
        if not forward_ids:
            return [], []

        expanded_parts = []
        review_images = []
        review_image_taken = False
        for idx, fid in enumerate(forward_ids[:3], 1):
            data = await self.fetch_forward_message(fid)
            nodes = self._forward_nodes_from_data(data)
            if not nodes:
                expanded_parts.append(f"[转发记录{idx}: 无法展开或内容为空 id={fid}]")
                continue
            expanded_parts.append(f"[转发记录{idx}开始，共{len(nodes)}条]")
            for nidx, node in enumerate(nodes[:30], 1):
                nick, user_id, content = self._node_sender_and_content(node)
                content_text = content
                node_images = self._extract_images_from_normalized(content)
                ocr_notes = []
                for img_idx, (url, is_sticker, file_id, sticker_meta) in enumerate(node_images[:8], 1):
                    cache_key = self._image_cache_key(file_id or url)
                    label = f"转发记录{idx}-第{nidx}条图片{img_idx}"
                    ocr_text = await self._ocr_image_once(cache_key, url, label)
                    ocr_notes.append(f"{label} OCR：{ocr_text or '无可读文字'}")
                    if not review_image_taken:
                        review_images.append({
                            "url": url,
                            "is_sticker": is_sticker,
                            "cache_key": cache_key,
                            "label": label,
                            "ocr_text": ocr_text,
                            "sticker_meta": sticker_meta,
                            "from_forward": True,
                        })
                        review_image_taken = True
                content_text = re.sub(r"\[CQ:image[^\]]*\]", "[图片]", content_text)
                content_text = re.sub(r"\[CQ:mface[^\]]*\]", "[表情]", content_text)
                content_text = re.sub(r"\[CQ:[^\]]*\]", "[消息元素]", content_text).strip()
                speaker = f"{nick}({user_id})" if user_id else nick
                line = f"[转发记录{idx}-第{nidx}条] 【原发言人:{speaker}】{content_text or '[空消息]'}"
                if ocr_notes:
                    line += " " + "；".join(ocr_notes)
                expanded_parts.append(line)
            if len(nodes) > 30:
                expanded_parts.append(f"[转发记录{idx}还有{len(nodes)-30}条未展开，已截断]")
            expanded_parts.append(f"[转发记录{idx}结束]")
        audit.log("INFO", "FORWARD", f"已展开合并转发 {len(forward_ids[:3])} 条，抽取复审原图 {len(review_images)} 张")
        return expanded_parts, review_images

    async def apply_ad_age_policy(self, gid, uid, nickname: str, group_name: str, ad_result: dict) -> dict:
        action = str(ad_result.get("action") or "").lower()
        if action not in ("suspect", "kick") and not ad_result.get("kick"):
            return ad_result

        reason = ad_result.get("reason", "")
        _, join_age = await self.get_group_member_join_age(gid, uid, nickname=nickname, group_name=group_name)

        if action == "suspect" and join_age is not None and 0 <= join_age < 3600:
            audit.log(
                "WARN", "ADKICK",
                f"疑似广告来自入群不足1小时成员，已升级为踢出 user={uid} "
                f"age={self._format_duration(join_age)} reason={reason}"
            )
            return {
                **ad_result,
                "action": "kick",
                "kick": True,
                "reason": f"{reason}；成员入群不足1小时，疑似广告按高风险处理",
            }

        if join_age is not None and join_age > 15 * 86400:
            suspect_count = await db.count_group_ad_suspects(str(gid), str(uid), time.time() - 7200)
            audit.log(
                "INFO", "ADKICK",
                f"老成员保护检查 group={gid} user={uid} age={self._format_duration(join_age)} "
                f"suspect_2h={suspect_count}"
            )
            if suspect_count < 2:
                await db.record_group_ad_suspect(str(gid), str(uid), action or "suspect", reason)
                if action == "kick" or ad_result.get("kick"):
                    audit.log(
                        "WARN", "ADKICK",
                        f"入群超过15天且2小时suspect记录<{2}，已将 kick 重定向为 suspect "
                        f"user={uid} count_before={suspect_count}"
                    )
                    return {
                        **ad_result,
                        "action": "suspect",
                        "kick": False,
                        "reason": f"{reason}；老成员保护：2小时内疑似记录不足2条，降级为只撤回",
                    }
                if action == "suspect":
                    audit.log(
                        "WARN", "ADKICK",
                        f"入群超过15天且2小时suspect记录<{2}，已将 suspect 重定向为安全放行 "
                        f"user={uid} count_before={suspect_count}"
                    )
                    return {
                        **ad_result,
                        "action": "pass",
                        "kick": False,
                        "reason": f"{reason}；老成员保护：2小时内疑似记录不足2条，本次安全放行",
                    }
        return ad_result

    async def send_temp_private(self, user_id, group_id, content: str) -> bool:
        if not self.ws:
            return False
        try:
            content = re.sub(r"\[表情\]", "", content).strip()
            if not content:
                return False
            payload = {
                "action": "send_private_msg",
                "params": {
                    "user_id": int(user_id),
                    "group_id": int(group_id),
                    "message": content,
                }
            }
            await self.ws.send(json.dumps(payload))
            audit.log("INFO", "SEND", f"已尝试通过群临时会话发送 user={user_id} group={group_id}")
            return True
        except Exception as e:
            audit.log("ERROR", "SEND", f"临时会话发送失败: {e}")
            return False

    async def approve_friend_request(self, flag, uid, nickname: str = "") -> bool:
        if not self.ws or not flag:
            return False
        try:
            payload = {
                "action": "set_friend_add_request",
                "params": {
                    "flag": flag,
                    "approve": True,
                    "remark": nickname or str(uid or ""),
                }
            }
            await self.ws.send(json.dumps(payload))
            audit.log("SUCCESS", "FRIEND", f"已同意好友申请 user={uid} remark={nickname}")
            return True
        except Exception as e:
            audit.log("ERROR", "FRIEND", f"同意好友申请失败: {e}")
            return False

    async def kick_group_member(self, gid, uid, reason: str = "") -> bool:
        if not self.ws:
            return False
        try:
            payload = {
                "action": "set_group_kick",
                "params": {
                    "group_id": int(gid),
                    "user_id": int(uid),
                    "reject_add_request": False,
                }
            }
            await self.ws.send(json.dumps(payload))
            audit.log("WARN", "ADKICK", f"已请求踢出群成员 group={gid} user={uid} reason={reason}")
            return True
        except Exception as e:
            audit.log("ERROR", "ADKICK", f"踢人请求发送失败: {e}")
            return False

    async def revoke_message(self, message_id) -> bool:
        if not self.ws or message_id is None:
            return False
        try:
            payload = {
                "action": "delete_msg",
                "params": {"message_id": int(message_id)}
            }
            await self.ws.send(json.dumps(payload))
            audit.log("WARN", "ADKICK", f"已请求撤回广告消息 message_id={message_id}")
            return True
        except Exception as e:
            audit.log("ERROR", "ADKICK", f"撤回消息请求发送失败: {e}")
            return False

    async def review_and_kick_ad_sender(
        self, gid, uid, nickname: str, group_name: str, message_text: str,
        first_judgement: dict | None = None, message_id=None, message_ids: list | None = None,
        images: list | None = None
    ) -> dict:
        # Return action flags for the caller: stop normal flow, or force a normal reply.
        if not gid or not await db.is_group_ad_kick_enabled(str(gid)):
            if first_judgement and first_judgement.get("ad_kick_candidate"):
                audit.log("INFO", "ADKICK", "本群广告封锁未开启，广告候选只参与回复判断，不执行撤回/踢人。")
            return {"stop": False, "force_reply": False, "ad_action": "disabled", "ad_reviewed": False}
        uid_str = str(uid)
        if uid_str in (GlobalConfig.ADMIN_UID, str(self.self_id or "")):
            return {"stop": False, "force_reply": False, "ad_action": "bypass", "ad_reviewed": False}
        first = None
        if first_judgement:
            try:
                ad_confidence = float(first_judgement.get("ad_confidence") or 0.0)
            except (TypeError, ValueError):
                ad_confidence = 0.0
            first = {
                "kick": BrainInterpreter._coerce_model_bool(first_judgement.get("ad_kick_candidate")),
                "confidence": max(0.0, min(1.0, ad_confidence)),
                "reason": first_judgement.get("reason") or "群消息初筛命中广告候选",
            }
        ad_result = await BrainInterpreter.should_kick_ad_sender(
            group_name or f"群{gid}", nickname, uid_str, message_text, first=first, images=images
        )
        ad_result = await self.apply_ad_age_policy(gid, uid, nickname, group_name, ad_result)
        if ad_result.get("action") == "suspect":
            reason = ad_result.get("reason", "疑似广告")
            revoke_ids = message_ids or ([message_id] if message_id is not None else [])
            seen_ids = set()
            for mid in revoke_ids:
                if mid is None or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                await self.revoke_message(mid)
            audit.log("WARN", "ADKICK", f"疑似广告已撤回但不踢人 user={uid_str} reason={reason}")
            return {"stop": True, "force_reply": False, "ad_action": "suspect", "ad_reviewed": True}
        if not ad_result.get("kick"):
            if ad_result.get("action") == "ignore":
                audit.log("INFO", "ADKICK", f"广告复审判定为非违规且无关，已忽略: {ad_result.get('reason', '')}")
                return {"stop": True, "force_reply": False, "ad_action": "ignore", "ad_reviewed": True}
            if ad_result.get("action") == "reply":
                audit.log("INFO", "ADKICK", f"广告复审判定为非广告且适合回复: {ad_result.get('reason', '')}")
                return {"stop": False, "force_reply": True, "ad_action": "reply", "ad_reviewed": True}
            return {"stop": False, "force_reply": False, "ad_action": ad_result.get("action") or "pass", "ad_reviewed": True}
        reason = ad_result.get("reason", "广告审核命中")
        revoke_ids = message_ids or ([message_id] if message_id is not None else [])
        seen_ids = set()
        for mid in revoke_ids:
            if mid is None or mid in seen_ids:
                continue
            seen_ids.add(mid)
            await self.revoke_message(mid)
        kicked = await self.kick_group_member(gid, uid, reason)
        if kicked:
            snarks = [
                "广告牌请去门口立着，别贴淑雪脸上。",
                "这种小广告，淑雪看一眼都嫌占缓存。",
                "群里空气净化完成，哼。",
                "发广告还想混进来，想得挺美。",
            ]
            await self.send_direct(
                gid,
                random.choice(snarks),
                True
            )
        return {"stop": True, "force_reply": False, "ad_action": "kick", "ad_reviewed": True}

    async def handle_file_scan_result(
        self, gid, uid, nickname: str, group_name: str,
        scan_result, message_id=None, message_ids: list | None = None
    ) -> dict:
        if not gid or str(uid) in (GlobalConfig.ADMIN_UID, str(self.self_id or "")):
            return {"stop": False, "file_action": "bypass"}
        if not scan_result or getattr(scan_result, "action", "safe") not in ("suspect", "kick"):
            return {"stop": False, "file_action": "safe"}
        if getattr(scan_result, "action", "safe") == "kick":
            reason = getattr(scan_result, "reason", "APK身份审核判定为高风险")
            revoke_ids = message_ids or ([message_id] if message_id is not None else [])
            seen_ids = set()
            for mid in revoke_ids:
                if mid is None or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                await self.revoke_message(mid)
            kicked = await self.kick_group_member(gid, uid, reason)
            if kicked:
                await self.send_direct(gid, "这个 APK 身份太可疑，风纪委员直接请出场。", True)
            audit.log("WARN", "FILESCAN", f"APK高风险文件已直接kick user={uid} reason={reason}")
            return {"stop": True, "file_action": "kick"}
        ad_result = await self.apply_ad_age_policy(
            gid, uid, nickname, group_name, scan_result.to_ad_result()
        )
        action = str(ad_result.get("action") or "").lower()
        reason = ad_result.get("reason") or getattr(scan_result, "reason", "文件安全扫描命中风险")
        revoke_ids = message_ids or ([message_id] if message_id is not None else [])
        if action == "pass" and not ad_result.get("kick"):
            audit.log("INFO", "FILESCAN", f"文件风险经成员保护规则降级放行 user={uid} reason={reason}")
            return {"stop": False, "file_action": "pass"}
        seen_ids = set()
        for mid in revoke_ids:
            if mid is None or mid in seen_ids:
                continue
            seen_ids.add(mid)
            await self.revoke_message(mid)
        if action == "suspect" and not ad_result.get("kick"):
            audit.log("WARN", "FILESCAN", f"疑似风险文件已撤回但不踢人 user={uid} reason={reason}")
            return {"stop": True, "file_action": "suspect"}
        kicked = await self.kick_group_member(gid, uid, reason)
        if kicked:
            await self.send_direct(gid, "风险文件先收走啦，风纪委员可不是摆设。", True)
        return {"stop": True, "file_action": "kick" if kicked else "suspect"}

    async def handle_message(self, data: dict):
        uid      = data.get("user_id")
        gid      = data.get("group_id")
        message_id = data.get("message_id")
        raw_msg  = self._normalize_message(data.get("message", ""))
        nickname = data.get("sender", {}).get("nickname", "未知")
        m_type   = "group" if gid else "private"
        tid      = str(gid or uid)

        group_name = None
        if gid:
            group_name = data.get("group_name") or data.get("group", {}).get("name")
            if not group_name:
                group_name = await db.get_group_name(str(gid))
            if not group_name:
                group_name = f"群{gid}"

        if await self.cmd_handler.execute(uid, gid, raw_msg, group_name=group_name): return

        prev_msg = None
        if m_type == "group":
            gid_str  = str(gid)
            await db.save_group_member_seen(gid_str, str(uid), nickname, group_name)
            prev_msg = await db.get_group_last_msg(gid_str)
            readable = self._render_for_judge(raw_msg)
            if readable:
                await db.save_group_last_msg(gid_str, nickname, readable, user_id=str(uid))

        has_at_bot = False
        if m_type == "private":
            should_reply = True
        elif m_type == "group":
            has_share = bool(re.search(r"\[CQ:share", raw_msg))
            has_forward = bool(re.search(r"\[CQ:forward", raw_msg))
            has_image = bool(re.search(r"\[CQ:(?:image|mface)[,\]]", raw_msg))
            has_video = bool(re.search(r"\[CQ:video[,\]]", raw_msg))
            has_file = bool(re.search(r"\[CQ:file[,\]]", raw_msg))
            has_record = bool(re.search(r"\[CQ:record[,\]]", raw_msg))
            has_at_bot = bool(self.self_id and re.search(rf"\[CQ:at,qq={re.escape(str(self.self_id))}\]", raw_msg))
            if not has_at_bot and not has_image and not has_video and not has_file and not has_share and not has_record and not has_forward and len(re.sub(r"\[CQ:[^\]]*\]", "", raw_msg).strip()) < 2:
                return
            should_reply = True
        else:
            should_reply = False

        if not should_reply: return

        audit.log("INFO", "RECV", f"[{nickname}]: {raw_msg}")

        cq_records = re.findall(r"\[CQ:record(?:,([^\]]*))?\]", raw_msg)
        record_texts = []
        for attrs_str in cq_records:
            attrs_str = attrs_str or ""
            url_match = re.search(r"url=([^\],\[]+)", attrs_str)
            file_match = re.search(r"file=([^\],\[]+)", attrs_str)
            if not url_match:
                audit.log("WARN", "ASR", f"语音消息缺少可访问 URL，暂时无法转写: {raw_msg}")
                record_texts.append("[语言消息] 对方发来了一条语音，但没有可访问的语音URL，无法转写。")
                continue
            url = unescape(url_match.group(1).strip())
            text = await self._transcribe_audio(url)
            if text:
                record_texts.append(f"[语言消息] {text}")
            else:
                record_texts.append("[语言消息] 对方发来了一条语音，但语音转文字失败。")

        image_list = self._extract_images_from_normalized(raw_msg)
        video_list = self._extract_videos_from_normalized(raw_msg)
        file_list = self._extract_files_from_normalized(raw_msg)

        file_scan_results = []
        if file_list:
            file_scan_results = await self.scan_file_messages(file_list)
            if m_type == "group":
                for scan_result in file_scan_results:
                    handling = await self.handle_file_scan_result(
                        gid, uid, nickname, group_name, scan_result,
                        message_id=message_id,
                        message_ids=[message_id] if message_id is not None else [],
                    )
                    if handling.get("stop"):
                        return
                if any(getattr(r, "action", "safe") in ("suspect", "kick") for r in file_scan_results):
                    audit.log("INFO", "FILESCAN", "风险文件已按成员保护规则处理完成，本轮不再进入模型回复链路。")
                    return
            if any(getattr(r, "action", "safe") in ("suspect", "kick") for r in file_scan_results):
                summary = "；".join(f"{r.name or '文件'}：{r.reason}" for r in file_scan_results if r.action in ("suspect", "kick"))
                await self.send_direct(uid, f"这个文件本地扫描有风险，先别打开：{summary}", False)
                return

        clean_input = raw_msg.strip()
        clean_input = re.sub(r"\[CQ:image[^\]]*\]", "", clean_input).strip()
        clean_input = re.sub(r"\[CQ:mface[^\]]*\]", "", clean_input).strip()
        clean_input = re.sub(r"\[CQ:video[^\]]*\]", "", clean_input).strip()
        clean_input = re.sub(r"\[CQ:file[^\]]*\]", "", clean_input).strip()
        clean_input = re.sub(r"\[CQ:record[^\]]*\]", "", clean_input).strip()
        clean_input = re.sub(r"\[CQ:forward[^\]]*\]", "", clean_input).strip()
        clean_input = self._limit_user_input_text(clean_input, "用户文本")
        image_payloads = []

        if file_scan_results:
            file_context = " ".join(
                f"[对方发送了文件{name_idx}：{r.name or '群文件'}，本地安全扫描结果：{r.action}；{r.reason}]"
                for name_idx, r in enumerate(file_scan_results, 1)
            )
            clean_input = f"{file_context} 对方同时说：{clean_input}" if clean_input else file_context
            clean_input = self._limit_user_input_text(clean_input, "文件扫描上下文")
            file_only = (
                not image_list and not video_list and not cq_records
                and not re.search(r"\[CQ:(?:forward|share)[,\]]", raw_msg)
            )
            if file_only:
                mentioned_bot = m_type == "private" or has_at_bot or bool(re.search(r"(淑雪|小雪)", clean_input))
                if mentioned_bot:
                    summary = "；".join(
                        f"{r.name or '群文件'}：{'未发现风险' if r.action == 'safe' else '疑似风险'}"
                        for r in file_scan_results[:3]
                    )
                    await self.send_direct(gid or uid, f"本地文件扫描完成：{summary}。", gid is not None)
                else:
                    audit.log("INFO", "FILESCAN", "群文件已完成本地安全扫描，未点名淑雪，本轮不进入模型回复链路。")
                return
            if not has_at_bot and not re.search(r"(淑雪|小雪)", clean_input) and all(r.action == "safe" for r in file_scan_results):
                audit.log("INFO", "FILESCAN", "群文件已完成本地安全扫描且未点名淑雪，跳过模型回复。")
                return

        forward_parts, forward_review_images = await self.expand_forward_records(raw_msg)
        if forward_parts:
            forward_context = "\n".join(forward_parts)
            clean_input = f"{forward_context}\n对方同时说：{clean_input}" if clean_input else forward_context
            audit.log("INFO", "FORWARD", f"合并转发已拼接进审核文本，长度={len(forward_context)}")
        if forward_review_images:
            image_payloads.extend(forward_review_images[:1])

        if record_texts:
            voice_context = " ".join(record_texts)
            voice_context = self._limit_user_input_text(voice_context, "语音转写文本")
            clean_input = f"{voice_context} 对方同时说：{clean_input}" if clean_input else voice_context
            audit.log("INFO", "ASR", f"语音上下文注入完成 -> {clean_input}")

        if video_list:
            video_parts = []
            for video_idx, (url, file_id) in enumerate(video_list[:2], 1):
                cache_key = self._image_cache_key(file_id or url)
                label = f"视频{video_idx}"
                frames = await self._extract_video_frames_once(cache_key, url, label)
                if not frames:
                    video_parts.append(f"[对方发来了{label}，但视频抽帧失败，初审只能依据文字判断。]")
                    continue
                for frame_idx, frame_url in enumerate(frames, 1):
                    image_payloads.append({
                        "url": frame_url,
                        "is_sticker": False,
                        "cache_key": f"video:{cache_key}:frame:{frame_idx}",
                        "label": f"{label}随机帧{frame_idx}",
                        "ocr_text": "",
                        "sticker_meta": {},
                        "from_video": True,
                    })
                video_parts.append(
                    f"[对方发来了{label}，已随机抽取{len(frames)}帧压缩图；视频不做OCR，初审模型需要直接查看这些帧判断广告风险和是否需要回复。]"
                )
            if video_parts:
                video_context = " ".join(video_parts)
                clean_input = f"{video_context} 对方同时说：{clean_input}" if clean_input else video_context
                clean_input = self._limit_user_input_text(clean_input, "视频上下文")
                audit.log("INFO", "VIDEO", f"视频抽帧上下文注入完成 frames={sum(1 for img in image_payloads if img.get('from_video'))} -> {clean_input}")

        if self.self_id:
            clean_input = re.sub(
                rf"\[CQ:at,qq={re.escape(str(self.self_id))}\]",
                "@淑雪",
                clean_input
            )
        clean_input = re.sub(r"\[CQ:at,qq=(\d+)\]", r"@QQ\1", clean_input).strip()

        if m_type == "group" and image_list:
            all_stickers = all(is_sticker for _, is_sticker, _, _ in image_list)
            meaningful_text = re.sub(r"@QQ\d+", "", clean_input).strip()
            if all_stickers and not has_at_bot and not record_texts and not meaningful_text:
                audit.log("INFO", "JUDGE", f"群聊纯表情包未点名淑雪，仍进入广告审核/收藏链路但默认不回复: [{nickname}]")

        if image_list:
            desc_parts = []
            img_counter = 1
            image_event_count = self._register_image_events(tid, len(image_list))
            if image_event_count > self.IMAGE_SPAM_LIMIT:
                sticker_count = sum(1 for _, is_sticker, _, _ in image_list if is_sticker)
                image_count = len(image_list) - sticker_count
                desc_bits = []
                if sticker_count:
                    desc_bits.append(f"{sticker_count}个表情包")
                if image_count:
                    desc_bits.append(f"{image_count}张图片")
                image_context = (
                    f"[群里短时间连续发送了{'、'.join(desc_bits) or '多张图片/表情'}，"
                    "疑似图片/表情刷屏。为节省资源，淑雪没有逐张识别。]"
                )
                clean_input = f"{image_context} 对方同时说：{clean_input}" if clean_input else image_context
                clean_input = self._limit_user_input_text(clean_input, "最终输入")
                audit.log(
                    "WARN", "VISION",
                    f"图片/表情刷屏限流: tid={tid}, window_count={image_event_count}, current={len(image_list)}"
                )
            else:
                seen_image_keys = set()
                for url, is_sticker, file_id, sticker_meta in image_list:
                    cache_key = self._image_cache_key(file_id or url)
                    if cache_key in seen_image_keys:
                        label = "表情包" if is_sticker else f"图片{img_counter}"
                        if not is_sticker:
                            img_counter += 1
                        desc_parts.append(f"[{label}与前面重复，已合并为同一张图处理]")
                        continue
                    seen_image_keys.add(cache_key)
                    label = "表情包" if is_sticker else f"图片{img_counter}"
                    if not is_sticker: img_counter += 1
                    if not is_sticker and not self._allow_real_image_for_user(str(uid)):
                        desc_parts.append(
                            f"[{label}超过该用户每分钟{self.REAL_IMAGE_USER_LIMIT}张图片的识别上限，已降级为未查看原图处理]"
                        )
                        audit.log(
                            "WARN", "VISION",
                            f"用户图片限流降级: uid={uid}, limit={self.REAL_IMAGE_USER_LIMIT}/min, url={url[:60]}..."
                        )
                        continue
                    ocr_text = await self._ocr_image_once(cache_key, url, label)
                    image_payloads.append({
                        "url": url,
                        "is_sticker": is_sticker,
                        "cache_key": cache_key,
                        "label": label,
                        "ocr_text": ocr_text,
                        "sticker_meta": sticker_meta,
                    })
                    ocr_note = f" OCR/文字风险摘要：{ocr_text}" if ocr_text else " OCR未识别到可用文字。"
                    if is_sticker:
                        desc_parts.append(f"[对方发来了{label}，只有在它明确回应淑雪或正在连续对话时才需要回复。{ocr_note}]")
                    else:
                        desc_parts.append(f"[对方发来了{label}，请先依据OCR文字判断广告风险和是否是发给淑雪看的，再决定是否回复。{ocr_note}]")
                image_context = " ".join(desc_parts)
                clean_input = f"{image_context} 对方同时说：{clean_input}" if clean_input else image_context
                clean_input = self._limit_user_input_text(clean_input, "最终输入")
                audit.log("INFO", "VISION", f"已收集 {len(image_payloads)} 张原生视觉输入 -> {clean_input}")

        if not clean_input:
            audit.log("WARN", "RECV", "消息清洗后为空，跳过推理。")
            return

        pending_key = f"group:{gid}:{uid}" if m_type == "group" else f"private:{uid}"
        if pending_key not in self._pending:
            self._pending[pending_key] = {"msgs": [], "meta": None, "message_ids": [], "images": []}
        queued_input = clean_input
        if m_type == "group":
            queued_input = f"【发言人:{nickname}({uid})】{clean_input}"
        self._pending[pending_key]["msgs"].append(queued_input)
        if image_payloads:
            existing_image_keys = {
                img.get("cache_key") for img in self._pending[pending_key].setdefault("images", [])
            }
            for payload in image_payloads:
                cache_key = payload.get("cache_key")
                if cache_key and cache_key in existing_image_keys:
                    continue
                self._pending[pending_key]["images"].append(payload)
                if cache_key:
                    existing_image_keys.add(cache_key)
        if message_id is not None:
            self._pending[pending_key].setdefault("message_ids", []).append(message_id)
        self._pending[pending_key]["meta"] = {
            "uid": uid, "gid": gid, "nickname": nickname, "m_type": m_type,
            "group_name": group_name, "prev_msg": prev_msg, "message_id": message_id,
            "message_ids": list(self._pending[pending_key].get("message_ids") or []),
            "images": list(self._pending[pending_key].get("images") or []),
        }
        old_task = self._pending[pending_key].get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        self._pending[pending_key]["task"] = asyncio.create_task(self._flush_after_debounce(pending_key))

    async def handle_message_safe(self, data: dict):
        # Keep background message tasks from losing tracebacks.
        try:
            await self.handle_message(data)
        except Exception:
            audit.log("ERROR", "TASK", f"消息处理异常:\n{traceback.format_exc()}")

    async def _flush_after_debounce(self, tid: str):
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await self._flush_pending_bucket(tid)
        except Exception:
            audit.log("ERROR", "TASK", f"延迟消息处理异常:\n{traceback.format_exc()}")

    async def _flush_pending_bucket(self, tid: str):
        bucket = self._pending.pop(tid, None)
        if not bucket or not bucket["msgs"]:
            return

        msgs       = bucket["msgs"]
        meta       = bucket["meta"]
        uid        = meta["uid"]
        gid        = meta["gid"]
        nickname   = meta["nickname"]
        m_type     = meta["m_type"]
        group_name = meta.get("group_name")
        prev_msg   = meta.get("prev_msg")
        message_id = meta.get("message_id")
        message_ids = meta.get("message_ids") or ([message_id] if message_id is not None else [])
        images     = meta.get("images") or []

        if len(msgs) == 1:
            combined_input = msgs[0]
            audit.log("INFO", "PROC", f"[{nickname}] 单条消息处理: {combined_input}")
        else:
            numbered = "\n".join(f"[第{i+1}条] {m}" for i, m in enumerate(msgs))
            combined_input = (
                f"【对方连续发来了{len(msgs)}条消息，你可以选择只回复一句，也可以使用[CONTINUE]分开回复。】\n"
                f"{numbered}"
            )
            audit.log("INFO", "PROC", f"[{nickname}] 合并 {len(msgs)} 条消息处理")
        combined_input = self._limit_user_input_text(combined_input, "合并输入")

        if m_type == "group":
            history = await db.fetch_context(str(gid or uid), limit=6, target_type="group")
            judge_images = [img for img in images if isinstance(img, dict) and img.get("from_video")]
            group_judgement = await BrainInterpreter.judge_group_message(
                combined_input, history, nickname, prev_msg, judge_images=judge_images
            )
            ad_handling = await self.review_and_kick_ad_sender(
                gid, uid, nickname, group_name, combined_input,
                first_judgement=group_judgement, message_id=message_id,
                message_ids=message_ids, images=images
            )
            if ad_handling.get("stop"):
                if ad_handling.get("ad_action") == "ignore":
                    await self._collect_non_ad_stickers(images, True)
                return
            sticker_safe = (
                not group_judgement.get("ad_kick_candidate")
                or ad_handling.get("ad_action") in ("pass", "ignore", "reply", "bypass")
            )
            await self._collect_non_ad_stickers(images, sticker_safe)
            should_reply = bool(ad_handling.get("force_reply")) or group_judgement.get("reply", False)
            if not should_reply:
                audit.log("INFO", "JUDGE", "判断层拦截，淑雪选择沉默。")
                return
        elif images:
            await self._collect_non_ad_stickers(images, True)

        if images:
            responses = await BrainInterpreter.process_multimodal_interaction(
                gid or uid, combined_input, images, nickname, m_type, self,
                group_name=group_name,
                sender_uid=uid
            )
        else:
            responses = await BrainInterpreter.process_interaction(
                gid or uid, combined_input, nickname, m_type, self,
                group_name=group_name,
                sender_uid=uid          # 群聊时传入真实发言人 UID，避免和 group_id 混淆
            )
        for r in responses:
            await self.send_direct(gid or uid, r, gid is not None)
            await asyncio.sleep(random.uniform(0.8, 1.5))

    async def handle_notice(self, data: dict):
        if data.get("notice_type") != "group_increase":
            return
        gid = data.get("group_id")
        uid = data.get("user_id")
        if not gid or not uid:
            return
        join_ts = data.get("time") or time.time()
        group_name = await db.get_group_name(str(gid)) or f"群{gid}"
        await db.save_group_member_join_time(str(gid), str(uid), join_ts, group_name=group_name)
        audit.log("INFO", "GROUP", f"记录新成员入群时间 group={gid} user={uid} join_time={join_ts}")

    async def listen(self):
        shared.set_bot_loop(asyncio.get_running_loop())
        # 启动 Web 控制台（守护线程）
        self._console = ConsoleServer(self)
        self._console.start()
        # 启动时预热 HTTP Session，减少多条消息并发触发初始化的竞争窗口。
        await _get_http_session()
        task = asyncio.create_task(SocialScheduler(self).run_loop())
        audit.log("INFO", "SOCIAL", f"心跳任务已创建: {task}")
        reconnect_interval = 5
        while True:
            try:
                async with websockets.connect(self.ws_uri) as ws:
                    self.ws = ws
                    audit.log("SUCCESS", "NET", "已接入 NapCat 通讯链路")
                    reconnect_interval = 5
                    await self.send_startup_overview()
                    while True:
                        raw_data = await ws.recv()
                        data = json.loads(raw_data)
                        echo = data.get("echo")
                        if echo in self._api_pending:
                            fut = self._api_pending.get(echo)
                            if fut and not fut.done():
                                fut.set_result(data)
                            continue
                        if data.get("meta_event_type") == "lifecycle":
                            self.self_id = data.get("self_id")
                            continue
                        if data.get("post_type") == "message":
                            asyncio.create_task(self.handle_message_safe(data))
                        elif data.get("post_type") == "notice":
                            asyncio.create_task(self.handle_notice(data))
                        elif data.get("post_type") == "request" and data.get("request_type") == "friend":
                            uid = data.get("user_id")
                            known_group = await db.get_recent_group_for_user(str(uid))
                            if known_group or str(uid) == GlobalConfig.ADMIN_UID:
                                await self.approve_friend_request(
                                    data.get("flag"),
                                    uid,
                                    known_group.get("nickname") if known_group else "管理员"
                                )
                            else:
                                audit.log("INFO", "FRIEND", f"收到陌生好友申请，未自动处理 user={uid}")
            except Exception as e:
                audit.log("WARN", "RETRY", f"连接意外中断 ({e})，将在 {reconnect_interval}s 后重连")
                await asyncio.sleep(reconnect_interval)
                reconnect_interval = min(60, reconnect_interval * 2)


# ==========================================
# 11. 系统主入口
# ==========================================


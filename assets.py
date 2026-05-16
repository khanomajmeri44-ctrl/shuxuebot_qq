"""表情包资源管理。

负责本地表情包查找、CQ 码生成，以及 GIF 尺寸/帧数压缩。
"""

from .shared import *
from .shared import _PIL_AVAILABLE, _PILImage

class AssetManager:
    _emote_cache: dict = {"files": [], "ts": 0.0}
    _EMOTE_CACHE_TTL = 60

    @staticmethod
    def _cq_escape(value) -> str:
        text = str(value if value is not None else "")
        return (
            text.replace("&", "&amp;")
                .replace("[", "&#91;")
                .replace("]", "&#93;")
                .replace(",", "&#44;")
        )

    @staticmethod
    def _find_emote_path(name: str) -> str | None:
        for ext in ['.png', '.jpg', '.jpeg', '.gif']:
            path = os.path.join(GlobalConfig.EMOTE_DIR, f"{name}{ext}")
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def _metadata_path(image_path: str) -> str:
        base, _ = os.path.splitext(image_path)
        return f"{base}.json"

    @staticmethod
    def _load_sticker_metadata(image_path: str) -> dict:
        meta_path = AssetManager._metadata_path(image_path)
        if not os.path.exists(meta_path):
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            audit.log("WARN", "ASSET", f"表情元数据读取失败: {meta_path} | {e}")
            return {}

    @staticmethod
    def get_emote_list() -> str:
        try:
            now = time.time()
            cache = AssetManager._emote_cache
            if cache["files"] and (now - cache["ts"]) < AssetManager._EMOTE_CACHE_TTL:
                files = cache["files"]
                return ", ".join(random.sample(files, min(len(files), 8)))
            if not os.path.exists(GlobalConfig.EMOTE_DIR):
                return "暂无（目录未创建）"
            files = [
                os.path.splitext(f)[0]
                for f in os.listdir(GlobalConfig.EMOTE_DIR)
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))
            ]
            if not files:
                return "空"
            AssetManager._emote_cache = {"files": files, "ts": now}
            return ", ".join(random.sample(files, min(len(files), 8)))
        except Exception as e:
            audit.log("ERROR", "ASSET", f"表情列表读取失败: {e}")
            return "目录读取异常"

    @staticmethod
    def convert_to_cq(emote_name: str) -> str:
        name = emote_name.strip()
        found_path = AssetManager._find_emote_path(name)
        if not found_path:
            audit.log("WARN", "ASSET", f"未找到表情文件: {name}")
            return f"(想发个名为 {name} 的表情，但是找不到了QwQ)"

        metadata = AssetManager._load_sticker_metadata(found_path)
        emoji_id = str(metadata.get("emoji_id") or "").strip()
        emoji_package_id = str(metadata.get("emoji_package_id") or "").strip()
        if emoji_id and emoji_package_id:
            fields = [
                f"emoji_id={AssetManager._cq_escape(emoji_id)}",
                f"emoji_package_id={AssetManager._cq_escape(emoji_package_id)}",
            ]
            key = str(metadata.get("key") or "").strip()
            if key:
                fields.append(f"key={AssetManager._cq_escape(key)}")
            summary = str(metadata.get("summary") or name).strip()
            if summary:
                fields.append(f"summary={AssetManager._cq_escape(summary)}")
            audit.log("INFO", "ASSET", f"使用 mface 表情段发送: {name}")
            return f"[CQ:mface,{','.join(fields)}]"

        # [FIX v10.4] 大文件保护：图片超过 1MB 时拒绝 Base64 注入，
        # 防止巨型 JSON Payload 触发 NapCat 长度限制
        try:
            file_size_kb = os.path.getsize(found_path) // 1024
            if file_size_kb > 3000:
                audit.log("WARN", "ASSET", f"表情文件过大({file_size_kb}KB)，跳过 Base64 注入: {name}")
                return f"（{name} 这张太大了发不出来...）"
        except OSError:
            pass
        try:
            with open(found_path, "rb") as image_file:
                raw_data = image_file.read()
                b64_str = base64.b64encode(raw_data).decode('utf-8').replace("\n", "").replace("\r", "")
                summary = AssetManager._cq_escape(name)
                return f"[CQ:image,file=base64://{b64_str},sub_type=1,summary={summary}]"
        except Exception as e:
            audit.log("ERROR", "ASSET", f"图片重编码致命错误: {name} | {e}")
            return f"（淑雪试图给你发一张【{name}】，但被系统拦截了...）"

    @staticmethod
    def clean_unsupported_tags(text: str) -> str:
        # [FIX v10.4] 原正则 r"\[IMG:(?!.*?\])" 使用了否定前瞻+懒惰量词，
        # 在 AI 输出漏掉右括号时会发生灾难性回溯（catastrophic backtracking）。
        # 改为：只清理"有 [IMG: 开头但没有配对 ] 的残缺标签"，使用固定长度前缀匹配。
        return re.sub(r"\[IMG:[^\]]{0,50}$", "", text, flags=re.MULTILINE)

    @staticmethod
    def _hash_of_existing_emotes() -> set:
        hashes = set()
        try:
            for fname in os.listdir(GlobalConfig.EMOTE_DIR):
                fpath = os.path.join(GlobalConfig.EMOTE_DIR, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        hashes.add(hashlib.md5(f.read()).hexdigest())
        except Exception:
            pass
        return hashes

    @staticmethod
    def extract_gif_frames(data: bytes) -> list[str] | None:
        # [FIX v10.4] 软降级：PIL 未安装时直接返回 None，不抛出 ImportError
        if not _PIL_AVAILABLE:
            audit.log("WARN", "EMOTE", "Pillow 未安装，跳过 GIF 帧提取。")
            return None
        try:
            img = _PILImage.open(io.BytesIO(data))
            total = getattr(img, "n_frames", 1)
            max_f = GlobalConfig.GIF_MAX_FRAMES
            if total <= max_f:
                indices = list(range(total))
            else:
                mid = total // 2
                half = max_f // 2
                start = max(0, mid - half)
                end = min(total, start + max_f)
                step = max(1, (end - start) // max_f)
                indices = list(range(start, end, step))[:max_f]
            frames_b64 = []
            for idx in indices:
                img.seek(idx)
                buf = io.BytesIO()
                img.convert("RGBA").save(buf, format="PNG")
                frames_b64.append(base64.b64encode(buf.getvalue()).decode())
            audit.log("INFO", "EMOTE", f"GIF 共 {total} 帧，抽取 {len(frames_b64)} 帧用于命名")
            return frames_b64
        except Exception as e:
            audit.log("ERROR", "EMOTE", f"GIF 帧提取失败: {e}")
            return None

    @staticmethod
    def save_sticker_bytes(data: bytes, name: str, ext: str, metadata: dict | None = None) -> bool:
        try:
            save_path = os.path.join(GlobalConfig.EMOTE_DIR, f"{name}{ext}")
            with open(save_path, "wb") as f:
                f.write(data)
            if metadata:
                allowed = {"emoji_id", "emoji_package_id", "key", "summary", "source_type"}
                clean_meta = {
                    k: str(v)
                    for k, v in metadata.items()
                    if k in allowed and v is not None and str(v).strip()
                }
                if clean_meta:
                    if not clean_meta.get("summary"):
                        clean_meta["summary"] = name
                    with open(AssetManager._metadata_path(save_path), "w", encoding="utf-8") as f:
                        json.dump(clean_meta, f, ensure_ascii=False, indent=2)
            audit.log("SUCCESS", "EMOTE", f"表情包已保存: {name}{ext} ({len(data)//1024}KB)")
            return True
        except Exception as e:
            audit.log("ERROR", "EMOTE", f"表情包写入失败: {name} | {e}")
            return False


# ==========================================
# 6. 人格核心与时间感知系统
# ==========================================

"""Local file safety scanner for QQ file messages.

This module intentionally avoids LLM calls. It combines deterministic checks,
optional ClamAV scanning, and conservative archive inspection.
"""

from .shared import *
from .shared import _get_http_session, _PIL_AVAILABLE, _PILImage

import mimetypes
import subprocess
import tarfile
import zipfile
import contextlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileScanResult:
    action: str = "safe"
    reason: str = "未发现本地文件风险。"
    path: str = ""
    name: str = ""
    size: int = 0
    sha256: str = ""
    file_type: str = ""
    detections: list[str] = field(default_factory=list)
    apk_info: dict = field(default_factory=dict)
    apk_review: dict = field(default_factory=dict)
    is_cached: bool = False

    def to_ad_result(self) -> dict:
        if self.action == "kick":
            return {
                "action": "kick",
                "kick": True,
                "reason": self.reason,
            }
        if self.action != "suspect":
            return {
                "action": "pass",
                "kick": False,
                "reason": self.reason,
            }
        return {
            "action": "suspect",
            "kick": False,
            "reason": self.reason,
        }


class LocalFileScanner:
    DANGEROUS_EXTS = {
        ".exe", ".scr", ".com", ".pif", ".msi", ".msp", ".bat", ".cmd",
        ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
        ".hta", ".reg", ".lnk", ".scf", ".url", ".chm", ".jar",
        ".app", ".dmg", ".pkg", ".iso", ".img", ".docm", ".xlsm", ".pptm",
        ".xlam", ".xll",
    }
    ARCHIVE_EXTS = {".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx"}
    DOUBLE_EXT_SAFE = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".txt", ".pdf",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3",
    }
    MAX_ARCHIVE_ENTRIES = 200
    MAX_ARCHIVE_UNCOMPRESSED = 150 * 1024 * 1024
    MAX_ARCHIVE_RATIO = 80
    APK_MIN_REASONABLE_SIZE = 512 * 1024
    SCAN_CACHE_VERSION = 2
    
    _scan_cache = None
    _scan_cache_path = ""

    @classmethod
    def _init_scan_cache(cls):
        if cls._scan_cache is not None:
            return
        cls._scan_cache_path = os.path.join(GlobalConfig.CACHE_DIR, "file_scan_cache.json")
        try:
            if os.path.exists(cls._scan_cache_path):
                with open(cls._scan_cache_path, "r", encoding="utf-8") as f:
                    cls._scan_cache = json.load(f)
            else:
                cls._scan_cache = {}
        except Exception:
            cls._scan_cache = {}

    @classmethod
    def get_cached_scan(cls, sha256: str) -> FileScanResult | None:
        if not sha256: return None
        cls._init_scan_cache()
        data = cls._scan_cache.get(sha256)
        if not data: return None
        if data.get("scan_cache_version") != cls.SCAN_CACHE_VERSION:
            return None
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(FileScanResult)}
        res = FileScanResult(**{k: v for k, v in data.items() if k in field_names})
        res.is_cached = True
        return res

    @classmethod
    def save_cached_scan(cls, result: FileScanResult):
        if not result or not result.sha256: return
        cls._init_scan_cache()
        import dataclasses
        data = dataclasses.asdict(result)
        data["is_cached"] = False
        data["scan_cache_version"] = cls.SCAN_CACHE_VERSION
        cls._scan_cache[result.sha256] = data
        try:
            with open(cls._scan_cache_path, "w", encoding="utf-8") as f:
                json.dump(cls._scan_cache, f, ensure_ascii=False)
        except Exception:
            pass

    @classmethod
    def clear_scan_cache(cls) -> int:
        cls._init_scan_cache()
        count = len(cls._scan_cache or {})
        cls._scan_cache = {}
        try:
            if cls._scan_cache_path:
                with open(cls._scan_cache_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False)
        except Exception as e:
            audit.log("WARN", "FILESCAN", f"清空文件扫描哈希缓存文件失败: {e}")
        return count

    @classmethod
    def scan_cache_dir(cls) -> str:
        path = os.path.join(GlobalConfig.CACHE_DIR, "file_scan")
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def _sanitize_name(cls, name: str) -> str:
        name = os.path.basename(str(name or "").strip()) or "qq_file"
        name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name)
        name = re.sub(r"\.\d+$", "", name)
        return name[:120] or "qq_file"

    @classmethod
    def _apk_display_name_from_filename(cls, name: str) -> str:
        stem = os.path.basename(str(name or "").strip())
        stem = re.sub(r"(?i)\.apk(?:\.\d+)?$", "", stem)
        stem = re.sub(r"[_\-]+", " ", stem).strip(" ._-\t")
        version_match = re.match(r"(.+?)\s+\d+(?:[.\-]\d+){1,}.*$", stem)
        if version_match and version_match.group(1).strip():
            stem = version_match.group(1).strip()
        stem = re.sub(r"\s+", " ", stem)
        if not stem or stem.lower() in ("qq_file", "file", "unknown"):
            return ""
        return stem[:40]

    @classmethod
    def _apk_app_name_looks_broken(cls, app_name: str, file_display_name: str = "") -> bool:
        name = re.sub(r"\s+", " ", str(app_name or "")).strip()
        if not name:
            return True
        compact = re.sub(r"\s+", "", name)
        file_compact = re.sub(r"\s+", "", str(file_display_name or ""))
        if file_compact and (compact in file_compact or file_compact in compact):
            return False
        if re.search(r"[\x00-\x1f\x7f\ufffd]", name):
            return True
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", name):
            return True
        cjk_count = len(re.findall(r"[\u4e00-\u9fff]", name))
        ascii_digit_count = len(re.findall(r"[A-Za-z0-9]", name))
        punctuation_count = len(re.findall(r"[^A-Za-z0-9\u4e00-\u9fff\s]", name))
        if len(compact) <= 2 and file_compact:
            return True
        if len(compact) <= 3 and cjk_count and ascii_digit_count:
            return True
        if punctuation_count >= max(2, len(compact) // 2):
            return True
        return False

    @classmethod
    def _select_apk_package(cls, packages: list[str]) -> str:
        sdk_prefixes = (
            "schemas.", "android.", "com.android.", "com.alipay.", "com.huawei.",
            "com.getui.", "com.xiaomi.", "com.sina.", "com.umeng.", "com.blankj.",
            "com.coloros.", "com.vivo.", "com.meizu.", "com.chuanglan.", "com.cmic.",
            "com.netease.", "com.ss.", "com.google.", "org.apache.", "com.baidu.",
            "cn.jiguang.", "cn.gravity.", "com.unionpay.", "com.jdpaysdk.",
            "com.eg.android.", "hk.alipay.", "com.jd.", "com.jingdong.",
            "com.bbk.", "com.oppo.", "com.tencent.", "com.lenovo.", "com.sec.",
            "org.simalliance.", "com.mdid.", "com.samsung.", "com.coolpad.",
            "com.heytap.", "com.qiku.", "freemme.", "oplus.", "com.nemu.",
        )
        bad_suffixes = (
            "Activity", "Service", "Provider", "Receiver", "Application",
            "Permission", "Callback", "Helper",
        )
        cleaned = []
        for package in packages or []:
            package = str(package or "").strip(".")
            if not package or package.count(".") < 1:
                continue
            if package.startswith(sdk_prefixes):
                continue
            if package not in cleaned:
                cleaned.append(package)
        for package in cleaned:
            parts = package.split(".")
            last = parts[-1]
            if any(last.endswith(suffix) for suffix in bad_suffixes):
                continue
            if any(part[:1].isupper() for part in parts):
                continue
            return package
        return cleaned[0] if cleaned else ""

    @classmethod
    def _sha256_file(cls, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def _sniff_file_type(cls, path: str, name: str) -> str:
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
            if head.startswith(b"MZ"):
                return "Windows PE executable"
            if head.startswith(b"\x7fELF"):
                return "ELF executable"
            if head.startswith(b"\xcf\xfa\xed\xfe") or head.startswith(b"\xfe\xed\xfa\xcf"):
                return "Mach-O executable"
            if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x07\x08"):
                return "ZIP archive"
            if head.startswith(b"%PDF"):
                return "PDF document"
            if head.startswith(b"\x89PNG"):
                return "PNG image"
            if head.startswith(b"\xff\xd8\xff"):
                return "JPEG image"
            if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
                return "GIF image"
        except Exception:
            pass
        guessed, _ = mimetypes.guess_type(name or path)
        return guessed or "unknown"

    @classmethod
    def _check_filename(cls, name: str, detections: list[str]):
        lower = str(name or "").lower().strip()
        suffixes = [s.lower() for s in Path(lower).suffixes]
        if not suffixes:
            return
        final_ext = suffixes[-1]
        if final_ext in cls.DANGEROUS_EXTS:
            detections.append(f"危险扩展名 {final_ext}")
        if len(suffixes) >= 2 and suffixes[-2] in cls.DOUBLE_EXT_SAFE and final_ext in cls.DANGEROUS_EXTS:
            detections.append(f"伪装双扩展名 {suffixes[-2]}{final_ext}")

    @classmethod
    def _filename_has_apk_suffix(cls, name: str) -> bool:
        lower = str(name or "").lower().strip()
        suffixes = [s.lower() for s in Path(lower).suffixes]
        if ".apk" in suffixes:
            return True
        return bool(re.search(r"\.apk(?:\.\d+)?(?:\s*)$", lower))

    @classmethod
    def _zip_looks_like_apk(cls, path: str) -> bool:
        try:
            if not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path) as zf:
                names = {str(n).replace("\\", "/") for n in zf.namelist()}
            if "AndroidManifest.xml" not in names:
                return False
            if any(n.endswith(".dex") for n in names):
                return True
            if any(n.startswith("res/") for n in names) and any(n.startswith("META-INF/") for n in names):
                return True
            return True
        except Exception:
            return False

    @classmethod
    def _check_magic(cls, path: str, name: str, file_type: str, detections: list[str]):
        lower_name = str(name or "").lower()
        lower_type = str(file_type or "").lower()
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except Exception:
            head = b""
        if head.startswith((b"MZ", b"\x7fELF")) or "executable" in lower_type:
            detections.append(f"文件头显示可执行文件: {file_type}")
            if not any(lower_name.endswith(ext) for ext in cls.DANGEROUS_EXTS):
                detections.append("文件内容和扩展名不一致，疑似伪装可执行文件")

    @classmethod
    def _is_apk(cls, path: str, name: str, file_type: str = "") -> bool:
        if not zipfile.is_zipfile(path):
            return False
        return cls._filename_has_apk_suffix(name or path) or cls._zip_looks_like_apk(path)

    @classmethod
    def _extract_strings_from_bytes(cls, data: bytes, limit: int = 160) -> list[str]:
        strings = []
        for encoding in ("utf-8", "utf-16le"):
            try:
                text = data.decode(encoding, errors="ignore")
                strings.extend(re.findall(r"[A-Za-z0-9_.\-\u4e00-\u9fff]{2,80}", text))
            except Exception:
                pass
        cleaned = []
        for s in strings:
            s = re.sub(r"\s+", " ", str(s)).strip()
            if s and s not in cleaned:
                cleaned.append(s)
            if len(cleaned) >= limit:
                break
        return cleaned

    @classmethod
    def _extract_apk_with_androguard(cls, path: str) -> dict:
        try:
            logging.getLogger("androguard").setLevel(logging.WARNING)
            logging.getLogger("androguard.core").setLevel(logging.WARNING)
            try:
                from loguru import logger as loguru_logger
                loguru_logger.disable("androguard")
            except Exception:
                pass
            from androguard.core.apk import APK

            with contextlib.redirect_stderr(io.StringIO()):
                apk = APK(path)
                icon_name = apk.get_app_icon()
                icon_b64 = ""
                icon_mime = "image/png"
                if icon_name:
                    suffix = Path(str(icon_name)).suffix.lower()
                    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
                        icon_bytes = apk.get_file(icon_name)
                        if icon_bytes:
                            icon_b64, icon_mime = cls._image_bytes_to_data_url(icon_bytes, icon_name)
                return {
                    "package": apk.get_package() or "",
                    "app_name": apk.get_app_name() or "",
                    "permissions": list(apk.get_permissions() or [])[:80],
                    "icon_path": icon_name or "",
                    "icon_data_url": icon_b64,
                    "icon_mime": icon_mime,
                    "source": "androguard",
                }
        except Exception:
            return {}

    @classmethod
    def _image_bytes_to_data_url(cls, data: bytes, name: str = "") -> tuple[str, str]:
        if not data:
            return "", "image/png"
        suffix = Path(str(name or "")).suffix.lower()
        head = data[:32]
        is_png = head.startswith(b"\x89PNG")
        is_jpeg = head.startswith(b"\xff\xd8\xff")
        is_webp = head.startswith(b"RIFF") and b"WEBP" in head[:16]
        if not (is_png or is_jpeg or is_webp):
            return "", "image/png"
        mime = "image/jpeg" if is_jpeg else "image/webp" if is_webp else "image/png"
        try:
            if _PIL_AVAILABLE:
                with _PILImage.open(io.BytesIO(data)) as img:
                    img.load()
                    img.thumbnail((512, 512))
                    if img.mode not in ("RGB", "L"):
                        bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                        if "A" in img.getbands():
                            bg.paste(img, mask=img.getchannel("A"))
                            img = bg
                        else:
                            img = img.convert("RGB")
                    out = io.BytesIO()
                    img.convert("RGB").save(out, format="PNG", optimize=True)
                return f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode('ascii')}", "image/png"
        except Exception:
            return "", "image/png"
        if is_webp:
            return "", "image/png"
        if not (is_png or is_jpeg or is_webp):
            return "", "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", mime

    @classmethod
    def _extract_apk_info(cls, path: str, name: str) -> dict:
        info = cls._extract_apk_with_androguard(path)
        file_display_name = cls._apk_display_name_from_filename(name)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                manifest_bytes = b""
                try:
                    manifest_bytes = zf.read("AndroidManifest.xml")
                except Exception:
                    pass
                strings = cls._extract_strings_from_bytes(manifest_bytes)
                packages = [
                    s for s in strings
                    if re.fullmatch(r"[A-Za-z]\w*(?:\.[A-Za-z]\w*){1,8}", s)
                    and not s.startswith(("android.", "com.android."))
                ]
                icon_candidates = [
                    n for n in names
                    if re.search(r"res/(mipmap|drawable)[^/]*/.*(ic_launcher|launcher|icon).*\.(png|webp|jpg|jpeg)$", n, re.I)
                ]
                if not icon_candidates:
                    icon_candidates = [
                        n for n in names
                        if re.search(r"res/(mipmap|drawable)[^/]*/.*\.(png|webp|jpg|jpeg)$", n, re.I)
                    ]
                icon_data_url = info.get("icon_data_url", "") if info else ""
                icon_path = info.get("icon_path", "") if info else ""
                if not icon_data_url and icon_candidates:
                    icon_path = sorted(icon_candidates, key=lambda x: ("xxxhdpi" not in x, "xxhdpi" not in x, len(x)))[0]
                    try:
                        icon_data_url, icon_mime = cls._image_bytes_to_data_url(zf.read(icon_path), icon_path)
                    except Exception:
                        icon_data_url, icon_mime = "", "image/png"
                else:
                    icon_mime = info.get("icon_mime", "image/png") if info else "image/png"
                app_name = info.get("app_name", "") if info else ""
                if not app_name:
                    label_candidates = [
                        s for s in strings
                        if not re.fullmatch(r"[A-Za-z]\w*(?:\.[A-Za-z]\w*){1,8}", s)
                        and not s.startswith(("android", "http", "res/"))
                        and not re.fullmatch(r"[A-Z0-9_]+", s)
                        and s not in ("internalVersionCode", "internalVersionName", "compileSdkVersion", "compileSdkVersionCodename")
                        and 2 <= len(s) <= 24
                    ]
                    chinese_labels = [s for s in label_candidates if re.search(r"[\u4e00-\u9fff]", s)]
                    title_labels = [s for s in label_candidates if s.isalpha() and s.istitle() and len(s) >= 4]
                    
                    app_name = next(iter(chinese_labels), "")
                    if not app_name:
                        app_name = next(iter(title_labels), "")
                
                package = info.get("package", "") if info else ""
                if not package and packages:
                    package = cls._select_apk_package(packages)
                if not app_name and package:
                    parts = package.split('.')
                    if len(parts) >= 2:
                        app_name = parts[1].capitalize()
                    else:
                        app_name = parts[0].capitalize()
                if cls._apk_app_name_looks_broken(app_name, file_display_name):
                    fallback_name = file_display_name or app_name
                    if fallback_name and fallback_name != app_name:
                        audit.log(
                            "WARN",
                            "FILESCAN",
                            f"APK应用名提取疑似异常，改用文件名兜底: extracted={app_name or '空'} fallback={fallback_name}",
                        )
                    app_name = fallback_name
                permissions = info.get("permissions", []) if info else []
                if not permissions:
                    permissions = [s for s in strings if s.startswith("android.permission.")][:80]
                return {
                    "is_apk": True,
                    "file_name": name,
                    "package": package,
                    "app_name": app_name,
                    "permissions": permissions[:80],
                    "manifest_strings": strings[:120],
                    "icon_path": icon_path,
                    "icon_data_url": icon_data_url,
                    "icon_mime": icon_mime,
                    "entry_count": len(names),
                    "source": info.get("source", "zip-fallback") if info else "zip-fallback",
                }
        except Exception as e:
            app_name = info.get("app_name", "") if info else ""
            if cls._apk_app_name_looks_broken(app_name, file_display_name):
                app_name = file_display_name or app_name
            return {
                "is_apk": True,
                "file_name": name,
                "package": info.get("package", "") if info else "",
                "app_name": app_name,
                "permissions": info.get("permissions", []) if info else [],
                "icon_path": info.get("icon_path", "") if info else "",
                "icon_data_url": info.get("icon_data_url", "") if info else "",
                "source": "error",
                "error": str(e),
            }

    @classmethod
    def _check_zip_archive(cls, path: str, detections: list[str], is_apk: bool = False):
        try:
            if not zipfile.is_zipfile(path):
                return
            total = 0
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                max_entries = cls.MAX_ARCHIVE_ENTRIES * 60 if is_apk else cls.MAX_ARCHIVE_ENTRIES
                if len(infos) > max_entries:
                    detections.append(f"压缩包文件数量过多: {len(infos)}")
                for info in infos[: max_entries + 1]:
                    member = info.filename or ""
                    normalized = member.replace("\\", "/")
                    if normalized.startswith("/") or "/../" in f"/{normalized}" or normalized.startswith("../"):
                        detections.append(f"压缩包存在路径穿越文件: {member[:120]}")
                    if info.flag_bits & 0x1:
                        detections.append(f"压缩包包含加密文件: {member[:120]}")
                    total += int(info.file_size or 0)
                    if info.compress_size and info.file_size / max(1, info.compress_size) > cls.MAX_ARCHIVE_RATIO:
                        if not is_apk or info.file_size > 10 * 1024 * 1024:
                            detections.append(f"压缩包高压缩比文件: {member[:120]}")
                    if not (is_apk and Path(member.lower()).suffix == ".jar"):
                        cls._check_filename(member, detections)
                max_uncompressed = cls.MAX_ARCHIVE_UNCOMPRESSED * 10 if is_apk else cls.MAX_ARCHIVE_UNCOMPRESSED
                if total > max_uncompressed:
                    detections.append(f"压缩包解压后体积过大: {total // 1024 // 1024}MB")
        except Exception as e:
            detections.append(f"压缩包检查异常: {e}")

    @classmethod
    def _check_tar_archive(cls, path: str, detections: list[str]):
        try:
            if not tarfile.is_tarfile(path):
                return
            total = 0
            with tarfile.open(path) as tf:
                members = tf.getmembers()
                if len(members) > cls.MAX_ARCHIVE_ENTRIES:
                    detections.append(f"tar归档文件数量过多: {len(members)}")
                for info in members[: cls.MAX_ARCHIVE_ENTRIES + 1]:
                    member = info.name or ""
                    normalized = member.replace("\\", "/")
                    if normalized.startswith("/") or "/../" in f"/{normalized}" or normalized.startswith("../"):
                        detections.append(f"tar归档存在路径穿越文件: {member[:120]}")
                    total += int(info.size or 0)
                    cls._check_filename(member, detections)
                if total > cls.MAX_ARCHIVE_UNCOMPRESSED:
                    detections.append(f"tar归档解包后体积过大: {total // 1024 // 1024}MB")
        except Exception as e:
            detections.append(f"tar归档检查异常: {e}")

    @classmethod
    def _run_clamav(cls, path: str, detections: list[str]) -> str:
        if not GlobalConfig.FILE_SCAN_CLAMAV_ENABLED:
            return "disabled"
        configured = str(GlobalConfig.FILE_SCAN_CLAMSCAN_PATH or "").strip()
        scanner = configured if configured else shutil.which("clamscan")
        if not scanner:
            return "missing"
        try:
            proc = subprocess.run(
                [
                    scanner,
                    "--no-summary",
                    "--infected",
                    "--max-filesize=5000M",
                    "--max-scansize=5000M",
                    "--max-scantime=15000",
                    path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(18, max(5, int(GlobalConfig.FILE_SCAN_TIMEOUT))),
            )
            output = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 1:
                detections.append(f"ClamAV 命中: {output or 'infected'}")
                return "infected"
            if proc.returncode == 0:
                return "clean"
            audit.log("WARN", "FILESCAN", f"ClamAV 扫描异常 exit={proc.returncode}: {output}")
            return "error"
        except subprocess.TimeoutExpired:
            detections.append("ClamAV 扫描超时")
            return "timeout"
        except Exception as e:
            audit.log("WARN", "FILESCAN", f"ClamAV 调用失败: {e}")
            return "error"

    @classmethod
    def scan_path(cls, path: str, name: str = "") -> FileScanResult:
        name = cls._sanitize_name(name or os.path.basename(path))
        detections: list[str] = []
        try:
            size = os.path.getsize(path)
            max_bytes = max(1, int(GlobalConfig.FILE_SCAN_MAX_MB)) * 1024 * 1024
            if size > max_bytes:
                detections.append(f"文件超过扫描上限: {size // 1024 // 1024}MB > {GlobalConfig.FILE_SCAN_MAX_MB}MB")
            file_type = cls._sniff_file_type(path, name)
            sha256 = cls._sha256_file(path)
            is_apk = cls._is_apk(path, name, file_type)
            if is_apk:
                audit.log(
                    "INFO", "FILESCAN",
                    f"APK结构识别命中: name={name} suffix_apk={cls._filename_has_apk_suffix(name)} type={file_type}"
                )
            cls._check_filename(name, detections)
            cls._check_magic(path, name, file_type, detections)
            if size <= max_bytes:
                cls._check_zip_archive(path, detections, is_apk=is_apk)
                cls._check_tar_archive(path, detections)
                clamav_status = cls._run_clamav(path, detections)
            else:
                clamav_status = "skipped_oversize"
            apk_info = cls._extract_apk_info(path, name) if is_apk else {}
            if is_apk:
                detections = [
                    d for d in detections
                    if d not in ("危险扩展名 .apk", "危险扩展名 .1")
                    and not d.startswith("伪装双扩展名 .apk.")
                ]

            if detections:
                reason = (
                    f"文件安全扫描命中风险：{'; '.join(dict.fromkeys(detections))}。"
                    f" 文件名={name} 类型={file_type} SHA256={sha256[:16]}..."
                )
                action = "suspect"
            else:
                reason = (
                    f"文件安全扫描通过：文件名={name} 类型={file_type} "
                    f"大小={size // 1024}KB ClamAV={clamav_status} SHA256={sha256[:16]}..."
                )
                action = "safe"
            return FileScanResult(
                action=action,
                reason=reason,
                path=path,
                name=name,
                size=size,
                sha256=sha256,
                file_type=file_type,
                detections=list(dict.fromkeys(detections)),
                apk_info=apk_info,
            )
        except Exception as e:
            return FileScanResult(
                action="suspect",
                reason=f"文件安全扫描失败，按疑似风险处理：{type(e).__name__}: {e}",
                path=path,
                name=name,
                detections=[str(e)],
            )

    @classmethod
    def _format_size(cls, size: int | str | None) -> str:
        try:
            size_int = int(size) # type: ignore
            if size_int < 0:
                return "unknown"
            if size_int < 1024:
                return f"{size_int}B"
            elif size_int < 1024 * 1024:
                return f"{size_int / 1024:.2f}KB"
            elif size_int < 1024 * 1024 * 1024:
                return f"{size_int / (1024 * 1024):.2f}MB"
            return f"{size_int / (1024 * 1024 * 1024):.2f}GB"
        except (TypeError, ValueError):
            return "unknown"

    @classmethod
    async def _wait_for_local_file(cls, path: str, size_hint: int | None, timeout: int = 45) -> bool:
        start_time = time.time()
        last_size = -1
        stable_count = 0
        while time.time() - start_time < timeout:
            if not os.path.exists(path):
                await asyncio.sleep(1)
                continue
            
            try:
                current_size = os.path.getsize(path)
            except Exception:
                current_size = -1
                
            if size_hint and current_size >= int(size_hint):
                await asyncio.sleep(0.5)
                return True
                
            if not size_hint and current_size > 0:
                if current_size == last_size:
                    stable_count += 1
                    if stable_count >= 3:
                        return True
                else:
                    stable_count = 0
                    last_size = current_size
            
            await asyncio.sleep(1)
            
        return False

    @classmethod
    async def download_and_scan(cls, url: str, name: str = "", size_hint: int | None = None) -> FileScanResult:
        if not GlobalConfig.FILE_SCAN_ENABLED:
            return FileScanResult(action="safe", reason="文件安全扫描未启用。")
        safe_name = cls._sanitize_name(name)
        is_apk_name = cls._filename_has_apk_suffix(safe_name)
        max_mb = int(GlobalConfig.FILE_SCAN_APK_MAX_MB if is_apk_name else GlobalConfig.FILE_SCAN_MAX_MB)
        max_bytes = max(1, max_mb) * 1024 * 1024
        if size_hint and int(size_hint) > max_bytes:
            return FileScanResult(
                action="suspect",
                name=safe_name,
                size=int(size_hint),
                reason=f"文件超过扫描上限：{int(size_hint) // 1024 // 1024}MB > {max_mb}MB",
                detections=["文件超过扫描上限"],
            )
        if not re.match(r"https?://", str(url or ""), re.I):
            local_path = str(url or "")
            timeout = max(10, int(GlobalConfig.FILE_SCAN_TIMEOUT))
            if await cls._wait_for_local_file(local_path, size_hint, timeout=timeout):
                try:
                    final_size = os.path.getsize(local_path)
                except Exception:
                    final_size = 0
                audit.log("INFO", "FILESCAN", f"本地文件落盘完成: name={safe_name} size={cls._format_size(final_size)} expected={cls._format_size(size_hint)}")
                
                digest = ""
                try:
                    digest = cls._sha256_file(local_path)
                except Exception:
                    pass
                if digest:
                    cached_result = cls.get_cached_scan(digest)
                    if cached_result:
                        if is_apk_name and cached_result.size < cls.APK_MIN_REASONABLE_SIZE and not (cached_result.apk_info and cached_result.apk_info.get("is_apk")):
                            audit.log("WARN", "FILESCAN", f"忽略疑似错误页APK缓存: name={safe_name} cached_size={cached_result.size}B")
                        else:
                            audit.log("INFO", "FILESCAN", f"命中全局文件扫描缓存: action={cached_result.action} name={safe_name}")
                            try:
                                if os.path.exists(local_path): os.remove(local_path)
                            except Exception: pass
                            cached_result.name = safe_name
                            return cached_result
                
                result = await asyncio.to_thread(cls.scan_path, local_path, safe_name)
                cls.save_cached_scan(result)
                try:
                    if os.path.exists(local_path):
                        os.remove(local_path)
                except Exception:
                    pass
                return result
            return FileScanResult(
                action="suspect",
                name=safe_name,
                reason="文件消息缺少可访问 URL 或本地路径，或等待文件写入超时，无法完成安全扫描。",
                detections=["无法获取或等待本地文件"],
            )
        session = await _get_http_session()
        local_path = ""
        try:
            data = await cls._download_file_bytes(session, url, safe_name, max_bytes)
            if isinstance(data, FileScanResult):
                return data
                
            digest = hashlib.sha256(data).hexdigest()
            cached_result = cls.get_cached_scan(digest)
            if cached_result:
                if is_apk_name and cached_result.size < cls.APK_MIN_REASONABLE_SIZE and not (cached_result.apk_info and cached_result.apk_info.get("is_apk")):
                    audit.log("WARN", "FILESCAN", f"忽略疑似错误页APK缓存: name={safe_name} cached_size={cached_result.size}B")
                else:
                    audit.log("INFO", "FILESCAN", f"命中全局文件扫描缓存: action={cached_result.action} name={safe_name}")
                    cached_result.name = safe_name
                    return cached_result
                
            ext = Path(safe_name).suffix[:16]
            local_path = os.path.join(cls.scan_cache_dir(), f"{digest}{ext}")
            with open(local_path, "wb") as f:
                f.write(data)
            result = await asyncio.to_thread(cls.scan_path, local_path, safe_name)
            if (
                is_apk_name
                and result.size
                and result.size < cls.APK_MIN_REASONABLE_SIZE
                and not (result.apk_info and result.apk_info.get("is_apk"))
            ):
                audit.log(
                    "WARN", "FILESCAN",
                    f"APK下载结果过小且不是有效APK，等待后重试下载: name={safe_name} size={result.size}B"
                )
                for attempt in range(2):
                    await asyncio.sleep(1.5 + attempt)
                    retry_data = await cls._download_file_bytes(session, url, safe_name, max_bytes)
                    if isinstance(retry_data, FileScanResult):
                        continue
                    retry_digest = hashlib.sha256(retry_data).hexdigest()
                    retry_path = os.path.join(cls.scan_cache_dir(), f"{retry_digest}{ext}")
                    with open(retry_path, "wb") as f:
                        f.write(retry_data)
                    retry_result = await asyncio.to_thread(cls.scan_path, retry_path, safe_name)
                    if retry_result.size > result.size or (retry_result.apk_info and retry_result.apk_info.get("is_apk")):
                        result = retry_result
                    if result.apk_info and result.apk_info.get("is_apk"):
                        break
                if not (result.apk_info and result.apk_info.get("is_apk")):
                    result.action = "suspect"
                    result.reason = (
                        f"APK文件下载后大小异常且不是有效APK结构，疑似QQ临时链接未返回真实文件。"
                        f" 实际大小={result.size}B。"
                    )
                    result.detections = list(dict.fromkeys((result.detections or []) + ["APK下载内容异常"]))
            cls.save_cached_scan(result)
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
                if 'retry_path' in locals() and os.path.exists(retry_path):
                    os.remove(retry_path)
            except Exception:
                pass
            return result
        except Exception as e:
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass
            return FileScanResult(
                action="suspect",
                name=safe_name,
                path=local_path,
                reason=f"文件下载/扫描异常，按疑似风险处理：{type(e).__name__}: {e}",
                detections=[str(e)],
            )

    @classmethod
    async def _download_file_bytes(cls, session, url: str, safe_name: str, max_bytes: int):
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=max(10, int(GlobalConfig.FILE_SCAN_TIMEOUT)))) as resp:
            if resp.status != 200:
                return FileScanResult(
                    action="suspect",
                    name=safe_name,
                    reason=f"文件下载失败 HTTP {resp.status}，无法完成安全扫描。",
                    detections=[f"下载失败 HTTP {resp.status}"],
                )
            expected = 0
            try:
                expected = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                expected = 0
            chunks = []
            total = 0
            async for chunk in resp.content.iter_chunked(512 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return FileScanResult(
                        action="suspect",
                        name=safe_name,
                        size=total,
                        reason=f"文件超过扫描上限：>{max_bytes // 1024 // 1024}MB",
                        detections=["文件超过扫描上限"],
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            if expected and len(data) != expected:
                return FileScanResult(
                    action="suspect",
                    name=safe_name,
                    size=len(data),
                    reason=f"文件下载不完整：实际{cls._format_size(len(data))} / 预期{cls._format_size(expected)}",
                    detections=["文件下载不完整"],
                )
            content_type = resp.headers.get("Content-Type", "")
            disposition = resp.headers.get("Content-Disposition", "")
            audit.log(
                "INFO", "FILESCAN",
                f"文件下载完成: name={safe_name} size={cls._format_size(len(data))} "
                f"expected={cls._format_size(expected)} content_type={content_type or 'unknown'} "
                f"disposition={disposition[:120] or 'none'}"
            )
            return data

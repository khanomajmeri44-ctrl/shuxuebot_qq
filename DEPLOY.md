# Shuxue Bot 配置与部署说明

## 1. 配置文件位置

默认配置文件在项目目录：

```text
C:\Users\duo0621\Desktop\AI\shuxue_bot\config.json
```

也可以用环境变量指定其他位置：

```powershell
$env:SHUXUE_CONFIG="D:\shuxue\config.json"
python -m shuxue_bot.app
```

## 2. 必填 API 配置

打开 `config.json`，至少填写：

```json
{
  "bot": {
    "admin_uid": "你的QQ号"
  },
  "napcat": {
    "ws_url": "ws://你的NapCat地址:端口?access_token=你的token"
  },
  "api": {
    "deepseek_key": "你的DeepSeek API Key",
    "dashscope_key": "你的DashScope API Key"
  }
}
```

`admin_uid` 是管理员 QQ 号，只有这个账号能使用 `/status`、`/fav`、`/ad_kick_on` 等管理命令。

## 3. 可选模型配置

默认模型配置如下，可按需修改：

```json
{
  "models": {
    "deepseek": "deepseek-v4-pro",
    "deepseek_temperature": 1.15,
    "max_tokens": 1024,
    "vision": "qwen3.6-plus",
    "judge": "qwen3.5-flash",
    "ad_review": "qwen3.6-plus",
    "apk_review": "qwen3.6-plus",
    "sticker_name": "qwen3.6-plus",
    "embedding": "text-embedding-v3"
  }
}
```

`judge` 用于群聊是否回复和广告初筛，可以使用较便宜的 `qwen3.5-flash`。`ad_review` 是广告踢人高级复审模型，建议保留更强模型。`api.dashscope_enable_thinking` 为 `false` 时不开启深度思考。

## 4. 路径配置

如果 `paths` 里的路径留空，程序会自动使用项目目录下的默认路径：

- `shuxue_v10_core.db`
- `logs`
- `emotes`
- `cache`
- `chroma_global_brain`

需要迁移旧数据时，可以填写绝对路径，例如：

```json
{
  "paths": {
    "base_dir": "C:\\Users\\duo0621\\Desktop\\AI",
    "db_path": "C:\\Users\\duo0621\\Desktop\\AI\\shuxue_v10_core.db"
  }
}
```

## 5. 环境变量覆盖

环境变量优先级高于 `config.json`。常用项：

```powershell
$env:DEEPSEEK_KEY="..."
$env:DASHSCOPE_KEY="..."
$env:ADMIN_UID="3598344975"
$env:NAPCAT_WS="ws://127.0.0.1:6001?access_token=..."
$env:CONSOLE_PORT="7860"
```

## Web 控制台登录

`config.json` 中配置控制台账号密码：

```json
{
  "console": {
    "port": 7860,
    "username": "dcloud",
    "password": "WangQD4567",
    "session_secret": "请换成一串随机长字符串"
  }
}
```

访问地址：

```text
http://localhost:7860/console/
```

未登录时会自动跳转到 `/console/login`，所有 `/console/api/*` 接口也会要求登录。

## 6. Python 依赖

基础依赖建议安装：

```powershell
pip install aiohttp aiosqlite websockets tenacity dashscope pillow rapidocr_onnxruntime opencv-python-headless
```

`rapidocr_onnxruntime` 用于普通图片 OCR。`opencv-python-headless` 用于视频消息随机抽帧；如果缺失，视频消息会记录错误并跳过抽帧。

APK 文件会额外提取应用名、包名和图标，并单独调用 `models.apk_review` 指定的多模态模型判断是否像正规游戏/软件。基础提取不需要额外依赖；如果希望更准确解析 AndroidManifest，可选安装：

```powershell
pip install androguard
```

文件安全扫描不调用大模型，依赖 Python 标准库完成扩展名、文件头和压缩包检查；如需病毒库扫描，另外安装 ClamAV：

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install clamav clamav-daemon
sudo freshclam
```

Windows 可安装 ClamAV 官方包，并把 `clamscan.exe` 加入 PATH；或者在 `config.json` 中填写绝对路径：

```json
{
  "security": {
    "file_scan_enabled": true,
    "file_scan_max_mb": 50,
    "file_scan_apk_max_mb": 150,
    "file_scan_timeout": 45,
    "file_scan_clamav_enabled": true,
    "file_scan_clamscan_path": "C:\\ClamAV\\clamscan.exe"
  }
}
```

如果未安装 ClamAV，机器人仍会执行本地规则扫描，但日志会显示 ClamAV 缺失。

## 7. 启动

从项目父目录启动包：

```powershell
cd C:\Users\duo0621\Desktop\AI
python -m shuxue_bot.app
```

如果在 `shuxue_bot` 目录内启动，需要用父目录作为 Python 包路径。

## 8. 管理命令

群聊主动社交：

```text
/social_on
/social_off
/social
```

广告自动踢人：

```text
/ad_kick_on
/ad_kick_off
/ad_kick_status
```

广告踢人会先快速初审，再高级模型复审，复审通过后撤回消息并踢人。

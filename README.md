# 淑雪 Bot (Shuxue Bot)

基于 DeepSeek + NapCat 的 QQ 聊天机器人，扮演 duo0621 的妹妹「淑雪」。

## 功能

- **角色扮演聊天** — DeepSeek 驱动，好感度系统，动态人格
- **群聊智能判断** — 自动判断是否需要回复，不刷屏
- **广告审核** — 两阶段审核（初筛 + 高级复审），支持图片/OCR 识别
- **文件安全扫描** — 本地规则 + ClamAV 病毒库 + APK 分析
- **多模态理解** — 图片识别、视频抽帧、语音转文字 (ASR)
- **向量记忆** — ChromaDB 长期记忆，跨会话召回
- **主动社交** — 心跳式主动私聊/群内冒泡，夜间自我反思
- **表情包系统** — 本地表情包管理，GIF 压缩
- **Web 控制台** — 浏览器端查看/修改 AI 配置

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

可选依赖（按需安装）：

```bash
# 图片理解 + 向量记忆 + Web 控制台
pip install dashscope chromadb flask pillow

# 图片 OCR
pip install rapidocr_onnxruntime

# 视频抽帧
pip install opencv-python-headless

# APK 深度解析
pip install androguard
```

### 2. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，至少填写：

- `bot.admin_uid` — 你的 QQ 号
- `napcat.ws_url` — NapCat WebSocket 地址
- `api.deepseek_key` — DeepSeek API Key
- `api.dashscope_key` — DashScope API Key（图片/OCR/向量记忆需要）

也可以用环境变量覆盖（优先级更高）：

```bash
export DEEPSEEK_KEY="..."
export DASHSCOPE_KEY="..."
export ADMIN_UID="你的QQ号"
export NAPCAT_WS="ws://127.0.0.1:6001"
```

### 3. 启动

```bash
python -m shuxue_bot.app
```

### 4. Web 控制台

启动后访问 `http://localhost:7860/console/`，可在浏览器中调整 AI 模型参数。

## 项目结构

```
shuxue_bot/
├── app.py              # 入口
├── host.py             # WebSocket 连接、消息调度
├── brain.py            # AI 推理、广告审核、多模态
├── memory.py           # SQLite + ChromaDB 记忆层
├── commands.py         # 管理员指令
├── scheduler.py        # 主动社交调度
├── personality.py      # 人格提示词
├── prompts.py          # 模型提示词模板
├── file_scanner.py     # 文件安全扫描
├── assets.py           # 表情包资源管理
├── console.py          # Web 控制台
├── shared.py           # 全局配置与工具
├── config.example.json # 配置模板
├── requirements.txt    # Python 依赖
└── DEPLOY.md           # 详细部署文档
```

## 管理命令

群聊中使用 `/shuxuebot <指令>`，私聊可省略前缀：

| 指令 | 说明 |
|------|------|
| `help` / `commands` | 查看所有指令 |
| `status` | 查看运行状态 |
| `clear` | 清空当前会话上下文 |
| `fav <QQ号> <数值>` | 调整好感度 |
| `social_on [群号]` | 开启主动群聊 |
| `social_off [群号]` | 关闭主动群聊 |
| `ad_kick_on [群号]` | 开启广告自动踢人 |
| `ad_kick_off [群号]` | 关闭广告自动踢人 |
| `file_hash_clear` | 清除文件扫描缓存 |

## 部署

详见 [DEPLOY.md](DEPLOY.md)

## 依赖

- Python 3.11+
- [NapCat](https://github.com/NapNeko/NapCatQQ) QQ 协议实现

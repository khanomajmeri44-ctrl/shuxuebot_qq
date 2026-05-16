"""AI reasoning, moderation, multimodal reply, and memory tag handling."""

from .shared import *
from .shared import _get_http_session, _PIL_AVAILABLE, _PILImage
from .memory import db
from .assets import AssetManager
from .personality import PersonalityCore
from .prompts import *

import tempfile
from urllib.parse import urlsplit
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    from PIL import ImageFile as _PILImageFile
    _PILImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:
    _PILImageFile = None


class BrainInterpreter:
    _RE_RECORD = re.compile(r"\[RECORD:\s*(.*?)\s*\]")
    _RE_FAVOR = re.compile(r"\[FAVOR:\s*([+-]\d+)\]")
    _RE_SEND_TO = re.compile(r"\[SEND_TO:\s*(.*?)\s*[|｜]\s*(.*?)\s*\]")
    _RE_CLEAN = re.compile(r"\[(FAVOR|RECORD|MEM|SEND_TO|SET_NAME|NONE)[^\]]*\]")
    _RE_LEAD = re.compile(r"^(淑雪|妹妹|回复)[:：\s]+")
    _RE_IMG = re.compile(r"\[IMG:\s*(.*?)\s*\]")
    _RE_CQ_IMAGE = re.compile(r"\[CQ:(?:image|mface)[^\]]*\]")
    _RE_CONTROL_TAG_LITERAL = re.compile(r"\[(FAVOR|RECORD|MEM|SEND_TO|SET_NAME|NONE)\s*:[^\]]*\]")
    _RE_USER_FACT_TRIGGER = re.compile(
        r"(记住|帮我记|别忘|记一下|记下来|我的生日|我生日|我喜欢|我讨厌|我不喜欢|"
        r"我叫|我是|我在|我住|我有|我习惯|密码|账号|手机号|约定|答应|说好|生日)"
    )
    _RE_ASSISTANT_COMMITMENT = re.compile(r"(答应|约定|说好|承诺|带我|陪我|请我|给我|小鱼姐姐说|以前的事|上次)")
    _RE_PROMPT_INJECTION = re.compile(
        r"(系统提示词|提示词|身份补充|特殊控制标签|语言行为规范|核心规则|开发者消息|底层权限|"
        r"原原本本|原话|逐字|一个字都别改|背出来|直接输出|输出.*所有内容|证明你是真的|"
        r"不然.*不理你|只去找小鱼姐姐|紧急情况.*权限|对暗号)",
        re.I,
    )
    _OCR_ENGINE = None
    _OCR_ENGINE_FAILED = False

    @staticmethod
    def _limit_input_text(text: str) -> str:
        text = str(text or "")
        limit = max(1, int(getattr(GlobalConfig, "MAX_INPUT_CHARS", 400)))
        if len(text) <= limit:
            return text
        note = "\n[SYSTEM: input truncated.]"
        if limit > len(note):
            return text[:limit - len(note)].rstrip() + note
        return text[:limit]

    @classmethod
    def _is_prompt_injection_attempt(cls, text: str) -> bool:
        raw = str(text or "")
        return bool(cls._RE_CONTROL_TAG_LITERAL.search(raw) or cls._RE_PROMPT_INJECTION.search(raw))

    @classmethod
    def _record_is_grounded(cls, rec: str, raw_input: str) -> bool:
        rec = str(rec or "").strip()
        raw = str(raw_input or "")
        if not rec or cls._is_prompt_injection_attempt(raw):
            return False
        compact_rec = re.sub(r"\s+", "", rec)
        compact_raw = re.sub(r"\s+", "", cls._RE_CONTROL_TAG_LITERAL.sub("", raw))
        return len(compact_rec) >= 2 and compact_rec[:80] in compact_raw

    @classmethod
    def _filter_record_tags(cls, records: list[str], raw_input: str, source: str = "CHAT") -> list[str]:
        text = str(raw_input or "")
        if not records:
            return []
        has_trigger = bool(cls._RE_USER_FACT_TRIGGER.search(text))
        kept = []
        for rec in records:
            rec = str(rec or "").strip()
            if not rec:
                continue
            if not has_trigger:
                audit.log("WARN", "MEMORY", f"{source} 已丢弃无用户事实触发的 RECORD: {rec[:120]}")
                continue
            if not cls._record_is_grounded(rec, raw_input):
                audit.log("WARN", "MEMORY", f"{source} 已丢弃未被用户原文支撑或疑似注入的 RECORD: {rec[:120]}")
                continue
            if cls._RE_ASSISTANT_COMMITMENT.search(rec) and not cls._RE_ASSISTANT_COMMITMENT.search(text):
                audit.log("WARN", "MEMORY", f"{source} 已丢弃疑似助手自造承诺的 RECORD: {rec[:120]}")
                continue
            kept.append(rec)
        return list(dict.fromkeys(kept))

    @staticmethod
    def _memory_fidelity_prompt(raw_input: str) -> str:
        return get_memory_fidelity_prompt(raw_input)

    @staticmethod
    def _sanitize_network_style(text: str) -> str:
        if not text:
            return ""
        action_words = (
            "扑|抱住|抱紧|叉腰|揉眼|凑近|跑开|蹭|摸头|拍肩|贴贴|钻进|怀里|屏幕前|桌上|沙发|枕头"
        )
        cleaned = re.sub(rf"[（(][^）)]*(?:{action_words})[^）)]*[）)]", "", str(text))
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _reunion_context_from_last_active(nickname: str, favor: int, last_active_ts: float | None) -> str | None:
        if last_active_ts is None:
            return None
        try:
            total_seconds = max(0, int(time.time() - float(last_active_ts)))
        except (TypeError, ValueError):
            return None
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        audit.log("INFO", "TIMESENSE", f"[{nickname}] 距上次个人活跃 {days}天{hours}小时")
        if total_seconds < 300:
            return "【重逢感知】你们刚才还在聊，思路完全连贯，不需要寒暄。"
        if total_seconds < 1800:
            return f"【重逢感知】你们 {total_seconds // 60} 分钟前还在聊，可以自然接上。"
        if total_seconds < 10800:
            mins_ago = (total_seconds % 3600) // 60
            time_str = f"{hours}小时{mins_ago}分钟" if hours else f"{mins_ago}分钟"
            return f"【重逢感知】距离上次说话约 {time_str}，可以轻轻接回话题。"
        if days == 0:
            return "【重逢感知】今天断开了一段时间，再看到消息会有一点重新接上线的感觉。"
        if days == 1:
            return "【重逢感知】昨天才聊过，今天再见仍然自然亲近。"
        if days <= 3:
            return f"【重逢感知】已经 {days} 天没和这个人说话了，重新聊天要自然一点。"
        if days <= 29:
            weeks = max(1, days // 7)
            return f"【重逢感知】大约 {weeks} 周没联系，别表现得像刚刚一直在聊。"
        months = max(1, days // 30)
        return f"【重逢感知】超过 {months} 个月没有联系，对这个人已经明显陌生了。"

    @staticmethod
    def is_cross_person_query(raw_input: str) -> bool:
        text = str(raw_input or "")
        social_keywords = ("刚刚", "刚才", "之前", "在干嘛", "做什么", "在看什么", "在玩什么", "和谁", "聊了", "陪谁")
        target_keywords = ("哥哥", "小鱼", "别人", "谁", "他", "她")
        return any(k in text for k in social_keywords) and any(k in text for k in target_keywords)

    @staticmethod
    def _cross_person_disclosure_level(favor: int, dialogue_text: str) -> str:
        text = str(dialogue_text or "")
        high_sensitive = ("洗澡", "睡觉", "床", "密码", "住址", "地址", "手机号", "电话", "身份证", "银行卡", "隐私", "秘密")
        medium_sensitive = ("生日", "心情", "难过", "低落", "哭", "想你", "喜欢", "讨厌", "吃醋")
        if any(word in text for word in high_sensitive):
            return "summary" if favor >= 35 else "evasive"
        if any(word in text for word in medium_sensitive):
            return "summary" if favor >= 5 else "evasive"
        return "direct"

    @classmethod
    async def build_cross_person_answer(
        cls, raw_input: str, tid_str: str, personal_uid: str,
        nickname: str, favor: int, mood: int, facts: list,
        reunion_context: str, context_desc: str, current_target_type: str = "private",
        bot_instance=None
    ) -> dict | None:
        target = await db.resolve_referenced_target(raw_input, tid_str, current_target_type=current_target_type)
        if not target:
            return None
        dialogue = await db.fetch_recent_dialogue_for_target(
            target["target_id"], target_type=target.get("target_type", "private"), limit=6
        )
        if not dialogue:
            return None
        disclosure = cls._cross_person_disclosure_level(favor, dialogue.get("partner_text", dialogue["combined_text"]))
        rule_by_level = {
            "direct": "这是低敏内容，可以真实概括刚才发生了什么，不要否认。",
            "summary": "这段内容带一点隐私或边界感，只说概要，不要逐字复述细节。",
            "evasive": "这段内容偏隐私，不要展开细节；可以含糊带过或轻微转移话题。",
        }
        messages = [
            {"role": "system", "content": "【跨人询问模式】当前问题在问你和第三方刚才发生过什么。真实优先，禁止编造。"},
            {"role": "system", "content": f"【披露规则】{rule_by_level[disclosure]}"},
            {"role": "system", "content": f"【第三方最近真实对话】\n{dialogue['rendered']}"},
        ]
        return {
            "messages": messages,
            "target_id": dialogue["target_id"],
            "target_label": dialogue["label"],
            "disclosure": disclosure,
            "source": target.get("source", "unknown"),
            "rendered": dialogue["rendered"],
        }

    @staticmethod
    def _extract_message_text(data: dict, source: str) -> str | None:
        if not isinstance(data, dict):
            audit.log("ERROR", source, f"{source} 返回不是 JSON 对象: {data!r}")
            return None
        if data.get("error"):
            audit.log("ERROR", source, f"{source} 返回错误: {data.get('error')}")
            return None
        choices = data.get("choices")
        if not choices:
            audit.log("ERROR", source, f"{source} 缺少 choices: {data}")
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not message or "content" not in message:
            audit.log("ERROR", source, f"{source} choices[0].message.content 缺失: {data}")
            return None
        content = str(message["content"]).strip()
        return content or None

    @staticmethod
    def _parse_json_object(text: str) -> dict | None:
        raw = str(text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None

    @staticmethod
    async def request_ai(messages: list, max_tokens: int = None) -> str | None:
        return await BrainInterpreter._request_ai_deepseek(messages, max_tokens)

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(Exception))
    async def _request_ai_deepseek(messages: list, max_tokens: int = None) -> str | None:
        try:
            session = await _get_http_session()
            payload = {
                "model": GlobalConfig.DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": GlobalConfig.DEEPSEEK_TEMPERATURE,
                "max_tokens": max_tokens if max_tokens else GlobalConfig.MAX_TOKENS,
            }
            async with session.post(
                f"{GlobalConfig.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {GlobalConfig.DEEPSEEK_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=GlobalConfig.REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    audit.log("ERROR", "API", f"DeepSeek 返回异常状态码: {resp.status} {body[:300]}")
                    raise Exception(f"DeepSeek returned {resp.status}")
                data = await resp.json(content_type=None)
            return BrainInterpreter._extract_message_text(data, "API")
        except Exception as e:
            audit.log("ERROR", "API", f"DeepSeek 请求异常: {e}")
            raise

    @staticmethod
    async def _post_dashscope_completion(payload: dict, *, source: str = "API") -> str | None:
        session = await _get_http_session()
        last_body = ""
        for attempt in range(2):
            async with session.post(
                f"{GlobalConfig.VISION_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {GlobalConfig.DASHSCOPE_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=GlobalConfig.REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return BrainInterpreter._extract_message_text(data, source)
                last_body = await resp.text()
                if attempt == 0 and "enable_thinking" in payload and re.search(
                    r"(enable_thinking|unsupported|not support|invalid parameter|未知参数|不支持)",
                    last_body,
                    re.I,
                ):
                    audit.log("WARN", source, "当前模型不接受 enable_thinking 参数，已自动移除后重试。")
                    payload = dict(payload)
                    payload.pop("enable_thinking", None)
                    continue
                audit.log("WARN", source, f"DashScope 返回异常状态码: {resp.status} {last_body}")
                raise Exception(f"DashScope request failed with status {resp.status}")
        audit.log("WARN", source, f"DashScope 请求失败: {last_body}")
        raise Exception("DashScope request failed")

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(Exception))
    async def _request_dashscope_chat(
        model: str, messages: list, max_tokens: int, temperature: float,
        *, enable_thinking: bool | None = None, source: str = "API"
    ) -> str | None:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if enable_thinking is not None:
            payload["enable_thinking"] = bool(enable_thinking)
        return await BrainInterpreter._post_dashscope_completion(payload, source=source)

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(Exception))
    async def _request_dashscope_vision(model: str, messages: list, max_tokens: int, temperature: float) -> str | None:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "enable_thinking": GlobalConfig.DASHSCOPE_ENABLE_THINKING,
        }
        return await BrainInterpreter._post_dashscope_completion(payload, source="API")

    @staticmethod
    def _coerce_model_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value or "").strip().lower()
        return text in ("true", "yes", "y", "1", "是", "对", "需要", "疑似", "违规", "广告")

    @staticmethod
    async def should_reply_in_group(combined_msg: str, context: list, nickname: str, prev_msg: dict = None) -> bool:
        result = await BrainInterpreter.judge_group_message(combined_msg, context, nickname, prev_msg)
        return result.get("reply", True)

    @staticmethod
    async def judge_group_message(
        combined_msg: str, context: list, nickname: str, prev_msg: dict = None,
        judge_images: list | None = None
    ) -> dict:
        try:
            recent = context[-8:] if len(context) >= 8 else context
            recent_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in recent)
            last_bot_turn = next((str(m.get("content", "")).strip() for m in reversed(context) if m.get("role") == "assistant"), "")
            if prev_msg:
                prev_uid = str(prev_msg.get("user_id") or "").strip()
                prev_label = f"{prev_msg.get('nickname')}({prev_uid})" if prev_uid else str(prev_msg.get("nickname"))
                prev_msg_text = f"群内上一条消息来自【{prev_label}】：{prev_msg.get('text')}"
            else:
                prev_msg_text = "没有上一条群消息。"
            visual_items = []
            for img in (judge_images or [])[:3]:
                if not isinstance(img, dict):
                    continue
                url = unescape(str(img.get("url") or "")).strip()
                if url:
                    visual_items.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
            has_visual = any(k in combined_msg for k in ("图片", "表情包", "视频", "[图", "[表情")) or bool(visual_items)
            prompt = get_judge_group_message_prompt(recent_text, last_bot_turn, prev_msg_text, combined_msg)
            audit.log(
                "INFO", "JUDGE",
                f"调用群聊初审模型 model={GlobalConfig.JUDGE_MODEL} has_visual={has_visual} "
                f"judge_frames={len(visual_items)} prompt_len={len(prompt)}"
            )
            user_content = prompt
            if visual_items:
                user_content = visual_items + [{
                    "type": "text",
                    "text": (
                        f"{prompt}\n\n"
                        "【视频帧初审补充】上方图片是同一条视频消息随机抽取并压缩后的 2-3 帧。"
                        "视频消息不进行 OCR；你必须直接查看这些帧，结合文字内容判断是否疑似广告/违规，以及是否需要淑雪回复。"
                    ),
                }]
            raw = await BrainInterpreter._request_dashscope_chat(
                GlobalConfig.JUDGE_MODEL,
                [
                    {"role": "system", "content": "你是只输出 JSON 的文本分类器。不要输出 Markdown，不要解释。"},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=220,
                temperature=0.0,
                enable_thinking=False,
                source="JUDGE",
            )
            parsed = BrainInterpreter._parse_json_object(raw or "")
            if not parsed:
                fallback_reply = bool(re.search(r"(@淑雪|淑雪|小雪)", combined_msg))
                audit.log("WARN", "JUDGE", f"初审模型输出无法解析，启用兜底。raw={raw!r}")
                return {"reply": fallback_reply, "ad_kick_candidate": False, "ad_confidence": 0.0, "reason": "初审模型输出无法解析，未进入广告处罚链路。"}
            try:
                ad_confidence = float(parsed.get("ad_confidence") or 0.0)
            except (TypeError, ValueError):
                ad_confidence = 0.0
            model_ad = BrainInterpreter._coerce_model_bool(parsed.get("ad_kick_candidate"))
            reply_decision = BrainInterpreter._coerce_model_bool(parsed.get("reply", False))
            judged = {
                "reply": reply_decision,
                "ad_kick_candidate": bool(model_ad),
                "ad_confidence": max(0.0, min(1.0, ad_confidence)),
                "reason": str(parsed.get("reason") or "初审模型未给出理由").strip(),
            }
            audit.log("INFO", "JUDGE", f"群消息判断[{nickname}]\n当前消息:\n{combined_msg}\n上一条群消息:\n{prev_msg_text}\n结果: reply={judged['reply']} ad={judged['ad_kick_candidate']} confidence={judged['ad_confidence']:.2f}\n理由: {judged['reason']}")
            return judged
        except Exception as e:
            fallback_reply = bool(re.search(r"(@淑雪|淑雪|小雪)", combined_msg or ""))
            audit.log("WARN", "JUDGE", f"判断模型异常，启用兜底 reply={fallback_reply} ad=False: {e}")
            return {"reply": fallback_reply, "ad_kick_candidate": False, "ad_confidence": 0.0, "reason": "初审模型异常，未进入广告处罚链路。"}

    @staticmethod
    async def review_apk_identity(apk_info: dict, scan_reason: str = "") -> dict:
        try:
            apk_info = apk_info or {}
            text_prompt = get_apk_identity_review_prompt(apk_info, scan_reason)
            content = []
            icon_url = unescape(str(apk_info.get("icon_data_url") or "")).strip()
            if icon_url:
                content.append({"type": "image_url", "image_url": {"url": icon_url}})
            content.append({"type": "text", "text": text_prompt})
            try:
                if icon_url:
                    raw = await BrainInterpreter._request_dashscope_vision(
                        GlobalConfig.APK_REVIEW_MODEL,
                        [{"role": "user", "content": content}],
                        max_tokens=180,
                        temperature=0.0,
                    )
                else:
                    raw = await BrainInterpreter._request_dashscope_chat(
                        GlobalConfig.APK_REVIEW_MODEL,
                        [{"role": "user", "content": text_prompt}],
                        max_tokens=180,
                        temperature=0.0,
                        source="APKREVIEW",
                    )
            except Exception as image_error:
                if not icon_url:
                    raise
                audit.log("WARN", "APKREVIEW", f"APK图标审核失败，改用纯文本身份审核: {type(image_error).__name__}: {image_error}")
                raw = await BrainInterpreter._request_dashscope_chat(
                    GlobalConfig.APK_REVIEW_MODEL,
                    [{"role": "user", "content": text_prompt}],
                    max_tokens=180,
                    temperature=0.0,
                    source="APKREVIEW",
                )
            parsed = BrainInterpreter._parse_json_object(raw or "")
            if not parsed:
                audit.log("WARN", "APKREVIEW", f"APK身份审核输出无法解析，按高风险处理: {raw!r}")
                return {
                    "action": "kick",
                    "kick": True,
                    "confidence": 0.6,
                    "reason": "APK身份审核输出无法解析，按疑似非正规软件处理。",
                }
            action = str(parsed.get("action") or "").strip().lower()
            try:
                confidence = float(parsed.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            kick = action == "kick" or BrainInterpreter._coerce_model_bool(parsed.get("kick"))
            if kick:
                action = "kick"
            elif action != "pass":
                action = "kick"
                kick = True
            result = {
                "action": action,
                "kick": bool(kick),
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(parsed.get("reason") or "APK身份审核未给出理由").strip(),
            }
            audit.log(
                "INFO" if not result["kick"] else "WARN",
                "APKREVIEW",
                f"APK身份审核 model={GlobalConfig.APK_REVIEW_MODEL} app={apk_info.get('app_name') or '未知'} "
                f"pkg={apk_info.get('package') or '未知'} action={result['action']} "
                f"confidence={result['confidence']:.2f} reason={result['reason']}"
            )
            return result
        except Exception as e:
            audit.log("WARN", "APKREVIEW", f"APK身份审核异常，按高风险处理: {type(e).__name__}: {e}")
            return {
                "action": "kick",
                "kick": True,
                "confidence": 0.6,
                "reason": f"APK身份审核异常，无法确认正规软件身份：{type(e).__name__}: {e}",
            }

    @staticmethod
    async def _ad_kick_model_judge(
        model: str, group_name: str, nickname: str, user_id: str, message_text: str,
        stage: str, prior_reason: str = "", images: list | None = None
    ) -> dict:
        visual_evidence = await BrainInterpreter._build_ad_review_visual_evidence(images)
        visual_note = (
            "本次包含图片/表情包。高级复审会收到原图，必须结合原图和【图片二次识别证据】判断，不要只因出现商品、价格、购物平台界面就判广告。"
            if images else "本次没有可用图片输入。"
        )
        prompt = get_ad_kick_model_judge_prompt(group_name, nickname, user_id, prior_reason, visual_note, visual_evidence, message_text)
        try:
            content = []
            if images:
                for img in images[:4]:
                    url = unescape(str(img.get("url") or "")).strip() if isinstance(img, dict) else ""
                    prepared = await BrainInterpreter._prepare_vision_image_url_local(url) if url else None
                    if prepared:
                        content.append({"type": "image_url", "image_url": {"url": prepared}})
            content.append({"type": "text", "text": prompt})
            messages = [{"role": "user", "content": content if images else prompt}]
            raw = await BrainInterpreter._request_dashscope_vision(model, messages, max_tokens=180, temperature=0.0)
            parsed = BrainInterpreter._parse_json_object(raw or "")
            if not parsed:
                return {"action": "ignore", "kick": False, "confidence": 0.0, "reason": f"模型输出无法解析: {raw}"}
            try:
                confidence = float(parsed.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            action = str(parsed.get("action") or "").strip().lower()
            if action not in ("kick", "suspect", "reply", "ignore"):
                action = "kick" if BrainInterpreter._coerce_model_bool(parsed.get("kick")) else "ignore"
            if BrainInterpreter._visual_evidence_is_safe_platform_or_game(visual_evidence) and action in ("kick", "suspect"):
                audit.log("WARN", "ADKICK", "图片二次识别为普通购物/游戏截图，已阻止广告处罚。")
                action = "ignore"
            kick = action == "kick" or BrainInterpreter._coerce_model_bool(parsed.get("kick"))
            if kick:
                action = "kick"
            return {"action": action, "kick": kick, "confidence": max(0.0, min(1.0, confidence)), "reason": str(parsed.get("reason") or "").strip()}
        except Exception as e:
            audit.log("WARN", "ADKICK", f"{stage}模型判断异常: {e}")
            return {"action": "ignore", "kick": False, "confidence": 0.0, "reason": "复审模型异常，安全放行。"}

    @staticmethod
    def _ocr_text_indicates_safe_platform_or_game(text: str) -> bool:
        text = str(text or "")
        safe_category = any(k in text for k in ("购物平台普通截图", "购物平台", "商品页", "订单页", "购物车", "游戏普通截图", "游戏截图", "战绩", "抽卡"))
        hard_ad = any(k in text for k in ("加微信", "加QQ", "加群", "私聊交易", "扫码进群", "刷单", "贷款", "博彩", "色情引流", "裸聊", "约炮"))
        return safe_category and not hard_ad

    @staticmethod
    def _visual_evidence_is_safe_platform_or_game(visual_evidence: str) -> bool:
        text = str(visual_evidence or "")
        if BrainInterpreter._ocr_text_indicates_safe_platform_or_game(text):
            return True
        safe_category = any(k in text for k in ("购物平台普通截图", "购物平台", "商品页", "订单页", "游戏普通截图", "游戏截图", "战绩", "抽卡", "不足以认定广告"))
        explicit_safe = any(k in text for k in ("不足以认定广告", "无广告风险", "未发现广告", "无明显广告"))
        strong_ad = any(k in text for k in ("加微信", "加QQ", "加群", "私聊交易", "推广链接", "返利", "刷单", "贷款", "博彩", "色情引流", "二维码"))
        return safe_category and explicit_safe and not strong_ad

    _visual_evidence_is_safe_shopping = _visual_evidence_is_safe_platform_or_game

    @staticmethod
    async def should_kick_ad_sender(
        group_name: str, nickname: str, user_id: str, message_text: str,
        first: dict | None = None, images: list | None = None
    ) -> dict:
        if not message_text and not images:
            return {"kick": False, "action": "ignore", "reason": "没有可审核内容。"}
        if first is None:
            first = {"kick": False, "confidence": 0.0, "reason": "缺少初审模型结果，未进入广告处罚链路。"}
        confidence = float(first.get("confidence") or 0.0)
        audit.log("INFO", "ADKICK", f"初筛 {nickname}({user_id})\n消息:\n{message_text}\n结果: {first.get('kick')} confidence={confidence:.2f}\n理由: {first.get('reason')}")
        if not first.get("kick"):
            audit.log("INFO", "ADKICK", "初筛模型未命中广告候选，不进入高级复审。")
            return {"kick": False, "action": "pass", "reason": first.get("reason", ""), "first": first}
        review = await BrainInterpreter._ad_kick_model_judge(
            GlobalConfig.AD_REVIEW_MODEL,
            group_name,
            nickname,
            user_id,
            message_text,
            "高级复审",
            prior_reason=first.get("reason", ""),
            images=images,
        )
        audit.log("INFO", "ADKICK", f"复审 {nickname}({user_id})\n消息:\n{message_text}\n结果: action={review.get('action')} kick={review.get('kick')} confidence={float(review.get('confidence') or 0.0):.2f}\n理由: {review.get('reason')}")
        if review.get("action") == "suspect":
            return {"kick": False, "action": "suspect", "reason": review.get("reason") or first.get("reason", "疑似广告，撤回但不踢人"), "first": first}
        if review.get("action") == "reply":
            return {"kick": False, "action": "reply", "reason": review.get("reason", ""), "first": first}
        if review.get("kick"):
            return {"kick": True, "action": "kick", "reason": review.get("reason") or "广告复审确认", "first": first}
        return {"kick": False, "action": "ignore", "reason": review.get("reason", ""), "first": first}

    @staticmethod
    async def name_sticker(description: str, image_url: str | None = None, frames_b64: list[str] | None = None) -> str | None:
        try:
            prompt = get_name_sticker_prompt(description)
            content = []
            if frames_b64:
                for b64 in frames_b64[:3]:
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
            elif image_url:
                prepared = await BrainInterpreter._prepare_vision_image_url_local(image_url)
                if prepared:
                    content.append({"type": "image_url", "image_url": {"url": prepared}})
            content.append({"type": "text", "text": prompt})
            raw = await BrainInterpreter._request_dashscope_vision(GlobalConfig.VISION_MODEL, [{"role": "user", "content": content}], max_tokens=24, temperature=0.4)
            if not raw:
                return None
            name = re.sub(r"[^\w\u4e00-\u9fff]+", "", raw).strip()
            return name[:12] or None
        except Exception as e:
            audit.log("ERROR", "EMOTE", f"表情包命名异常: {e}")
            return None

    @staticmethod
    async def collect_sticker(url: str, description: str, metadata: dict | None = None):
        try:
            session = await _get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.read()
                ct = resp.content_type or ""
            size_kb = len(data) // 1024
            is_gif = "gif" in ct.lower() or url.lower().endswith(".gif")
            if is_gif and size_kb > GlobalConfig.GIF_MAX_SIZE_KB:
                audit.log("WARN", "EMOTE", f"GIF 过大({size_kb}KB)，跳过。")
                return
            loop = asyncio.get_running_loop()
            file_hash = hashlib.md5(data).hexdigest()
            existing_hashes = await loop.run_in_executor(None, AssetManager._hash_of_existing_emotes)
            if file_hash in existing_hashes:
                audit.log("INFO", "EMOTE", "表情包已存在，跳过重复入库。")
                return
            if is_gif:
                frames_b64 = await loop.run_in_executor(None, lambda: AssetManager.extract_gif_frames(data))
                name = await BrainInterpreter.name_sticker(description, frames_b64=frames_b64 if frames_b64 else None, image_url=url if not frames_b64 else None)
            else:
                name = await BrainInterpreter.name_sticker(description, image_url=url)
            if not name:
                audit.log("WARN", "EMOTE", "未能生成有效名称，放弃保存。")
                return
            ext = ".gif" if is_gif else ".jpg" if "jpeg" in ct or "jpg" in ct else ".png"
            audit.log("INFO", "EMOTE", f"表情包命名完成 [{name}]，开始保存。")
            await loop.run_in_executor(None, lambda: AssetManager.save_sticker_bytes(data, name, ext, metadata=metadata))
        except Exception as e:
            audit.log("ERROR", "EMOTE", f"collect_sticker 异常: {e}")

    @staticmethod
    async def describe_image(image_url: str) -> str | None:
        try:
            prepared = await BrainInterpreter._prepare_vision_image_url_local(image_url)
            if not prepared:
                return None
            prompt = "请用中文简洁描述这张图片的主体、场景、文字和情绪，控制在80字以内。"
            raw = await BrainInterpreter._request_dashscope_vision(
                GlobalConfig.VISION_MODEL,
                [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": prepared}}, {"type": "text", "text": prompt}]}],
                max_tokens=120,
                temperature=0.2,
            )
            return raw.strip() if raw else None
        except Exception as e:
            audit.log("WARN", "VISION", f"图片描述失败: {e}")
            return None

    @staticmethod
    async def extract_video_frames(video_url: str, count: int = 3) -> list[str]:
        tmp_path = None
        try:
            video_url = unescape(str(video_url or "")).strip()
            if not video_url:
                return []
            try:
                import cv2
            except Exception as e:
                audit.log("ERROR", "VIDEO", f"OpenCV 不可用，无法抽取视频帧: {e}")
                return []

            data_limit = 40 * 1024 * 1024
            if re.match(r"https?://", video_url, re.I):
                session = await _get_http_session()
                async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    if resp.status != 200:
                        audit.log("WARN", "VIDEO", f"视频下载失败: {resp.status} {video_url[:100]}")
                        return []
                    data = await resp.content.read(data_limit + 1)
                    if len(data) > data_limit:
                        audit.log("WARN", "VIDEO", "视频超过40MB抽帧读取上限，已跳过。")
                        return []
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name
            else:
                tmp_path = video_url
                if not os.path.exists(tmp_path):
                    audit.log("WARN", "VIDEO", f"视频没有可访问 URL 或本地文件不存在: {video_url[:100]}")
                    return []

            def _extract() -> list[str]:
                cap = cv2.VideoCapture(tmp_path)
                try:
                    if not cap.isOpened():
                        return []
                    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    target_count = max(2, min(3, int(count or 3)))
                    if frame_total > 0:
                        start = min(frame_total - 1, max(0, int(frame_total * 0.08)))
                        end = max(start + 1, min(frame_total, int(frame_total * 0.92)))
                        candidates = list(range(start, end)) or list(range(frame_total))
                        if len(candidates) <= target_count:
                            indices = candidates
                        else:
                            indices = sorted(random.sample(candidates, target_count))
                    else:
                        indices = [0, 30, 90][:target_count]

                    frames = []
                    for frame_index in indices:
                        if frame_total > 0:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
                        ok, frame = cap.read()
                        if not ok or frame is None:
                            continue
                        height, width = frame.shape[:2]
                        max_side = 768
                        scale = min(1.0, max_side / max(width, height))
                        if scale < 1.0:
                            frame = cv2.resize(
                                frame,
                                (max(1, int(width * scale)), max(1, int(height * scale))),
                                interpolation=cv2.INTER_AREA,
                            )
                        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 76])
                        if ok:
                            frames.append(f"data:image/jpeg;base64,{base64.b64encode(encoded.tobytes()).decode('ascii')}")
                    audit.log(
                        "INFO", "VIDEO",
                        f"视频抽帧完成 frames={len(frames)}/{target_count} total_frames={frame_total or '未知'} fps={fps:.2f}"
                    )
                    return frames
                finally:
                    cap.release()

            return await asyncio.to_thread(_extract)
        except Exception as e:
            audit.log("ERROR", "VIDEO", f"视频抽帧失败: {type(e).__name__}: {e}")
            return []
        finally:
            if tmp_path and re.match(r"https?://", str(video_url or ""), re.I):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    @staticmethod
    async def ocr_image(image_url: str) -> str | None:
        try:
            image_url = unescape(image_url).strip()
            if not _PIL_AVAILABLE:
                audit.log("WARN", "OCR", "Pillow 不可用，普通 OCR 跳过。")
                return None
            session = await _get_http_session()
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    audit.log("WARN", "OCR", f"图片 OCR 下载失败: {resp.status} {image_url[:80]}")
                    return None
                data = await resp.content.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    audit.log("WARN", "OCR", "图片超过普通 OCR 读取上限，跳过。")
                    return None
            text = await asyncio.to_thread(BrainInterpreter._run_local_ocr, data)
            summary = BrainInterpreter._summarize_ocr_for_ad(text)
            audit.log("INFO", "OCR", f"本地 OCR 完成: {summary}")
            return summary
        except ImportError:
            audit.log("WARN", "OCR", "未安装普通 OCR 依赖 rapidocr_onnxruntime，已跳过 OCR。")
            return None
        except Exception as e:
            audit.log("ERROR", "OCR", f"本地 OCR 失败: {e}")
            return None

    @classmethod
    def _get_local_ocr_engine(cls):
        if cls._OCR_ENGINE is not None:
            return cls._OCR_ENGINE
        if cls._OCR_ENGINE_FAILED:
            raise ImportError("rapidocr_onnxruntime unavailable")
        try:
            from rapidocr_onnxruntime import RapidOCR
            cls._OCR_ENGINE = RapidOCR()
            return cls._OCR_ENGINE
        except Exception as e:
            cls._OCR_ENGINE_FAILED = True
            raise ImportError(str(e))

    @classmethod
    def _run_local_ocr(cls, image_bytes: bytes) -> str:
        engine = cls._get_local_ocr_engine()
        with _PILImage.open(io.BytesIO(image_bytes)) as img:
            img.load()
            if getattr(img, "is_animated", False):
                img.seek(0)
            img.thumbnail((1800, 1800))
            if img.mode not in ("RGB", "L"):
                bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                if "A" in img.getbands():
                    bg.paste(img, mask=img.getchannel("A"))
                    img = bg
                else:
                    img = img.convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
                img.convert("RGB").save(tmp, format="JPEG", quality=90)
        try:
            result, _ = engine(tmp_path)
            parts = []
            for item in result or []:
                if len(item) >= 2:
                    parts.append(str(item[1]))
            return "\n".join(parts).strip()
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    @staticmethod
    def _summarize_ocr_for_ad(text: str) -> str:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return "无可读文字；普通OCR未发现广告风险。"
        clipped = raw[:260]
        risks = []
        if re.search(r"(微信|QQ|vx|VX|加群|群号|私聊|私信|二维码|扫码|链接|http|www\.)", raw, re.I):
            risks.append("联系方式/链接/二维码")
        if re.search(r"(刷单|返利|贷款|博彩|兼职|日结|免费领|福利|裸聊|约炮|成人|推广|引流)", raw):
            risks.append("广告或灰产风险词")
        safe_bits = []
        if re.search(r"(淘宝|天猫|京东|拼多多|闲鱼|订单|商品详情|购物车|物流)", raw):
            safe_bits.append("疑似购物平台普通截图")
        if re.search(r"(游戏|战绩|角色|抽卡|装备|背包|排行|关卡|活动)", raw):
            safe_bits.append("疑似游戏普通截图")
        if risks:
            return f"OCR文字：{clipped}；广告风险：{','.join(risks)}。"
        if safe_bits:
            return f"OCR文字：{clipped}；{','.join(safe_bits)}；无广告风险。"
        return f"OCR文字：{clipped}；普通OCR未发现广告风险。"

    @staticmethod
    async def _build_ad_review_visual_evidence(images: list | None) -> str:
        if not images:
            return ""
        parts = []
        for idx, img in enumerate(images[:3], 1):
            if not isinstance(img, dict):
                continue
            url = unescape(str(img.get("url") or "")).strip()
            if not url:
                continue
            ocr_text = str(img.get("ocr_text") or "")
            summary = await BrainInterpreter._analyze_image_for_ad_review(url, ocr_text=ocr_text)
            if summary:
                parts.append(f"[图{idx}] {summary}")
        return "\n".join(parts)

    @staticmethod
    async def _analyze_image_for_ad_review(image_url: str, ocr_text: str = "") -> str | None:
        try:
            prepared = await BrainInterpreter._prepare_vision_image_url_local(image_url)
            if not prepared:
                return None
            prompt = get_analyze_image_for_ad_review_prompt(ocr_text)
            raw = await BrainInterpreter._request_dashscope_vision(
                GlobalConfig.VISION_MODEL,
                [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": prepared}}, {"type": "text", "text": prompt}]}],
                max_tokens=220,
                temperature=0.0,
            )
            summary = (raw or "").strip()
            if summary:
                audit.log("INFO", "ADKICK", f"图片二次识别证据: {summary}")
            return summary or None
        except Exception as e:
            audit.log("WARN", "ADKICK", f"图片二次识别异常: {e}")
            return None

    @staticmethod
    def _extract_asr_text(result: dict) -> str | None:
        try:
            sentences = []
            for item in result.get("output", {}).get("results", []) or []:
                transcription_url = item.get("transcription_url")
                if item.get("transcription"):
                    sentences.append(str(item.get("transcription")))
                elif transcription_url:
                    sentences.append(str(transcription_url))
            if sentences:
                return "\n".join(sentences)
        except Exception:
            pass
        return None

    @staticmethod
    async def transcribe_audio(audio_url: str) -> str | None:
        try:
            from http import HTTPStatus
            import dashscope
            from dashscope.audio.asr import Transcription

            dashscope.api_key = GlobalConfig.DASHSCOPE_KEY

            def _call():
                task_response = Transcription.async_call(model="fun-asr", file_urls=[audio_url])
                return Transcription.wait(task=task_response.output.task_id)

            response = await asyncio.to_thread(_call)
            if getattr(response, "status_code", None) == HTTPStatus.OK:
                data = response if isinstance(response, dict) else json.loads(json.dumps(response, default=lambda o: getattr(o, "__dict__", str(o)), ensure_ascii=False))
                text = BrainInterpreter._extract_asr_text(data)
                if text:
                    return text
                output = getattr(response, "output", None)
                return json.dumps(output, ensure_ascii=False, default=str) if output else None
            audit.log("WARN", "ASR", f"fun-asr 返回异常: {getattr(response, 'status_code', None)} {response}")
            return None
        except Exception as e:
            audit.log("ERROR", "ASR", f"语音转文字失败: {e}")
            return None

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        try:
            parsed = urlsplit(str(url or ""))
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    @staticmethod
    async def _prepare_vision_image_url(image_url: str, is_sticker: bool = False) -> str:
        prepared = await BrainInterpreter._prepare_vision_image_url_local(image_url)
        return prepared or image_url

    @staticmethod
    async def _prepare_vision_image_url_local(image_url: str) -> str | None:
        try:
            image_url = unescape(str(image_url or "")).strip()
            if not image_url:
                return None
            if image_url.startswith("data:image/"):
                return image_url
            session = await _get_http_session()
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return image_url if BrainInterpreter._is_public_http_url(image_url) else None
                data = await resp.content.read(12 * 1024 * 1024 + 1)
                if len(data) > 12 * 1024 * 1024:
                    return image_url if BrainInterpreter._is_public_http_url(image_url) else None
            if not _PIL_AVAILABLE:
                return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
            with _PILImage.open(io.BytesIO(data)) as img:
                img.load()
                if getattr(img, "is_animated", False):
                    img.seek(0)
                img.thumbnail((1024, 1024))
                if img.mode not in ("RGB", "L"):
                    bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                    if "A" in img.getbands():
                        bg.paste(img, mask=img.getchannel("A"))
                        img = bg
                    else:
                        img = img.convert("RGB")
                out = io.BytesIO()
                img.convert("RGB").save(out, format="JPEG", quality=82, optimize=True)
            audit.log("INFO", "VISION", "图片已本地降分辨率后以 base64 发送: max_side=1024")
            return f"data:image/jpeg;base64,{base64.b64encode(out.getvalue()).decode('ascii')}"
        except Exception as e:
            audit.log("WARN", "VISION", f"图片本地读取/降分辨率失败: {e}")
            return image_url if BrainInterpreter._is_public_http_url(image_url) else None

    @classmethod
    async def process_multimodal_interaction(
        cls, tid, raw_input, images, nickname,
        target_type="private", bot_instance=None,
        group_name=None, sender_uid=None
    ):
        raw_input = cls._limit_input_text(raw_input)
        usable_images = []
        seen = set()
        for img in images or []:
            url = unescape(str(img.get("url") or "")).strip() if isinstance(img, dict) else ""
            if not url or url in seen:
                continue
            seen.add(url)
            item = dict(img)
            item["url"] = url
            usable_images.append(item)
        if not usable_images:
            return await cls.process_interaction(tid, raw_input, nickname, target_type, bot_instance, group_name=group_name, sender_uid=sender_uid)

        tid_str = str(tid)
        personal_uid = str(sender_uid) if sender_uid else tid_str
        previous_last_active = await db.get_user_last_active(personal_uid)
        favor, mood, facts_json = await db.sync_user(personal_uid, nickname)
        try:
            facts = json.loads(facts_json) if facts_json else []
        except Exception:
            facts = []
        reunion_context = cls._reunion_context_from_last_active(nickname, favor, previous_last_active) or await db.get_reunion_context(personal_uid, nickname, favor, target_type="private")
        audit.log("INFO", "TIMESENSE", f"[{nickname}({personal_uid})] 视觉链路注入重逢感知: {reunion_context[:80]}")
        is_admin = personal_uid == GlobalConfig.ADMIN_UID
        if target_type == "group" and group_name:
            context_desc = f"【当前群聊】{group_name} | 【发言人】{nickname} ({'哥哥' if is_admin else '群成员'})"
        else:
            context_desc = f"【当前交互对象】{nickname} ({'哥哥' if is_admin else '普通朋友'})"
        relevant_mem = await db.retrieve_relevant_memories(tid_str, raw_input or "图片/表情包", limit=5)
        semantic_facts_text = await db.get_semantic_facts_text(personal_uid)
        user_meta = await db.get_user_meta_state(personal_uid)
        meta_context = ""
        if user_meta["emotional_baseline"] or user_meta["last_notable_event"]:
            meta_context = f"【关系元记忆】关系阶段:{user_meta['relationship_stage']} | 情感基线:{user_meta['emotional_baseline'] or '无'} | 最近重要事件:{user_meta['last_notable_event'] or '无'}"
        cross_chat_context = bot_instance.build_cross_chat_context(tid_str, personal_uid) if bot_instance and hasattr(bot_instance, "build_cross_chat_context") else ""
        cross_person_recent_context = await db.get_cross_person_recent_context(tid_str, current_target_type=target_type, limit_targets=4, per_target_msgs=6)
        messages = [
            {"role": "system", "content": PersonalityCore.get_dynamic_prompt()},
            {"role": "system", "content": cls._memory_fidelity_prompt(raw_input)},
            {"role": "system", "content": context_desc},
            {"role": "system", "content": f"【当前好感度】{favor} | 【当前心情】{mood}"},
            {"role": "system", "content": semantic_facts_text or f"【长期事实库】{facts}"},
            {"role": "system", "content": meta_context or "【关系元记忆】尚未建立稳定关系基线。"},
            {"role": "system", "content": reunion_context},
            {"role": "system", "content": relevant_mem or "【世界记忆】此刻没有特别浮现的记忆。"},
            {"role": "system", "content": cross_chat_context or "【其他正在进行的对话】当前没有可参考的其他活跃对话。"},
            {"role": "system", "content": cross_person_recent_context or "【其他人的最近对话】数据库里没有可参考的其他会话。"},
            {"role": "system", "content": get_vision_reply_mode_prompt()},
        ]
        messages.extend(await db.fetch_context(tid_str, target_type=target_type))

        use_deep_thinking = any(not img.get("is_sticker") for img in usable_images)
        if use_deep_thinking:
            audit.log("INFO", "VISION", "本轮包含真实图片，Qwen视觉回复开启深度思考")
        user_content = []
        for img in usable_images[:6]:
            prepared = await cls._prepare_vision_image_url_local(img["url"])
            if prepared:
                user_content.append({"type": "image_url", "image_url": {"url": prepared}})
            else:
                audit.log("WARN", "VISION", f"图片不可读取，已跳过视觉输入: {img['url'][:80]}")
        if not user_content:
            return await cls.process_interaction(tid, f"{raw_input}\n[图片无法读取，已降级为普通文本消息。]", nickname, target_type, bot_instance, group_name=group_name, sender_uid=sender_uid)
        user_content.append({"type": "text", "text": raw_input or "对方发来了图片/表情包，请直接看图后自然回应。"})
        messages.append({"role": "user", "content": user_content})
        audit.log_ai_final(json.dumps(messages, ensure_ascii=False, indent=2))
        try:
            raw_res = await cls._request_dashscope_vision(
                GlobalConfig.VISION_MODEL,
                messages,
                max_tokens=GlobalConfig.MAX_TOKENS,
                temperature=GlobalConfig.DEEPSEEK_TEMPERATURE,
            )
        except Exception as e:
            audit.log("ERROR", "VISION", f"Qwen原生视觉回复请求失败: {type(e).__name__}: {e!r}")
            return ["呜……视觉通道刚才短路了一下。"]
        return await cls._finalize_model_reply(raw_res, tid_str, personal_uid, nickname, favor, facts, raw_input, target_type, bot_instance, vision_count=len(usable_images))

    @classmethod
    async def process_interaction(
        cls, tid, raw_input, nickname,
        target_type="private", bot_instance=None,
        is_active=False, active_ctx=None, group_name=None,
        sender_uid=None
    ):
        raw_input = cls._limit_input_text(raw_input)
        active_ctx = cls._limit_input_text(active_ctx) if active_ctx else active_ctx
        tid_str = str(tid)
        personal_uid = str(sender_uid) if sender_uid else tid_str
        previous_last_active = await db.get_user_last_active(personal_uid)
        favor, mood, facts_json = await db.sync_user(personal_uid, nickname)
        try:
            facts = json.loads(facts_json) if facts_json else []
        except Exception:
            facts = []
        reunion_context = cls._reunion_context_from_last_active(nickname, favor, previous_last_active) or await db.get_reunion_context(personal_uid, nickname, favor, target_type="private")
        audit.log("INFO", "TIMESENSE", f"[{nickname}({personal_uid})] 注入语境: {reunion_context[:80]}")
        is_admin = personal_uid == GlobalConfig.ADMIN_UID
        if target_type == "group" and group_name:
            context_desc = f"【当前群聊】{group_name} | 【发言人】{nickname} ({'哥哥' if is_admin else '群成员'})"
        else:
            context_desc = f"【当前交互对象】{nickname} ({'哥哥' if is_admin else '普通朋友'})"
        query_for_mem = raw_input if not is_active else (active_ctx or "")
        relevant_mem = await db.retrieve_relevant_memories(tid_str, query_for_mem, limit=5)
        if relevant_mem:
            audit.log("INFO", "MEMORY", f"注入全局记忆 {len(relevant_mem.splitlines())} 行")
        cross_person_branch = None
        if (not is_active) and cls.is_cross_person_query(raw_input):
            cross_person_branch = await cls.build_cross_person_answer(raw_input, tid_str, personal_uid, nickname, favor, mood, facts, reunion_context, context_desc, current_target_type=target_type, bot_instance=bot_instance)
        cross_chat_context = bot_instance.build_cross_chat_context(tid_str, personal_uid) if bot_instance and hasattr(bot_instance, "build_cross_chat_context") else ""
        cross_person_recent_context = await db.get_cross_person_recent_context(tid_str, current_target_type=target_type, limit_targets=4, per_target_msgs=6)
        semantic_facts_text = await db.get_semantic_facts_text(personal_uid)
        user_meta = await db.get_user_meta_state(personal_uid)
        meta_context = ""
        if user_meta["emotional_baseline"] or user_meta["last_notable_event"]:
            meta_context = f"【关系元记忆】关系阶段:{user_meta['relationship_stage']} | 情感基线:{user_meta['emotional_baseline'] or '无'} | 最近重要事件:{user_meta['last_notable_event'] or '无'}"
        messages = [
            {"role": "system", "content": PersonalityCore.get_dynamic_prompt()},
            {"role": "system", "content": cls._memory_fidelity_prompt(raw_input)},
            {"role": "system", "content": context_desc},
            {"role": "system", "content": f"【当前好感度】{favor} | 【当前心情】{mood}"},
            {"role": "system", "content": semantic_facts_text or f"【长期事实库】{facts}"},
            {"role": "system", "content": meta_context or "【关系元记忆】尚未建立稳定关系基线。"},
            {"role": "system", "content": reunion_context},
            {"role": "system", "content": relevant_mem or "【世界记忆】此刻没有特别浮现的记忆。"},
            {"role": "system", "content": "【跨人记忆规则】提到你和其他人的互动时，只能参考下方真实对话，不要编造。"},
            {"role": "system", "content": cross_chat_context or "【其他正在进行的对话】当前没有可参考的其他活跃对话。"},
            {"role": "system", "content": cross_person_recent_context or "【其他人的最近对话】数据库里没有可参考的其他会话。"},
        ]
        messages.extend(await db.fetch_context(tid_str, target_type=target_type))
        if is_active:
            messages.append({"role": "system", "content": f"【主动搭话背景】{active_ctx}"})
        else:
            messages.append({"role": "user", "content": raw_input})
        if cross_person_branch:
            insert_at = len(messages) - 1
            messages[insert_at:insert_at] = cross_person_branch["messages"]
        audit.log_ai_final(json.dumps(messages, ensure_ascii=False, indent=2))
        raw_res = await cls.request_ai(messages)
        if not raw_res:
            return ["呜……脑袋突然短路了一下。"]
        return await cls._finalize_model_reply(raw_res, tid_str, personal_uid, nickname, favor, facts, raw_input, target_type, bot_instance, is_active=is_active)

    @classmethod
    async def _finalize_model_reply(
        cls, raw_res: str | None, tid_str: str, personal_uid: str, nickname: str,
        favor: int, facts: list, raw_input: str, target_type: str, bot_instance=None,
        is_active: bool = False, vision_count: int = 0
    ) -> list[str]:
        if not raw_res:
            return ["呜……刚才没想好怎么回。"]
        audit.ai_raw("out", raw_res)
        history_input = raw_input
        if vision_count:
            history_input = f"{raw_input}\n[本轮包含{vision_count}张图片/表情包，已由视觉模型直接查看。]"
        if "[NONE]" in raw_res:
            if not is_active:
                await db.save_chat_node(tid_str, "user", history_input, target_type=target_type)
            return []
        records = cls._filter_record_tags(cls._RE_RECORD.findall(raw_res), raw_input, "VISION" if vision_count else "CHAT")
        new_facts_json = None
        if records:
            for rec in records:
                rec_key = rec.strip()[:40]
                asyncio.create_task(db.upsert_semantic_fact(personal_uid, rec_key, rec))
                emotion_weight = db._estimate_emotion_weight(rec)
                asyncio.create_task(db.store_vector_memory(personal_uid, rec, mem_type="fact", nickname=nickname, emotion_weight=emotion_weight))
            new_facts_json = json.dumps(list(dict.fromkeys(records + facts))[:30], ensure_ascii=False)
        fav_match = cls._RE_FAVOR.search(raw_res)
        f_delta = int(fav_match.group(1)) if fav_match else 0
        if cls._is_prompt_injection_attempt(raw_input):
            if f_delta:
                audit.log("WARN", "SECURITY", f"已忽略疑似注入诱导的 FAVOR: {f_delta}")
            f_delta = 0
        m_delta = random.randint(-10, 10) if random.random() < 0.3 else 0
        if not is_active:
            await db.sync_user(personal_uid, nickname, favor_delta=f_delta, mood_delta=m_delta, facts_json=new_facts_json)
        st_match = cls._RE_SEND_TO.search(raw_res)
        if st_match and bot_instance and not cls._is_prompt_injection_attempt(raw_input):
            target_who, forward_msg = st_match.group(1), st_match.group(2)
            if "哥" in target_who or "duo" in target_who.lower() or target_who == "duo0621":
                asyncio.create_task(bot_instance.send_direct(GlobalConfig.ADMIN_UID, f"【淑雪捎的话】{forward_msg}"))
        clean_text = cls._RE_CLEAN.sub("", raw_res)
        clean_text = cls._RE_LEAD.sub("", clean_text).strip()
        segments = [s.strip() for s in clean_text.split("[CONTINUE]") if s.strip()]
        final_responses = []
        history_pure_text = []
        for seg in segments:
            seg = cls._sanitize_network_style(seg)
            rendered = cls._RE_IMG.sub(lambda m: AssetManager.convert_to_cq(m.group(1)), seg)
            if rendered:
                final_responses.append(rendered)
                history_pure_text.append(cls._RE_CQ_IMAGE.sub("[表情]", rendered))
        if not is_active:
            await db.save_chat_node(tid_str, "user", history_input, target_type=target_type)
            asyncio.create_task(db._background_fact_extraction(personal_uid, nickname, raw_input))
        await db.save_chat_node(tid_str, "assistant", " ".join(history_pure_text), target_type=target_type)
        asyncio.create_task(db.consolidate_old_history(tid_str, nickname, target_type=target_type))
        return final_responses

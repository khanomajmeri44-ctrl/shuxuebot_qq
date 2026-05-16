"""主动社交调度器。

负责心跳式主动私聊/群内冒泡，以及每日夜间自我反思。
"""

from datetime import timezone
from .shared import *
from .memory import db
from .personality import PersonalityCore
from .brain import BrainInterpreter
from .assets import AssetManager

class SocialScheduler:
    def __init__(self, bot):
        self.bot = bot
        self.is_running = True
        self.HEARTBEAT_INTERVAL = random.randint(1000, 1800)

    async def run_loop(self):
        audit.log("SUCCESS", "SOCIAL", "心跳社交引擎已启动。")
        last_evolve_date = None
        while self.is_running:
            now = datetime.now()

            # ── [NEW] 夜间演化：凌晨3~4点，每天只执行一次 ──
            if 3 <= now.hour < 4 and now.date() != last_evolve_date:
                await self.nightly_evolution(now.date())
                last_evolve_date = now.date()

            if not (7 <= now.hour <= 23):
                audit.log("INFO", "SOCIAL", f"当前 {now.hour} 点，静默中，跳过心跳。")
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                continue
            audit.log("INFO", "SOCIAL", "心跳触发中...")
            await self._heartbeat_tick(now)
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _heartbeat_tick(self, now: datetime):
        try:
            candidates = await db.get_heartbeat_candidates()
            if not candidates:
                audit.log("INFO", "SOCIAL", "心跳检查：候选人池为空，跳过。")
                return

            weights = [max(1, c["favor"]) for c in candidates]
            target     = random.choices(candidates, weights=weights, k=1)[0]
            tid        = target["id"]
            tname      = target["name"]
            t_is_group = target["type"] == "group"
            direct_user_id = None
            direct_user_name = tname
            total_interaction = int(target.get("total_interaction") or 0)
            if (not t_is_group) and target.get("source_group_id") and str(tid) != GlobalConfig.ADMIN_UID:
                direct_user_id = str(tid)
                tid = str(target["source_group_id"])
                tname = target.get("source_group_name") or f"群{tid}"
                t_is_group = True

            audit.log("INFO", "SOCIAL", f"心跳抽中目标：{tname}({tid})")

            # [FIX] 群组心跳不应用群ID写 users 表（群ID ≠ 用户ID）
            if t_is_group:
                favor = 50
                facts = []
                if direct_user_id:
                    profile = await db.get_user_profile(direct_user_id)
                    favor = int(profile["favor"]) if profile else int(target.get("favor") or 10)
                    facts_json = profile["facts"] if profile else "[]"
                    facts = json.loads(facts_json)
            else:
                profile = await db.get_user_profile(tid)
                favor = int(profile["favor"]) if profile else int(target.get("favor") or 10)
                facts_json = profile["facts"] if profile else "[]"
                facts = json.loads(facts_json)
            history        = await db.fetch_context(
                tid, limit=6, target_type="group" if t_is_group else "private"
            )

            # ── [NEW v10.1] 心跳也注入时间流逝感 ──
            reunion_context = await db.get_reunion_context(
                tid, tname, favor, target_type="group" if t_is_group else "private"
            )

            is_admin      = str(tid) == GlobalConfig.ADMIN_UID
            if direct_user_id:
                relation_desc = (
                    f"这是群聊「{tname}」里见过的群友 {direct_user_name}。"
                    f"你们累计有效互动大约 {total_interaction} 次，还不算很熟。"
                    "如果主动开口，要像网络上刚认识不久的人一样轻一点、礼貌一点，"
                    "不要撒娇过度，不要装成私下非常亲密，也不要提拥抱贴贴之类线下动作。"
                )
            elif t_is_group:
                relation_desc = f"这是群聊「{tname}」，你是在群里主动冒泡。语气要像网络群聊，轻松但别过度装熟。"
            else:
                relation_desc = "这是你哥哥 duo0621，你对他最亲近" if is_admin else f"这是你的朋友 {tname}，按真实熟悉程度说话，不要默认线下贴贴。"

            heartbeat_prompt = f"""
现在是 {now.strftime('%Y-%m-%d %H:%M')}，星期{['一','二','三','四','五','六','日'][now.weekday()]}。
{relation_desc}。
{reunion_context}
你对 {direct_user_name if direct_user_id else tname} 的记忆是：{facts if facts else '暂无特别记录'}。
最近的对话片段是：{json.dumps(history, ensure_ascii=False) if history else '无'}。

现在请你以淑雪的身份，发自内心地判断：
- 你此刻有没有想主动跟 {direct_user_name if direct_user_id else tname} 说的话？
- 可以是关心、分享一个梗、想起了什么、或者只是无聊想聊天。
- 如果你觉得现在没什么特别想说的，或者不想打扰对方，就只输出 [NONE]。
- 如果有，就直接说出来，不要解释原因，直接说正文。
- 回复必须像 QQ 网络聊天，禁止写括号动作戏，禁止凭空装熟。
"""
            messages = [
                {"role": "system", "content": PersonalityCore.get_dynamic_prompt()},
                {"role": "system", "content": f"【当前交互对象】: {tname} ({'哥哥' if is_admin else '普通朋友'})"},
                # ── [NEW v10.1] 心跳 system 层也注入重逢感知 ──
                {"role": "system", "content": reunion_context},
                {"role": "user",   "content": heartbeat_prompt}
            ]

            raw_res = await BrainInterpreter.request_ai(messages)
            if not raw_res or "[NONE]" in raw_res:
                audit.log("INFO", "SOCIAL", f"心跳结果：淑雪觉得现在没什么想跟 {tname} 说的。")
                if t_is_group:
                    await db.mark_group_bubble(tid, tname)
                return

            clean_text = BrainInterpreter._RE_CLEAN.sub("", raw_res)
            clean_text = BrainInterpreter._RE_LEAD.sub("", clean_text).strip()
            segments   = [s.strip() for s in clean_text.split("[CONTINUE]") if s.strip()]

            if segments:
                audit.log("SOC", "HEARTBEAT", f"淑雪主动找 {tname} 说话 -> {segments[0][:30]}...")
                sent_segments = []
                used_temp_private = False
                for seg in segments:
                    seg = BrainInterpreter._sanitize_network_style(seg)
                    if not seg:
                        continue
                    sent_segments.append(seg)
                    rendered = BrainInterpreter._RE_IMG.sub(
                        lambda m: AssetManager.convert_to_cq(m.group(1)), seg
                    )
                    if direct_user_id:
                        sent = False
                        if hasattr(self.bot, "send_temp_private"):
                            sent = await self.bot.send_temp_private(direct_user_id, tid, rendered)
                        if not sent:
                            audit.log(
                                "WARN", "SOCIAL",
                                f"群友主动私聊触达失败，已放弃回群打扰 user={direct_user_id} group={tid}"
                            )
                        else:
                            used_temp_private = True
                    else:
                        await self.bot.send_direct(tid, rendered, t_is_group)
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                if direct_user_id:
                    await db.mark_group_member_contact_attempt(tid, direct_user_id)
                    if used_temp_private:
                        await db.save_chat_node(
                            direct_user_id, "assistant", " ".join(sent_segments),
                            target_type="private"
                        )
                else:
                    await db.save_chat_node(
                        tid, "assistant", " ".join(sent_segments),
                        target_type="group" if t_is_group else "private"
                    )
                if t_is_group:
                    await db.mark_group_bubble(tid, tname)

        except Exception:
            audit.log("ERROR", "SOCIAL", f"心跳异常: {traceback.format_exc()}")

    # ── [NEW] 拉取过去24小时全部对话记录 ──
    async def fetch_last_24h_logs(self) -> str:
        try:
            conn = await db._get_conn()
            cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            async with conn.execute(
                """SELECT target_id, role, content, timestamp
                   FROM history
                   WHERE timestamp > ?
                   ORDER BY timestamp ASC""",
                (cutoff,)
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return ""

            lines = []
            for target_id, role, msg_content, timestamp in rows:
                if role == "assistant":
                    who = "淑雪"
                else:
                    # target_id 格式为 p:12345 或 g:12345，解码后取更易读的形式
                    _, decoded_id = db._decode_history_key(target_id)
                    who = decoded_id
                text = re.sub(r"\s+", " ", str(msg_content or "")).strip()
                if len(text) > 120:
                    text = text[:117] + "..."
                # SQLite CURRENT_TIMESTAMP 以 UTC 存储，这里转为本地时区后再写入反思提示词。
                try:
                    utc_dt = datetime.strptime(str(timestamp), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    parsed_dt = datetime.fromisoformat(str(timestamp))
                    utc_dt = parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone()
                lines.append(f"[{local_dt.strftime('%Y-%m-%d %H:%M:%S')}] {who}: {text}")
            return "\n".join(lines)
        except Exception as e:
            audit.log("ERROR", "EVOLVE", f"fetch_last_24h_logs 异常: {e}")
            return ""

    # ── [NEW] 夜间演化：让AI对一天的对话进行自我反思 ──
    async def nightly_evolution(self, today_date):
        audit.log("INFO", "EVOLVE", f"开始夜间演化（{today_date}）...")
        try:
            chat_logs = await self.fetch_last_24h_logs()
            if not chat_logs or len(chat_logs.split("\n")) < 5:
                audit.log("INFO", "EVOLVE", "今日交互过少（< 5条），跳过演化。")
                return

            evolve_prompt = f"""以下是今天（{today_date}）淑雪与各人的全部对话记录：

{chat_logs}

---
请你以淑雪的第一人称视角，对今天的经历进行深夜反思，输出严格符合以下格式的 JSON（不要有任何其他内容）：
{{
  "daily_summary": "用1-2句话总结今天整体发生了什么（对话内容、情绪起伏）",
  "personality_shift": "今天的互动让我在性格/态度上产生了什么微妙变化？（1句话，例如：更愿意主动分享心情了）",
  "impression": "今天哥哥给我留下的最新印象是什么？（1句话，要具体，例如：哥哥今天心情不好，我要多关心他）"
}}
只输出 JSON，不要 Markdown 代码块，不要解释。"""

            messages = [
                {"role": "system", "content": PersonalityCore.get_dynamic_prompt()},
                {"role": "user", "content": evolve_prompt}
            ]

            raw_res = await BrainInterpreter.request_ai(messages, max_tokens=400)
            if not raw_res:
                audit.log("WARN", "EVOLVE", "AI未返回有效内容，跳过演化。")
                return

            # 容错：去掉 Markdown 代码块包裹
            clean_res = raw_res.strip()
            clean_res = re.sub(r"^```(?:json)?\s*", "", clean_res)
            clean_res = re.sub(r"\s*```$", "", clean_res).strip()

            try:
                evo = json.loads(clean_res)
            except json.JSONDecodeError as je:
                audit.log("ERROR", "EVOLVE", f"JSON解析失败: {je} | 原始内容: {clean_res[:200]}")
                return

            # 校验必要字段
            required = {"daily_summary", "personality_shift", "impression"}
            if not required.issubset(evo.keys()):
                audit.log("ERROR", "EVOLVE", f"AI返回字段不完整: {list(evo.keys())}")
                return

            conn = await db._get_conn()
            await conn.execute(
                """INSERT OR REPLACE INTO BotEvolution
                   (date, daily_summary, personality_shift, impression_on_user, last_evolve_ts)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(today_date),
                    evo["daily_summary"],
                    evo["personality_shift"],
                    evo["impression"],
                    time.time()
                )
            )
            await conn.commit()
            audit.log("SUCCESS", "EVOLVE", f"夜间演化完成 → {evo['personality_shift'][:40]}")

            # 同步写入向量记忆，让演化结果参与后续语义检索
            summary_text = (
                f"[夜间反思 {today_date}] {evo['daily_summary']} "
                f"性格微调：{evo['personality_shift']} "
                f"对哥哥印象：{evo['impression']}"
            )
            asyncio.create_task(
                db.store_vector_memory(
                    "self_evolution", summary_text,
                    mem_type="evolution", nickname="淑雪自我反思"
                )
            )

        except Exception:
            audit.log("ERROR", "EVOLVE", f"nightly_evolution 异常: {traceback.format_exc()}")


# ==========================================
# 9. 指令注册与逻辑路由
# ==========================================

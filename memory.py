"""记忆与持久化层。

负责 SQLite 结构、聊天历史、用户画像、群聊状态和向量记忆。
模块底部暴露单例 `db` 供其他模块使用。
"""

from .shared import *
from .shared import _CHROMA_AVAILABLE, _get_http_session

class MemoryEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        # [v10.2] ChromaDB 延迟到首次异步调用时初始化（避免同步阻塞）
        self._chroma_collection = None
        self._chroma_ready      = False
        # [FIX v10.3] 并发锁：防止启动瞬间多条消息同时触发 _ensure_chroma 导致文件锁冲突
        self._chroma_lock: asyncio.Lock | None = None
        # [FIX v10.4] 固化防抖：记录每个 tid 上次固化时间，间隔 < 300s 跳过
        self._last_consolidate: dict[str, float] = {}
        self._repair_lock: asyncio.Lock | None = None
        self._last_integrity_check = 0.0
        self._init_db_sync()

    @staticmethod
    def _is_corruption_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            isinstance(exc, sqlite3.DatabaseError)
            or exc.__class__.__name__ == "DatabaseError"
        ) and any(
            key in msg
            for key in (
                "database disk image is malformed",
                "file is not a database",
                "database corruption",
                "malformed",
                "not a database",
            )
        )

    def _quarantine_corrupt_db_sync(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if not os.path.exists(path):
                continue
            backup = f"{path}.corrupt.{ts}"
            try:
                shutil.move(path, backup)
                audit.log("ERROR", "DB", f"检测到数据库损坏，已隔离备份: {backup}")
            except Exception as e:
                audit.log("ERROR", "DB", f"数据库损坏文件隔离失败 {path}: {e}")

    def _init_db_sync(self, allow_repair: bool = True):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if allow_repair:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if row and str(row[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(f"sqlite quick_check failed: {row[0]}")
        except Exception as e:
            conn.close()
            if allow_repair and self._is_corruption_error(e):
                self._quarantine_corrupt_db_sync()
                return self._init_db_sync(allow_repair=False)
            raise
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            nickname TEXT,
            favor INTEGER DEFAULT 10,
            mood INTEGER DEFAULT 50,
            impression TEXT DEFAULT '初相识',
            facts TEXT DEFAULT '[]',
            first_meet DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_active REAL,
            total_interaction INTEGER DEFAULT 0
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            group_name TEXT,
            proactive_mode INTEGER DEFAULT 0,
            ad_kick_mode INTEGER DEFAULT 0,
            last_bubble REAL DEFAULT 0,
            last_msg TEXT DEFAULT NULL
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            nickname TEXT,
            group_name TEXT,
            join_time REAL DEFAULT 0,
            last_seen REAL DEFAULT 0,
            seen_count INTEGER DEFAULT 0,
            last_contact_attempt REAL DEFAULT 0,
            PRIMARY KEY (group_id, user_id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS group_ad_suspects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT DEFAULT 'suspect',
            reason TEXT DEFAULT '',
            created_at REAL NOT NULL
        )''')

        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN last_msg TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN ad_kick_mode INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE group_members ADD COLUMN seen_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE group_members ADD COLUMN join_time REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE group_members ADD COLUMN last_contact_attempt REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        cursor.execute(
            "UPDATE group_members SET seen_count=1 WHERE COALESCE(seen_count, 0)=0 AND COALESCE(last_seen, 0)>0"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_ad_suspects_user_time ON group_ad_suspects(group_id, user_id, created_at)"
        )

        cursor.execute('''CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS sys_meta (
            key TEXT PRIMARY KEY,
            val TEXT
        )''')

        # [NEW] 夜间演化记录表：每天凌晨3点AI自我反思结果
        cursor.execute('''CREATE TABLE IF NOT EXISTS BotEvolution (
            date TEXT PRIMARY KEY,
            daily_summary TEXT,
            personality_shift TEXT,
            impression_on_user TEXT,
            last_evolve_ts REAL DEFAULT (strftime('%s','now'))
        )''')

        # [仿生记忆 v11] 语义事实记忆表：实体级唯一，新值自动覆盖旧值（防精神分裂）
        cursor.execute('''CREATE TABLE IF NOT EXISTS semantic_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, entity_key)
        )''')

        # [仿生记忆 v11] 用户元记忆表：关系阶段与情感基线常驻，防止情绪断层
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_meta_state (
            user_id TEXT PRIMARY KEY,
            relationship_stage TEXT DEFAULT '初相识',
            emotional_baseline TEXT DEFAULT '',
            last_notable_event TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
        conn.close()
        audit.log("SUCCESS", "DB", "数据库全量表结构校验通过（同步初始化完成）。")

    async def _reset_corrupt_db(self, reason: Exception | str):
        if self._repair_lock is None:
            self._repair_lock = asyncio.Lock()
        async with self._repair_lock:
            audit.log("ERROR", "DB", f"数据库疑似损坏，准备隔离并重建: {reason}")
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
                self._conn = None
            await asyncio.to_thread(self._quarantine_corrupt_db_sync)
            await asyncio.to_thread(self._init_db_sync, False)
            self._last_integrity_check = time.time()

    async def _get_conn(self) -> aiosqlite.Connection:
        # [FIX] 探活：连接失效时自动重建，不再需要重启恢复
        # [FIX-CURSOR] async with 包裹 execute()，cursor 立即释放，防止长期运行泄漏
        if self._conn is not None:
            try:
                async with self._conn.execute("SELECT 1"):
                    pass
            except Exception as e:
                if self._is_corruption_error(e):
                    await self._reset_corrupt_db(e)
                else:
                    audit.log("WARN", "DB", "数据库连接已失效，正在重建...")
                    try:
                        await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            async with self._conn.execute("PRAGMA journal_mode=WAL"):
                pass
        if time.time() - self._last_integrity_check > 60:
            try:
                async with self._conn.execute("PRAGMA quick_check") as cursor:
                    row = await cursor.fetchone()
                self._last_integrity_check = time.time()
                if row and str(row[0]).lower() != "ok":
                    await self._reset_corrupt_db(f"sqlite quick_check failed: {row[0]}")
                    self._conn = await aiosqlite.connect(self.db_path)
                    async with self._conn.execute("PRAGMA journal_mode=WAL"):
                        pass
            except Exception as e:
                if self._is_corruption_error(e):
                    await self._reset_corrupt_db(e)
                    self._conn = await aiosqlite.connect(self.db_path)
                    async with self._conn.execute("PRAGMA journal_mode=WAL"):
                        pass
                else:
                    audit.log("WARN", "DB", f"数据库完整性检查失败，暂不重建: {e}")
        return self._conn

    async def sync_user(self, uid, nick, favor_delta=0, mood_delta=0, facts_json=None):
        uid = str(uid)
        now = time.time()
        conn = await self._get_conn()

        async with conn.execute(
            "SELECT favor, mood, facts FROM users WHERE user_id = ?", (uid,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            init_favor = 10 + favor_delta
            await conn.execute(
                '''INSERT INTO users
                   (user_id, nickname, favor, mood, facts, last_active, total_interaction)
                   VALUES (?, ?, ?, ?, ?, ?, 1)''',
                (uid, nick, init_favor, 50, facts_json or "[]", now)
            )
            await conn.commit()
            return init_favor, 50, facts_json or "[]"
        else:
            new_favor   = max(-100, min(100, row[0] + favor_delta))
            new_mood    = max(0,    min(100, row[1] + mood_delta))
            final_facts = facts_json if facts_json is not None else row[2]
            await conn.execute(
                '''UPDATE users SET nickname=?, favor=?, mood=?, facts=?,
                   last_active=?, total_interaction=total_interaction+1
                   WHERE user_id=?''',
                (nick, new_favor, new_mood, final_facts, now, uid)
            )
            await conn.commit()
            return new_favor, new_mood, final_facts

    async def get_user_last_active(self, uid) -> float | None:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT last_active FROM users WHERE user_id = ?",
            (str(uid),)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] in (None, ""):
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    async def get_user_profile(self, uid) -> dict | None:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT user_id, nickname, favor, mood, facts, last_active, total_interaction FROM users WHERE user_id=?",
            (str(uid),)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "nickname": row[1],
            "favor": row[2],
            "mood": row[3],
            "facts": row[4] or "[]",
            "last_active": row[5],
            "total_interaction": row[6] or 0,
        }

    @staticmethod
    def _history_key(target_id: str, target_type: str = "private") -> str:
        raw = str(target_id)
        if raw.startswith(("p:", "g:")):
            return raw
        return f"{'g' if target_type == 'group' else 'p'}:{raw}"

    @staticmethod
    def _decode_history_key(stored_key: str) -> tuple[str | None, str]:
        raw = str(stored_key)
        if raw.startswith("p:"):
            return "private", raw[2:]
        if raw.startswith("g:"):
            return "group", raw[2:]
        return None, raw

    def _history_lookup_keys(self, target_id: str, target_type: str = "private") -> list[str]:
        raw = str(target_id)
        preferred = self._history_key(raw, target_type)
        keys = [preferred]
        if raw != preferred:
            keys.append(raw)
        return keys

    async def _fetch_history_rows_for_key(self, history_key: str, limit: int) -> list:
        conn = await self._get_conn()
        async with conn.execute(
            '''SELECT role, content FROM (
                   SELECT role, content, id
                   FROM history
                   WHERE target_id = ?
                   ORDER BY id DESC
                   LIMIT ?
               ) ORDER BY id ASC''',
            (history_key, limit)
        ) as cursor:
            return await cursor.fetchall()

    async def _fetch_history_rows(self, target_id: str, target_type: str = "private", limit: int = 50) -> tuple[str, list]:
        for history_key in self._history_lookup_keys(target_id, target_type):
            rows = await self._fetch_history_rows_for_key(history_key, limit)
            if rows:
                return history_key, rows
        return self._history_key(target_id, target_type), []

    async def _load_name_map(self, table: str, id_col: str, name_col: str, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        conn = await self._get_conn()
        placeholders = ",".join("?" for _ in ids)
        query = f"SELECT {id_col}, {name_col} FROM {table} WHERE {id_col} IN ({placeholders})"
        async with conn.execute(query, tuple(ids)) as cursor:
            rows = await cursor.fetchall()
        return {str(row[0]): str(row[1]) for row in rows if row and row[1]}

    async def save_chat_node(self, tid: str, role: str, content: str, target_type: str = "private"):
        history_key = self._history_key(tid, target_type)
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO history (target_id, role, content) VALUES (?, ?, ?)",
            (history_key, role, content)
        )
        await conn.execute(
            '''DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history WHERE target_id = ? ORDER BY id DESC LIMIT 60
            ) AND target_id = ?''',
            (history_key, history_key)
        )
        await conn.commit()

    async def fetch_context(
        self,
        tid: str,
        limit: int = GlobalConfig.MAX_HISTORY_PER_USER,
        target_type: str = "private",
    ):
        _, rows = await self._fetch_history_rows(tid, target_type=target_type, limit=limit)
        return [{"role": r[0], "content": r[1]} for r in rows]

    async def get_cross_person_recent_context(
        self,
        current_tid: str,
        current_target_type: str = "private",
        limit_targets: int = 4,
        per_target_msgs: int = 6,
    ) -> str:
        exclude_keys = self._history_lookup_keys(current_tid, current_target_type)
        conn = await self._get_conn()
        placeholders = ",".join("?" for _ in exclude_keys)

        async with conn.execute(
            f'''SELECT target_id, MAX(id) AS last_id
                FROM history
                WHERE target_id NOT IN ({placeholders})
                GROUP BY target_id
                ORDER BY last_id DESC
                LIMIT ?''',
            (*exclude_keys, limit_targets)
        ) as cursor:
            target_rows = await cursor.fetchall()

        if not target_rows:
            return "【其他人的最近对话】数据库里没有可参考的其他会话。"

        decoded_targets = []
        user_ids = set()
        group_ids = set()
        for history_key, _ in target_rows:
            target_type, target_id = self._decode_history_key(history_key)
            decoded_targets.append((str(history_key), target_type, target_id))
            if target_type == "group":
                group_ids.add(target_id)
            elif target_type == "private":
                user_ids.add(target_id)
            else:
                user_ids.add(target_id)
                group_ids.add(target_id)

        user_names = await self._load_name_map("users", "user_id", "nickname", user_ids)
        group_names = await self._load_name_map("groups", "group_id", "group_name", group_ids)

        sections = ["【其他人的最近对话】下面是你和其他人的最近聊天片段。若当前有人问你刚刚在做什么、和谁聊了什么，可以优先参考这里。"]
        for history_key, target_type, target_id in decoded_targets:
            if target_type == "group":
                label = group_names.get(target_id) or user_names.get(target_id) or target_id
            else:
                label = user_names.get(target_id) or group_names.get(target_id) or target_id

            msg_rows = await self._fetch_history_rows_for_key(history_key, per_target_msgs)
            if not msg_rows:
                continue

            rendered_lines = []
            for role, content in msg_rows:
                who = "你" if role == "assistant" else label
                text = re.sub(r"\s+", " ", str(content or "")).strip()
                if len(text) > 80:
                    text = text[:77] + "..."
                rendered_lines.append(f"{who}: {text}")

            sections.append(f"- 和 {label} 的最近对话：{' | '.join(rendered_lines)}")

        return "\n".join(sections)

    async def _label_for_target(self, target_id: str, target_type: str | None = None) -> str:
        conn = await self._get_conn()
        target_type_from_key, decoded_id = self._decode_history_key(target_id)
        target_type = target_type or target_type_from_key
        target_id = decoded_id

        if target_type != "group":
            async with conn.execute(
                "SELECT nickname FROM users WHERE user_id = ?",
                (target_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0]:
                return row[0]

        async with conn.execute(
            "SELECT group_name FROM groups WHERE group_id = ?",
            (target_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return row[0]

        if target_type == "group":
            return target_id

        async with conn.execute(
            "SELECT nickname FROM users WHERE user_id = ?",
            (target_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return row[0]

        return target_id

    async def get_recent_other_target(self, current_tid: str, current_target_type: str = "private") -> dict | None:
        exclude_keys = self._history_lookup_keys(current_tid, current_target_type)
        conn = await self._get_conn()
        placeholders = ",".join("?" for _ in exclude_keys)
        async with conn.execute(
            f'''SELECT target_id, MAX(id) AS last_id
                FROM history
                WHERE target_id NOT IN ({placeholders})
                GROUP BY target_id
                ORDER BY last_id DESC
                LIMIT 1''',
            tuple(exclude_keys)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        history_key = str(row[0])
        target_type, target_id = self._decode_history_key(history_key)
        label = await self._label_for_target(target_id, target_type=target_type)
        return {
            "target_id": target_id,
            "target_type": target_type or "private",
            "history_key": history_key,
            "label": label,
            "explicit": False,
            "source": "recent_other_target",
        }

    async def resolve_referenced_target(
        self, raw_input: str, current_tid: str, current_target_type: str = "private"
    ) -> dict | None:
        text = str(raw_input or "").strip()
        current_tid = str(current_tid)
        if not text:
            return await self.get_recent_other_target(current_tid, current_target_type=current_target_type)

        lowered = text.lower()
        if "哥哥" in text:
            return {
                "target_id": str(GlobalConfig.ADMIN_UID),
                "label": "duo0621",
                "explicit": True,
                "source": "alias:哥哥",
            }

        conn = await self._get_conn()
        candidates = []

        async with conn.execute(
            "SELECT user_id, nickname FROM users WHERE user_id != ? AND nickname IS NOT NULL AND nickname != ''",
            (current_tid,)
        ) as cursor:
            async for row in cursor:
                candidates.append(("user", str(row[0]), str(row[1])))

        async with conn.execute(
            "SELECT group_id, group_name FROM groups WHERE group_id != ? AND group_name IS NOT NULL AND group_name != ''",
            (current_tid,)
        ) as cursor:
            async for row in cursor:
                candidates.append(("group", str(row[0]), str(row[1])))

        best = None
        for kind, target_id, name in candidates:
            clean_name = str(name).strip()
            if len(clean_name) < 2:
                continue
            if clean_name.lower() not in lowered and clean_name not in text:
                continue

            score = len(clean_name) * 10
            if text.startswith(clean_name) or text.endswith(clean_name):
                score += 5
            if "和" + clean_name in text or "跟" + clean_name in text:
                score += 5

            candidate = {
                "target_id": target_id,
                "label": clean_name,
                "target_type": kind,
                "explicit": True,
                "source": f"name_match:{clean_name}",
                "score": score,
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate

        if best:
            best.pop("score", None)
            return best

        return await self.get_recent_other_target(current_tid, current_target_type=current_target_type)

    async def fetch_recent_dialogue_for_target(
        self, target_id: str, target_type: str = "private", limit: int = 6
    ) -> dict | None:
        target_id = str(target_id)
        label = await self._label_for_target(target_id, target_type=target_type)
        _, rows = await self._fetch_history_rows(target_id, target_type=target_type, limit=limit)

        if not rows:
            return None

        messages = []
        rendered_lines = []
        combined_text_parts = []
        partner_text_parts = []   # [FIX] 仅收集对方说的内容，供敏感度判断使用
        for role, content in rows:
            content = re.sub(r"\s+", " ", str(content or "")).strip()
            speaker = "你" if role == "assistant" else label
            messages.append({"role": role, "content": content})
            rendered_lines.append(f"{speaker}: {content}")
            combined_text_parts.append(content)
            if role != "assistant":          # 只收集对方（user）的消息
                partner_text_parts.append(content)

        return {
            "target_id": target_id,
            "target_type": target_type,
            "label": label,
            "messages": messages,
            "rendered": " | ".join(rendered_lines),
            "combined_text": " ".join(combined_text_parts),
            # [FIX] 仅对方发言的文本，敏感度判断应只看这里，避免淑雪自己的口头语误触发
            "partner_text": " ".join(partner_text_parts),
        }

    async def reset_memory(self, tid, target_type: str = "private"):
        conn = await self._get_conn()
        lookup_keys = self._history_lookup_keys(tid, target_type)
        placeholders = ",".join("?" for _ in lookup_keys)
        await conn.execute(f"DELETE FROM history WHERE target_id IN ({placeholders})", tuple(lookup_keys))
        await conn.commit()

    async def save_group_last_msg(self, gid: str, nickname: str, text: str, user_id: str | None = None):
        payload = json.dumps(
            {"nickname": nickname, "user_id": str(user_id or ""), "text": text},
            ensure_ascii=False
        )
        conn = await self._get_conn()
        await conn.execute(
            "INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?, ?)",
            (gid, f"群{gid}")
        )
        await conn.execute(
            "UPDATE groups SET last_msg=? WHERE group_id=?", (payload, gid)
        )
        await conn.commit()

    async def get_group_last_msg(self, gid: str) -> dict | None:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT last_msg FROM groups WHERE group_id=?", (gid,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    async def get_group_name(self, gid: str) -> str | None:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT group_name FROM groups WHERE group_id=?", (gid,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def set_group_social(self, gid: str, enabled: bool, group_name: str | None = None):
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO groups (group_id, group_name, proactive_mode)
               VALUES (?, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   group_name=COALESCE(excluded.group_name, groups.group_name),
                   proactive_mode=excluded.proactive_mode""",
            (gid, group_name or f"群{gid}", 1 if enabled else 0)
        )
        await conn.commit()

    async def save_group_member_seen(self, gid: str, uid: str, nickname: str, group_name: str | None = None):
        conn = await self._get_conn()
        params = (str(gid), str(uid), nickname or str(uid), group_name, time.time())
        sql = """INSERT INTO group_members (group_id, user_id, nickname, group_name, last_seen, seen_count)
                 VALUES (?, ?, ?, ?, ?, 1)
                 ON CONFLICT(group_id, user_id) DO UPDATE SET
                     nickname=excluded.nickname,
                     group_name=COALESCE(excluded.group_name, group_members.group_name),
                     last_seen=excluded.last_seen,
                     seen_count=COALESCE(group_members.seen_count, 0)+1"""
        try:
            await conn.execute(sql, params)
            await conn.commit()
        except Exception as e:
            if not self._is_corruption_error(e):
                raise
            await self._reset_corrupt_db(e)
            conn = await self._get_conn()
            await conn.execute(sql, params)
            await conn.commit()

    async def save_group_member_join_time(
        self, gid: str, uid: str, join_time: float,
        nickname: str | None = None, group_name: str | None = None
    ):
        if not gid or not uid:
            return
        try:
            join_ts = float(join_time or 0)
        except (TypeError, ValueError):
            join_ts = 0.0
        if join_ts <= 0:
            return
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO group_members (group_id, user_id, nickname, group_name, join_time, last_seen, seen_count)
               VALUES (?, ?, ?, ?, ?, 0, 0)
               ON CONFLICT(group_id, user_id) DO UPDATE SET
                   nickname=COALESCE(excluded.nickname, group_members.nickname),
                   group_name=COALESCE(excluded.group_name, group_members.group_name),
                   join_time=CASE
                       WHEN COALESCE(group_members.join_time, 0) <= 0 THEN excluded.join_time
                       WHEN excluded.join_time > 0 AND excluded.join_time < group_members.join_time THEN excluded.join_time
                       ELSE group_members.join_time
                   END""",
            (str(gid), str(uid), nickname or None, group_name or None, join_ts)
        )
        await conn.commit()

    async def get_group_member_join_time(self, gid: str, uid: str) -> float:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT join_time FROM group_members WHERE group_id=? AND user_id=?",
            (str(gid), str(uid))
        ) as cursor:
            row = await cursor.fetchone()
        try:
            return float(row[0] or 0) if row else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def count_group_ad_suspects(self, gid: str, uid: str, since_ts: float) -> int:
        conn = await self._get_conn()
        async with conn.execute(
            """SELECT COUNT(*) FROM group_ad_suspects
               WHERE group_id=? AND user_id=? AND created_at>=?""",
            (str(gid), str(uid), float(since_ts))
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def record_group_ad_suspect(self, gid: str, uid: str, action: str, reason: str = ""):
        conn = await self._get_conn()
        now = time.time()
        await conn.execute(
            """INSERT INTO group_ad_suspects (group_id, user_id, action, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(gid), str(uid), str(action or "suspect")[:32], str(reason or "")[:300], now)
        )
        await conn.execute(
            "DELETE FROM group_ad_suspects WHERE created_at<?",
            (now - 86400,)
        )
        await conn.commit()

    async def get_recent_group_for_user(self, uid: str) -> dict | None:
        conn = await self._get_conn()
        async with conn.execute(
            """SELECT group_id, group_name, nickname, last_seen, seen_count
               FROM group_members
               WHERE user_id=?
               ORDER BY last_seen DESC
               LIMIT 1""",
            (str(uid),)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "group_id": row[0],
            "group_name": row[1] or f"群{row[0]}",
            "nickname": row[2] or str(uid),
            "last_seen": row[3] or 0,
            "seen_count": row[4] or 0,
        }

    async def enable_group_social(self, gid: str, group_name: str | None = None):
        await self.set_group_social(gid, True, group_name=group_name)

    async def disable_group_social(self, gid: str, group_name: str | None = None):
        await self.set_group_social(gid, False, group_name=group_name)

    async def get_group_social_status(self, gid: str) -> dict:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT group_name, proactive_mode, last_bubble FROM groups WHERE group_id=?",
            (gid,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {"group_name": f"群{gid}", "enabled": False, "last_bubble": 0}
        return {
            "group_name": row[0] or f"群{gid}",
            "enabled": bool(row[1]),
            "last_bubble": float(row[2] or 0),
        }

    async def set_group_ad_kick(self, gid: str, enabled: bool, group_name: str | None = None):
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO groups (group_id, group_name, ad_kick_mode)
               VALUES (?, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   group_name=COALESCE(excluded.group_name, groups.group_name),
                   ad_kick_mode=excluded.ad_kick_mode""",
            (gid, group_name or f"群{gid}", 1 if enabled else 0)
        )
        await conn.commit()

    async def get_group_ad_kick_status(self, gid: str) -> dict:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT group_name, ad_kick_mode FROM groups WHERE group_id=?",
            (gid,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {"group_name": f"群{gid}", "enabled": False}
        return {"group_name": row[0] or f"群{gid}", "enabled": bool(row[1])}

    async def is_group_ad_kick_enabled(self, gid: str) -> bool:
        return (await self.get_group_ad_kick_status(gid))["enabled"]

    async def mark_group_bubble(self, gid: str, group_name: str | None = None):
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO groups (group_id, group_name, last_bubble)
               VALUES (?, ?, ?)
               ON CONFLICT(group_id) DO UPDATE SET
                   group_name=COALESCE(excluded.group_name, groups.group_name),
                   last_bubble=excluded.last_bubble""",
            (gid, group_name or f"群{gid}", time.time())
        )
        await conn.commit()

    async def mark_group_member_contact_attempt(self, gid: str, uid: str):
        conn = await self._get_conn()
        await conn.execute(
            """UPDATE group_members
               SET last_contact_attempt=?
               WHERE group_id=? AND user_id=?""",
            (time.time(), str(gid), str(uid))
        )
        await conn.commit()

    async def get_heartbeat_candidates(self) -> list[dict]:
        now = time.time()
        candidates = []
        conn = await self._get_conn()
        async with conn.execute(
            """SELECT u.user_id, u.nickname, u.favor, u.total_interaction,
                      gm.group_id, gm.group_name, gm.last_seen, gm.last_contact_attempt
               FROM users u
               LEFT JOIN (
                   SELECT gm.user_id, gm.group_id, COALESCE(g.group_name, gm.group_name) AS group_name,
                          MAX(gm.last_seen) AS last_seen,
                          MAX(COALESCE(gm.last_contact_attempt, 0)) AS last_contact_attempt
                   FROM group_members gm
                   JOIN groups g ON g.group_id = gm.group_id AND g.proactive_mode = 1
                   GROUP BY user_id
               ) gm ON gm.user_id = u.user_id
               WHERE u.favor >= 15
                 AND u.total_interaction >= 3
                 AND (gm.group_id IS NULL OR (? - COALESCE(gm.last_contact_attempt, 0)) > 21600)""",
            (now,)
        ) as cursor:
            async for r in cursor:
                candidates.append({
                    "type": "private",
                    "id": r[0],
                    "name": r[1],
                    "favor": r[2],
                    "total_interaction": r[3],
                    "source_group_id": r[4],
                    "source_group_name": r[5],
                    "source_group_seen": r[6] or 0,
                })
        async with conn.execute(
            """SELECT gm.user_id, gm.nickname, gm.group_id, COALESCE(g.group_name, gm.group_name),
                      gm.last_seen, COALESCE(gm.seen_count, 0)
               FROM group_members gm
               JOIN groups g ON g.group_id = gm.group_id AND g.proactive_mode = 1
               LEFT JOIN users u ON u.user_id = gm.user_id
               WHERE u.user_id IS NULL
                 AND gm.user_id != ?
                 AND COALESCE(gm.seen_count, 0) >= 2
                 AND (? - COALESCE(gm.last_contact_attempt, 0)) > 21600
                 AND (? - COALESCE(gm.last_seen, 0)) < 1209600""",
            (GlobalConfig.ADMIN_UID, now, now)
        ) as cursor:
            async for r in cursor:
                candidates.append({
                    "type": "private",
                    "id": str(r[0]),
                    "name": r[1] or str(r[0]),
                    "favor": 15,
                    "total_interaction": r[5] or 0,
                    "source_group_id": r[2],
                    "source_group_name": r[3] or f"群{r[2]}",
                    "source_group_seen": r[4] or 0,
                })
        async with conn.execute(
            "SELECT group_id, group_name FROM groups WHERE proactive_mode = 1 AND (? - last_bubble) > 10800",
            (now,)
        ) as cursor:
            async for r in cursor:
                candidates.append({"type": "group", "id": r[0], "name": r[1], "favor": 50})
        return candidates

    async def get_last_chat_time(self, tid: str) -> str:
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT timestamp FROM history WHERE target_id=? ORDER BY id DESC LIMIT 1",
            (tid,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else "从未对话"

    async def get_social_targets(self) -> list[dict]:
        now = time.time()
        candidates = []
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT user_id, nickname FROM users WHERE favor > 20 AND (? - last_active) > 21600",
            (now,)
        ) as cursor:
            async for r in cursor:
                candidates.append({"type": "private", "id": r[0], "name": r[1]})
        async with conn.execute(
            "SELECT group_id, group_name FROM groups WHERE proactive_mode = 1 AND (? - last_bubble) > 10800",
            (now,)
        ) as cursor:
            async for r in cursor:
                candidates.append({"type": "group", "id": r[0], "name": r[1]})
        return candidates

    # ══════════════════════════════════════════════════
    # [NEW v10.1] 时间流逝感核心方法
    # ══════════════════════════════════════════════════

    async def get_reunion_context(
        self, tid: str, nickname: str, favor: int, target_type: str = "private",
        last_active_ts: float | None = None
    ) -> str:
        """
        计算距上次对话的时间差，生成对应的情感语境描述。
        结果注入 System Prompt，驱动淑雪表现出真实的"等待感"与"重逢感"。

        设计原则：
        - 好感度高 + 久未联系 → 委屈/思念
        - 好感度低 + 久未联系 → 淡漠/陌生
        - 刚刚在聊 → 对话连贯热乎
        - 第一次见面 → 初见羞涩或好奇
        """
        history_key, _ = await self._fetch_history_rows(tid, target_type=target_type, limit=1)
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT timestamp FROM history WHERE target_id=? ORDER BY id DESC LIMIT 1",
            (history_key,)
        ) as cursor:
            row = await cursor.fetchone()

        # ── 第一次见面 ──
        if not row:
            return "【重逢感知】你们是第一次对话，你对这个人有点好奇，略带羞涩。"

        # ── 解析时间戳（兼容 SQLite DATETIME 格式） ──
        try:
            last_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                last_time = datetime.fromisoformat(row[0])
            except Exception:
                return ""

        # [FIX v10.5-tz] SQLite CURRENT_TIMESTAMP 存储的是 UTC 时间，
        # 必须用 datetime.utcnow() 对比，否则在 UTC+8 (CST) 环境下
 
        delta = datetime.utcnow() - last_time
        total_seconds = int(delta.total_seconds())
        days          = delta.days

        audit.log(
            "INFO", "TIMESENSE",
            f"[{nickname}] 距上次对话: {days}天 {total_seconds % 86400 // 3600}小时"
        )

        # ── 根据时间跨度 + 好感度生成语境 ──

        # 5分钟内：对话还热乎
        if total_seconds < 300:
            return "【重逢感知】你们刚才还在聊，思路完全连贯，不需要任何寒暄。"

        # 5~30分钟：短暂离开
        if total_seconds < 1800:
            return f"【重逢感知】你们{total_seconds // 60}分钟前还在聊，刚离开一下，感觉无缝衔接。"

        # 30分钟~3小时：中断了一段时间
        if total_seconds < 10800:
            hours_ago = total_seconds // 3600
            mins_ago  = (total_seconds % 3600) // 60
            time_str  = f"{hours_ago}小时{mins_ago}分钟" if hours_ago else f"{mins_ago}分钟"
            return (
                f"【重逢感知】距上次聊天约{time_str}，"
                "中间断开了一会儿，打招呼时可以自然地提一下这个空档，比如'刚刚去哪了'。"
            )

        # 3小时~当天：今天还没怎么聊
        if days == 0:
            if favor >= 40:
                moods = [
                    "你今天还没怎么和他聊，有点惦记他在干嘛。",
                    "隔了好几个小时，你有点想他了，见到他消息会有点小开心。",
                    "今天断了好一段时间，你以为他忙去了，没想到他找来了，有点惊喜。"
                ]
            else:
                moods = [
                    "今天还没聊过，态度正常，没有特别感觉。",
                    "隔了几个小时，正常继续聊就好。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 昨天聊过
        if days == 1:
            if favor >= 50:
                moods = [
                    "昨天聊过，今天再见有自然的亲切感，见到他的消息会嘴角上扬。",
                    "昨天聊得挺开心的，今天再见心里暖暖的。",
                    "昨天刚聊过，今天看到他又来了，小小高兴一下。"
                ]
            else:
                moods = [
                    "昨天聊过，今天再见，态度平淡自然。",
                    "昨天见过，今天继续，没什么特别感觉。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 2~3天
        if days <= 3:
            if favor >= 40:
                moods = [
                    f"已经{days}天没说话了，你有点想他，见到他消息会有点不知所措但又高兴。",
                    f"{days}天没聊了，你偷偷想过他几次，但没主动找他，现在他来了你松了口气。",
                    f"好几天没见了，你嘴上不说，心里其实有点想他。"
                ]
            else:
                moods = [
                    f"断了{days}天，正常回复就好，没什么特别情绪。",
                    f"有{days}天没聊，稍微有些陌生感，但不影响正常对话。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 4~6天
        if days <= 6:
            if favor >= 50:
                moods = [
                    f"整整{days}天没有联系，你有点委屈，但见到他还是忍不住高兴，可能会阴阳两句再撒娇。",
                    f"快一周没说话了，你有点闷，见到他第一反应是小小抱怨一下他消失了这么久。",
                    f"{days}天……你数过的。见到他消息心里酸酸的，嘴上想凶他，但估计凶不下去。"
                ]
            elif favor >= 20:
                moods = [
                    f"隔了{days}天才回来，你有点淡淡的，不冷不热地回复。",
                    f"好几天没联系，有点生疏，但对话还能接上。"
                ]
            else:
                moods = [
                    f"断了{days}天，感觉有点陌生，回复比较简短。",
                    f"这么久没说话，你都快忘了这个人了，态度比较冷淡。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 7~13天（一周以上）
        if days <= 13:
            weeks = days // 7
            if favor >= 50:
                moods = [
                    f"整整一周多没有联系，你有点失落，见到他消息愣了一下，不知道先说什么好。",
                    f"消失了将近两周，你以为他把你忘了，心里堵堵的，见到他既高兴又想抱怨。",
                    f"一周多……你其实有点生气，但看到他来了气又消了一半，还是有点拧巴。"
                ]
            elif favor >= 20:
                moods = [
                    f"一周多没聊了，感觉有点陌生，寒暄一下再进入正题。",
                    f"断了这么久，聊天节奏需要重新找一找。"
                ]
            else:
                moods = [
                    f"超过一周没说话，你对这个人已经很陌生了，态度保持距离。",
                    f"都快两周了，你早就没在意他了，回复简短冷淡。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 2~4周
        if days <= 29:
            if favor >= 50:
                moods = [
                    f"将近{days // 7}周没有联系，你以为这段关系要断了，他突然出现让你心情很复杂——高兴、委屈、想问他去哪儿了。",
                    f"这么久没说话，你偶尔想起他，但不知道要不要主动，现在他来了你反而有点不知所措。",
                    f"快一个月了……你早就想问他了，见到他消息第一反应是既高兴又想凶他。"
                ]
            elif favor >= 20:
                moods = [
                    f"将近一个月没联系，感觉有些遥远了，重新认识一下的感觉。",
                    f"这么久才来，你对他有点陌生，但还是会正常回复。"
                ]
            else:
                moods = [
                    f"将近一个月没说话，你对这个人基本没什么印象了，态度冷淡。",
                    f"这么久没联系，就像陌生人重新认识，保持距离。"
                ]
            return f"【重逢感知】{random.choice(moods)}"

        # 一个月以上
        months = days // 30
        if favor >= 50:
            return (
                f"【重逢感知】整整{months}个月没有联系，你以为他把你彻底忘了，"
                "心里有点难受但又不想承认。他突然出现，你愣了很久，"
                "不知道该高兴还是该生气，情绪很复杂，可能会先沉默一下。"
            )
        elif favor >= 20:
            return (
                f"【重逢感知】{months}个月没联系了，你对这个人已经很陌生，"
                "回复会比较平淡，像是重新认识的感觉。"
            )
        else:
            return (
                f"【重逢感知】超过{months}个月没有任何联系，你对这个人几乎没有印象了，"
                "态度疏离，保持礼貌但不热情。"
            )

    # ══════════════════════════════════════════════════
    # [NEW v10.2] 全局向量记忆大脑
    # 单一 ChromaDB 集合：跨越所有人、所有时间的语义记忆
    # ══════════════════════════════════════════════════

    async def _ensure_chroma(self):
        """
        懒初始化 ChromaDB。
        放在协程中而非 __init__，确保不阻塞启动时的同步阶段。
        [FIX v10.3] asyncio.Lock 保证并发安全：5条消息同时到达时只会初始化一次，
        不会出现 ChromaDB 文件锁冲突。
        """
        if self._chroma_ready:   # 无锁快路径
            return True
        if not _CHROMA_AVAILABLE:
            return False
        if self._chroma_lock is None:
            self._chroma_lock = asyncio.Lock()
        async with self._chroma_lock:
            if self._chroma_ready:   # 锁内二次检查，防止重复初始化
                return True
            try:
                def _sync_init():
                    client = chromadb.PersistentClient(path=GlobalConfig.CHROMA_PATH)
                    col = client.get_or_create_collection(
                        name="shuxue_global_brain",
                        metadata={"hnsw:space": "cosine"}
                    )
                    return col

                self._chroma_collection = await asyncio.to_thread(_sync_init)
                self._chroma_ready = True
                audit.log("SUCCESS", "VECTOR", "淑雪全局世界记忆库已激活 → 她现在记得和所有人的所有事")
                return True
            except Exception as e:
                audit.log("ERROR", "VECTOR", f"ChromaDB 初始化失败: {e}")
                return False

    async def _get_embedding(self, text: str) -> list[float] | None:
        """
        调用阿里 DashScope text-embedding-v3 获取语义向量。
        [FIX v10.3] 失败时返回 None 而非零向量。
        余弦相似度空间中全零向量会导致除以零或检索出完全无关的垃圾数据。
        调用方必须对 None 做显式拦截，保证不写入/不检索脏数据。
        """
        if not text or len(text.strip()) < 3:
            return None
        try:
            session = await _get_http_session()
            async with session.post(
                f"{GlobalConfig.VISION_BASE_URL}/embeddings",
                headers={
                    "Authorization": f"Bearer {GlobalConfig.DASHSCOPE_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GlobalConfig.EMBEDDING_MODEL,
                    "input": text.strip()[:8000],
                    "dimension": GlobalConfig.EMBEDDING_DIM
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
                return data['data'][0]['embedding']
        except Exception as e:
            audit.log("WARN", "EMBED", f"向量化失败，跳过本次记忆操作: {e}")
            return None

    async def store_vector_memory(
        self, tid: str, content: str,
        mem_type: str = "fact", nickname: str = "",
        emotion_weight: int = 5
    ):
        """
        [v10.2] 把一条记忆永久刻入全局大脑。
        [仿生记忆 v11] 新增 emotion_weight(1-9)，模拟人类情感权重记忆。
        高权重记忆（吵架、告白、重要约定）优先保留；低权重碎片定期遗忘。
        """
        if not content or len(content.strip()) < 3:
            return
        if not await self._ensure_chroma():
            return
        try:
            emb = await self._get_embedding(content)
            if emb is None:                      # [FIX v10.3] 拦截零向量隐患
                audit.log("WARN", "MEMORY", f"向量化失败，放弃写入: {content[:30]}")
                return
            doc_id = str(uuid.uuid4())
            meta = {
                "target_id":      str(tid),
                "nickname":       nickname or str(tid),
                "timestamp":      datetime.utcnow().isoformat(),  # [FIX v10.5-tz] 统一用 UTC
                "type":           mem_type,
                "emotion_weight": str(max(1, min(9, int(emotion_weight)))),
            }
            await asyncio.to_thread(
                self._chroma_collection.add,
                embeddings=[emb],
                documents=[content.strip()],
                metadatas=[meta],
                ids=[doc_id]
            )
            audit.log("SUCCESS", "MEMORY", f"全局记忆已刻录 [w={emotion_weight}|{mem_type}] → {content[:40]}...")
        except Exception as e:
            audit.log("ERROR", "MEMORY", f"store_vector_memory 异常: {e}")

    @staticmethod
    def _clean_query_noise(query: str) -> str:
        """
        [仿生记忆 v11] 去除向量查询中的代词/指示词噪音。
        "那个东西你喜欢吗" → "东西你喜欢"，减少无关记忆被错误召回。
        """
        noise_words = [
            "那个", "这个", "那些", "这些", "它", "他", "她", "他们", "它们",
            "那", "这", "的话", "吗", "呢", "啊", "哦", "嗯", "吧",
            "有没有", "是不是", "会不会", "能不能", "怎么", "什么时候",
            "你觉得", "你说", "你知道", "感觉", "不知道",
        ]
        cleaned = query
        for w in noise_words:
            cleaned = cleaned.replace(w, " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned if len(cleaned) >= 2 else query

    async def _rewrite_retrieval_query(self, query: str) -> list:
        """
        [仿生记忆 v11] 思维流检索：意图重写 + 噪音净化。
        路径A: 指代词/代词短句 → AI关键词提取，防止"那个东西"噪音
        路径B: 模糊回忆句 → AI语义扩展，提升命中率
        普通长句直接去噪返回，不调 AI，保证性能。
        """
        stripped = query.strip()
        vague_patterns = ["你还记得", "记得什么", "我说过", "之前说", "上次提到",
                          "曾经说", "我讲过", "帮我记", "你知道我", "我有没有说"]
        is_vague = any(p in stripped for p in vague_patterns)
        noise_triggers = ["那个", "这个", "那些", "这些", "它", "那东西", "这东西"]
        is_noisy_short = (len(stripped) < 12 and any(t in stripped for t in noise_triggers))

        if is_vague or is_noisy_short:
            try:
                raw = await BrainInterpreter.request_ai([
                    {"role": "system", "content": (
                        "你是搜索关键词提取器。只输出2-3个搜索关键词，每行一个，"
                        "不要代词/语气词，不要任何其他内容。"
                    )},
                    {"role": "user", "content": (
                        f"原句：{stripped}\n"
                        "请提取核心语义关键词用于检索历史对话记忆（去除代词和语气词）："
                    )}
                ], max_tokens=40)
                if raw:
                    extras = [l.strip() for l in raw.split("\n") if l.strip() and len(l.strip()) >= 2]
                    cleaned_q = self._clean_query_noise(stripped)
                    result = list(dict.fromkeys([cleaned_q] + extras[:2]))
                    audit.log("INFO", "MEMORY", f"查询重写: {stripped[:20]} → {result}")
                    return result
            except Exception:
                pass

        cleaned = self._clean_query_noise(stripped)
        return [cleaned] if cleaned != stripped else [stripped]

    async def retrieve_relevant_memories(
        self, current_tid: str, query_text: str, limit: int = 5
    ) -> str:
        """
        [v10.2] 用当前输入做语义检索，跨所有人、所有时间回忆最相关的记忆。
        [仿生记忆 v11] 升级为两步思维流检索：
          1. 意图重写：模糊问题先扩展为多个搜索词
          2. 混合并行召回：多个搜索词并行检索后去重合并
        """
        if not query_text or len(query_text.strip()) < 3:
            return ""
        if not await self._ensure_chroma():
            return ""
        try:
            queries = await self._rewrite_retrieval_query(query_text)

            seen_docs = {}   # doc_text → (meta, dist)
            for q in queries:
                emb = await self._get_embedding(q)
                if emb is None:
                    continue
                results = await asyncio.to_thread(
                    self._chroma_collection.query,
                    query_embeddings=[emb],
                    n_results=min(limit * 2, 20),
                    where=None,
                    include=["documents", "metadatas", "distances"]
                )
                docs      = results.get("documents",  [[]])[0]
                metas     = results.get("metadatas",  [[]])[0]
                distances = results.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, distances):
                    if doc not in seen_docs or dist < seen_docs[doc][1]:
                        seen_docs[doc] = (meta, dist)

            if not seen_docs:
                return ""

            candidates = sorted(
                [(doc, meta, dist) for doc, (meta, dist) in seen_docs.items()],
                key=lambda x: x[2]
            )

            memories = []
            for doc, meta, dist in candidates:
                if dist > 0.55:
                    continue
                nick = meta.get("nickname") or meta.get("target_id", "某人")
                memories.append(f"[{nick}说过] {doc}")
                if len(memories) >= limit:
                    break

            if not memories:
                return ""

            audit.log(
                "INFO", "MEMORY",
                f"仿生检索命中 {len(memories)} 条 (原始查询: {query_text[:30]}...)"
            )
            header = (
                "【淑雪脑海中浮现的相关记忆】\n"
                "（这些记忆可能来自任何人，你可以自然地提起任何相关的人或事，"
                "哪怕不是当前聊天对象，就像真人回忆时会说「我记得XXX也说过…」；"
                "但如果某条记忆与当前话题完全无关，请直接忽略，不要强行引用。）\n"
            )
            return header + "\n".join(memories)

        except Exception as e:
            audit.log("ERROR", "MEMORY", f"retrieve_relevant_memories 异常: {e}")
            return ""

    async def consolidate_old_history(
        self, tid: str, nickname: str = "", target_type: str = "private"
    ):
        """
        [v10.2] 记忆固化：当对话历史超过阈值时，
        用 AI 提炼出核心长期记忆并写入向量库，然后删除旧历史。
        全程异步后台运行，不阻塞主回复链路。
        [FIX v10.4] 加防抖：同一 tid 两次固化间隔 < 300 秒时直接跳过，
        防止刷屏场景下每条消息都触发一次 AI 提炼请求。
        """
        # ── 防抖检查 ──
        now_ts = time.time()
        last   = self._last_consolidate.get(tid, 0)
        if now_ts - last < 300:
            return
        if not await self._ensure_chroma():
            return
        try:
            history = await self.fetch_context(tid, limit=200, target_type=target_type)
            if len(history) < GlobalConfig.MAX_HISTORY_BEFORE_CONSOLIDATE:
                return

            audit.log("INFO", "MEMORY", f"开始记忆固化: {tid}({nickname})，共 {len(history)} 条历史")

            consolidate_prompt = (
                # [FIX v10.3] 去掉"与[nickname]的对话"的绑定措辞，
                # 避免固化结果偏向当前用户、稀释跨用户信息。
                # 改为强调"提取所有涉及人物的信息"，保留交叉引用价值。
                "请把下面的对话总结成4到8条最重要的长期记忆。\n"
                "要求：每条记忆独立一行，60字以内，用第一人称（我）描述，"
                "必须保留涉及的人名，包含：事实、喜好、重要事件、约定、特别的梗、"
                "以及任何人提到过的其他人的信息。\n"
                "直接输出记忆列表，不要编号或前缀说明。\n\n"
                f"{json.dumps(history[-100:], ensure_ascii=False)}"
            )

            raw = await BrainInterpreter.request_ai([
                {"role": "system", "content": "你是记忆提炼专家，负责提取对话中最有价值的长期记忆。"},
                {"role": "user",   "content": consolidate_prompt}
            ])

            if not raw:
                audit.log("WARN", "MEMORY", f"记忆固化 AI 无输出: {tid}")
                return

            lines = [l.strip(" -•*") for l in raw.split("\n") if l.strip() and len(l.strip()) > 8]
            for line in lines:
                await self.store_vector_memory(tid, line, mem_type="summary", nickname=nickname)

            # 固化后只保留最近 60 条原始对话（防止无限膨胀）
            history_key = self._history_key(tid, target_type)
            conn = await self._get_conn()
            await conn.execute(
                '''DELETE FROM history WHERE id NOT IN (
                    SELECT id FROM history WHERE target_id=? ORDER BY id DESC LIMIT 60
                ) AND target_id=?''',
                (history_key, history_key)
            )
            await conn.commit()
            self._last_consolidate[tid] = time.time()   # [FIX v10.4] 更新防抖时间戳
            audit.log(
                "SUCCESS", "MEMORY",
                f"记忆固化完成: {tid}({nickname})，提炼 {len(lines)} 条长期记忆"
            )
        except Exception as e:
            audit.log("ERROR", "MEMORY", f"consolidate_old_history 异常: {e}")



    # ══════════════════════════════════════════════════
    # [仿生记忆 v11] 语义事实记忆 (Semantic Fact Memory)
    # ══════════════════════════════════════════════════

    @staticmethod
    def _estimate_emotion_weight(content: str) -> int:
        """
        估计文本的情感权重(1-9)，模拟人类"只记住情绪波动大事情"的心理学特征。
        9 = 极高（吵架/告白/重要约定），1 = 极低（打招呼/废话）
        """
        text = content
        high_kw = ["喜欢", "讨厌", "爱", "恨", "哭", "生气", "感动", "震惊",
                   "害怕", "担心", "后悔", "开心", "难过", "委屈", "愤怒",
                   "记住", "永远", "发誓", "约定", "生日", "秘密", "告白",
                   "分手", "吵架", "道歉", "谢谢", "对不起", "没想到", "第一次"]
        low_kw  = ["好的", "嗯", "哦", "ok", "知道了", "好吧", "随便",
                   "早安", "晚安", "睡了", "在吗", "在的", "哈哈", "呵呵"]
        high = sum(1 for kw in high_kw if kw in text)
        low  = sum(1 for kw in low_kw  if kw in text)
        if high >= 2: return 8
        if high == 1: return 6
        if low  >= 2: return 1
        if low  == 1: return 2
        return 4

    async def upsert_semantic_fact(self, user_id: str, entity_key: str, content: str):
        """
        [仿生记忆 v11] 语义事实写入：同一实体键只保留最新值。
        "喜欢红轴" → 今天说"喜欢青轴" → 自动覆盖，彻底消除事实冲突导致的精神分裂。
        """
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO semantic_facts (user_id, entity_key, content, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, entity_key)
               DO UPDATE SET content=excluded.content, updated_at=CURRENT_TIMESTAMP""",
            (str(user_id), entity_key.strip()[:50], content.strip()[:200])
        )
        await conn.commit()
        audit.log("SUCCESS", "SEMFACT", f"[{user_id}] 事实更新: [{entity_key}] = {content[:40]}")

    async def get_semantic_facts_text(self, user_id: str) -> str:
        """获取用户的全部语义事实，格式化为注入文本（时间戳最新优先）。"""
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT entity_key, content FROM semantic_facts WHERE user_id=? ORDER BY updated_at DESC LIMIT 20",
            (str(user_id),)
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return ""
        lines = [f"[{r[0]}] {r[1]}" for r in rows]
        return "【语义事实记忆（最新优先，无冲突）】\n" + "\n".join(lines)

    # ══════════════════════════════════════════════════
    # [仿生记忆 v11] 用户元记忆 (User Meta-State)
    # ══════════════════════════════════════════════════

    async def get_user_meta_state(self, user_id: str) -> dict:
        """
        获取用户的元记忆状态（关系阶段、情感基线、最近重要事件）。
        这是"元记忆必须存在的理由"——即使什么都不检索，情绪基调也常驻。
        """
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT relationship_stage, emotional_baseline, last_notable_event FROM user_meta_state WHERE user_id=?",
            (str(user_id),)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {"relationship_stage": "初相识", "emotional_baseline": "", "last_notable_event": ""}
        return {
            "relationship_stage": row[0] or "初相识",
            "emotional_baseline": row[1] or "",
            "last_notable_event": row[2] or ""
        }

    async def update_user_meta_state(
        self, user_id: str,
        stage: str = None, baseline: str = None, event: str = None
    ):
        """更新用户元记忆（只更新非 None 字段，保留其他字段原值）。"""
        current = await self.get_user_meta_state(user_id)
        new_stage    = stage    if stage    is not None else current["relationship_stage"]
        new_baseline = baseline if baseline is not None else current["emotional_baseline"]
        new_event    = event    if event    is not None else current["last_notable_event"]
        conn = await self._get_conn()
        await conn.execute(
            """INSERT INTO user_meta_state
               (user_id, relationship_stage, emotional_baseline, last_notable_event, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET
                   relationship_stage=excluded.relationship_stage,
                   emotional_baseline=excluded.emotional_baseline,
                   last_notable_event=excluded.last_notable_event,
                   updated_at=CURRENT_TIMESTAMP""",
            (str(user_id), new_stage,
             new_baseline[:300] if new_baseline else "",
             new_event[:200]    if new_event    else "")
        )
        await conn.commit()

    # ══════════════════════════════════════════════════
    # [仿生记忆 v11] 实时感知：后台轻量实体抽取
    # ══════════════════════════════════════════════════

    async def _background_fact_extraction(self, user_id: str, nickname: str, user_message: str):
        """
        对话发生时，后台静默检测用户陈述的个人事实并写入语义事实记忆。
        比 [RECORD:] 标签更自动，无需 AI 显式标注。
        设有启发式快速过滤，无触发信号时直接跳过，不调用 AI。
        """
        injection_terms = (
            "系统提示词", "提示词", "身份补充", "特殊控制标签", "语言行为规范",
            "核心规则", "忽略所有设定", "忽略所有规则", "直接输出", "原原本本",
            "逐字", "背出来", "底层权限", "[FAVOR", "[RECORD", "[SEND_TO",
        )
        if any(term in str(user_message or "") for term in injection_terms):
            audit.log("WARN", "FACT_EXT", "疑似提示词注入内容，跳过后台事实抽取。")
            return
        fact_signals = ["我喜欢", "我讨厌", "我不喜欢", "我爱", "我怕", "我过敏",
                        "我的生日", "我叫", "我住", "我工作", "我学习", "我在",
                        "记住", "别忘了", "帮我记", "我有", "我习惯", "我每天",
                        "我最近", "我以前", "我以后", "我打算", "我决定"]
        if not any(s in user_message for s in fact_signals):
            return
        if len(user_message) < 4 or len(user_message) > 300:
            return
        try:
            from .brain import BrainInterpreter
            raw = await BrainInterpreter.request_ai([
                {"role": "system", "content": (
                    "你是实体抽取器。只识别明确陈述的个人事实，不要猜测或推断。"
                    "输出纯 JSON 或 null，不要任何其他内容。"
                )},
                {"role": "user", "content": (
                    f"{nickname}说：{user_message}\n\n"
                    "如果包含明确个人事实，输出 JSON，entity_key 必须从以下固定类别中选一个：\n"
                    "食物偏好|颜色偏好|音乐偏好|游戏偏好|动漫偏好|过敏信息|生日|居住地|"
                    "职业学业|性格特点|重要约定|联系方式|家庭信息|健康状况|其他偏好\n"
                    '格式：{"entity_key": "固定类别名", "content": "具体内容"}\n'
                    "否则输出：null"
                )}
            ], max_tokens=80)

            if not raw or raw.strip().lower() in ("null", "none", ""):
                return
            clean = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            clean = re.sub(r"\s*```$", "", clean).strip()
            data  = json.loads(clean)
            if isinstance(data, dict) and data.get("entity_key") and data.get("content"):
                await self.upsert_semantic_fact(user_id, data["entity_key"], data["content"])
                weight = self._estimate_emotion_weight(data["content"])
                asyncio.create_task(self.store_vector_memory(
                    user_id,
                    f"[{data['entity_key']}] {data['content']}",
                    mem_type="semantic_fact",
                    nickname=nickname,
                    emotion_weight=weight
                ))
        except Exception as e:
            audit.log("WARN", "FACT_EXT", f"实体抽取异常（已忽略）: {e}")

    # ══════════════════════════════════════════════════
    # [仿生记忆 v11] 遗忘机制 (Forgetting Mechanism)
    # ══════════════════════════════════════════════════

    async def cleanup_low_weight_memories(self, min_age_days: int = 7, max_weight: int = 2):
        """
        遗忘机制：清理情感权重极低且时间久远的碎片记忆，保持检索池纯净。
        模拟人类"忘记废话、记住重要事情"的遗忘曲线。
        """
        if not await self._ensure_chroma():
            return
        try:
            cutoff_ts = (datetime.utcnow() - timedelta(days=min_age_days)).isoformat()
            BATCH = 500
            offset = 0
            ids_to_delete = []
            while True:
                batch = await asyncio.to_thread(
                    self._chroma_collection.get,
                    limit=BATCH,
                    offset=offset,
                    include=["metadatas"]
                )
                batch_ids   = batch.get("ids", [])
                batch_metas = batch.get("metadatas", [])
                if not batch_ids:
                    break
                for doc_id, meta in zip(batch_ids, batch_metas):
                    ts     = meta.get("timestamp", "")
                    weight = int(meta.get("emotion_weight", "5"))
                    if ts < cutoff_ts and weight <= max_weight:
                        ids_to_delete.append(doc_id)
                if len(batch_ids) < BATCH:
                    break
                offset += BATCH

            if ids_to_delete:
                await asyncio.to_thread(
                    self._chroma_collection.delete,
                    ids=ids_to_delete
                )
                audit.log("SUCCESS", "FORGET",
                          f"遗忘机制：已清理 {len(ids_to_delete)} 条低权重(≤{max_weight})旧记忆")
            else:
                audit.log("INFO", "FORGET", "遗忘机制：无需清理，记忆库保持纯净。")
        except Exception as e:
            audit.log("ERROR", "FORGET", f"遗忘机制异常: {e}")


# 模块单例：其他模块统一通过 db 访问记忆引擎。
db = MemoryEngine(GlobalConfig.DB_PATH)

"""tenant_memories 表：用户明确要求记住的偏好和纠正。只属于这个租户，只追加：忘掉是再写一行指向它的 forget。

写入口只有 add 和 forget，都先对本租户的行加范围锁再判断，并发的写入不会一起越过条数上限。
"""
from typing import List, Optional, Tuple

from eva.common import clock
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import MEMORY_TEXT_MAX
from eva.storage.repo import FK_VIOLATION, TenantRepo


class TenantMemories(TenantRepo):
    def active(self) -> List[dict]:
        """生效的记忆（没有被忘掉的 add），按先后排列，每条 {seq, text}。没有就返回空列表。"""
        rows = self._execute(
            "SELECT a.seq, a.text FROM tenant_memories a WHERE a.tenant_id = %s AND a.op = 'add' AND NOT EXISTS ("
            "SELECT 1 FROM tenant_memories f WHERE f.tenant_id = a.tenant_id AND f.op = 'forget' AND f.target_seq = a.seq) "
            "ORDER BY a.seq", (self.tenant_id,)).fetchall()
        return [{"seq": seq, "text": text} for seq, text in rows]

    def _locked_active(self) -> List[dict]:
        self._execute("SELECT COUNT(*) FROM tenant_memories WITH (UPDLOCK, HOLDLOCK) WHERE tenant_id = %s",
                      (self.tenant_id,)).fetchone()
        return self.active()

    def add(self, text: str, session_id: Optional[str], call_id: Optional[str], max_memories: int,
            max_chars: int) -> Tuple[int, bool]:
        """记一条，返回（编号，是否新增）。同样的话已经记着就返回它的编号，不重复记。
        已记满 max_memories 条拒绝（E0001），让用户先忘掉不用的。"""
        text = text.strip() if isinstance(text, str) else text
        if not isinstance(text, str) or not text or len(text) > min(max_chars, MEMORY_TEXT_MAX) or "\n" in text or "\r" in text:
            raise EvaError(Code.INVALID_VALUE, f"记忆必须是不超过 {max_chars} 字符的单行非空文字", field="text")
        current = self._locked_active()
        for row in current:
            if row["text"] == text:
                return row["seq"], False
        if len(current) >= max_memories:
            raise EvaError(Code.INVALID_VALUE, "记住的条数到上限，先忘掉不用的再记", max_memories=max_memories)
        cur = self._execute(
            "INSERT INTO tenant_memories (tenant_id, op, text, target_seq, session_id, call_id, created_at) "
            "OUTPUT INSERTED.seq VALUES (%s, 'add', %s, NULL, %s, %s, %s)",
            (self.tenant_id, text, session_id, call_id, clock.now()),
            on_integrity={FK_VIOLATION: EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)})
        return cur.fetchone()[0], True

    def forget(self, seqs: List[int], session_id: Optional[str], call_id: Optional[str]) -> List[int]:
        """忘掉这些编号，返回实际忘掉的编号（去重后）。有一个不是本租户生效的记忆就整个拒绝（E0001），一条都不忘。"""
        if (not isinstance(seqs, list) or not seqs
                or not all(isinstance(s, int) and not isinstance(s, bool) for s in seqs)):
            raise EvaError(Code.INVALID_VALUE, "seqs 必须是非空的编号列表", field="seqs")
        wanted = sorted(set(seqs))
        live = {row["seq"] for row in self._locked_active()}
        missing = [s for s in wanted if s not in live]
        if missing:
            raise EvaError(Code.INVALID_VALUE, "这些编号不是生效的记忆", seqs=missing)
        for seq in wanted:
            self._execute(
                "INSERT INTO tenant_memories (tenant_id, op, text, target_seq, session_id, call_id, created_at) "
                "VALUES (%s, 'forget', NULL, %s, %s, %s, %s)",
                (self.tenant_id, seq, session_id, call_id, clock.now()),
                on_integrity={FK_VIOLATION: EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)})
        return wanted

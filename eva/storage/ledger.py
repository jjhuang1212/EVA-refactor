"""ledger_events 表：只追加。一次预扣 hold 最终对应一条 settle 或一条 release，二选一。"""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Iterator, Optional

from eva.common import clock, ids
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import LedgerKind, Purpose, check
from eva.storage import db
from eva.storage.repo import UNIQUE_VIOLATION, TenantRepo

_MAX_CNY = Decimal(10) ** 6  # decimal(10,4) 的整数部分上限


def _money(value) -> Decimal:
    """不静默四舍五入：小数超过 4 位、负数、非有限值、超范围都拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise EvaError(Code.INVALID_VALUE, "金额必须是 Decimal 或 int", value=repr(value))
    d = Decimal(value)
    if not d.is_finite() or d < 0 or d >= _MAX_CNY or d != d.quantize(Decimal("0.0001")):
        raise EvaError(Code.INVALID_VALUE, "金额必须是 0 到 1000000 之间、最多 4 位小数", value=str(value))
    return d


def _count(name: str, value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaError(Code.INVALID_VALUE, f"{name} 必须是非负整数或 None", value=repr(value))
    return value


# 从 hold 行派生 settle / release：租户、会话、用途沿用 hold。
# 对该 hold_id 加范围锁后再判断是否已结算或释放，并发的两次收尾不会都通过
_CLOSE = (
    "INSERT INTO ledger_events (tenant_id, session_id, kind, hold_id, idem_key, purpose, model, amount_cny, "
    "tokens_in, tokens_out, cache_hit_tokens, cache_miss_tokens, asr_seconds, pricing_version, created_at) "
    "SELECT h.tenant_id, h.session_id, %s, h.hold_id, %s, h.purpose, ISNULL(%s, h.model), ISNULL(%s, h.amount_cny), "
    "%s, %s, %s, %s, %s, ISNULL(%s, h.pricing_version), %s "
    "FROM ledger_events h WHERE h.tenant_id = %s AND h.hold_id = %s AND h.kind = 'hold' "
    "AND NOT EXISTS (SELECT 1 FROM ledger_events x WITH (UPDLOCK, HOLDLOCK) "
    "WHERE x.hold_id = h.hold_id AND x.kind IN ('settle', 'release'))"
)


# 已用（settle）加占用（还没被 settle 或 release 关掉的 hold）
_LIVE = (
    "(x.kind = 'settle' OR (x.kind = 'hold' AND NOT EXISTS "
    "(SELECT 1 FROM ledger_events y WHERE y.hold_id = x.hold_id AND y.kind IN ('settle', 'release'))))"
)
_COMMITTED = "x.tenant_id = %s AND x.session_id = %s AND " + _LIVE
# 当天：创建时间在东八区今天 0 点之后的行。跨零点还没结算的预扣记在前一天，它的结算行记在结算那天
_COMMITTED_TODAY = "x.tenant_id = %s AND x.created_at >= %s AND " + _LIVE


@contextmanager
def open_ledger(tenant_id: str) -> Iterator["Ledger"]:
    """独立事务的账本。预扣、结算必须先提交再调模型，崩溃后才有记录可清扫，所以模型层用它而不是共用连接。"""
    with db.transaction() as conn:
        yield Ledger(conn, tenant_id)


def stale_holds(conn, older_than, limit: int) -> list:
    """跨租户列出创建时间早于 older_than、仍未结算也未释放的预扣，返回 (tenant_id, hold_id)。
    只给清扫用；拿到后必须按各自的租户构造 Ledger 再操作，不在这里改任何数据。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT TOP (%s) h.tenant_id, h.hold_id FROM ledger_events h WHERE h.kind = 'hold' AND h.created_at < %s "
        "AND NOT EXISTS (SELECT 1 FROM ledger_events y WHERE y.hold_id = h.hold_id AND y.kind IN ('settle', 'release')) "
        "ORDER BY h.created_at, h.event_id",
        (limit, older_than),
    )
    return [tuple(r) for r in cur.fetchall()]


class Ledger(TenantRepo):
    def hold(self, session_id: str, idem_key: str, purpose: str, amount_cny, pricing_version: str,
             model: Optional[str] = None, budget_cny=None) -> str:
        """预扣。返回 hold_id，后续 settle / release 用它。会话不属于本租户抛 E3002。
        给了 budget_cny 时，它是本租户一天（东八区 0 点起）的预算：当天已用额加占用额再加本笔超过它就抛 E4004，
        详情带 resets_at（下次 0 点）。检查和写入在同一条语句里，并对该租户当天的账本行加范围锁，
        并发的预扣不会一起超支，同租户的不同会话也共用这份额度。"""
        purpose = check(Purpose, purpose).value
        amount = _money(amount_cny)
        hold_id = ids.new_id()
        params = [self.tenant_id, session_id, hold_id, idem_key, purpose, model, amount, pricing_version,
                  clock.now(), session_id, self.tenant_id]
        budget_sql = ""
        if budget_cny is not None:
            budget_sql = (" AND (SELECT ISNULL(SUM(x.amount_cny), 0) FROM ledger_events x WITH (UPDLOCK, HOLDLOCK) "
                          f"WHERE {_COMMITTED_TODAY}) + %s <= %s")
            params += [self.tenant_id, clock.day_start(), amount, _money(budget_cny)]
        cur = self._execute(
            "INSERT INTO ledger_events (tenant_id, session_id, kind, hold_id, idem_key, purpose, model, amount_cny, "
            "pricing_version, created_at) SELECT %s, %s, 'hold', %s, %s, %s, %s, %s, %s, %s "
            "WHERE EXISTS (SELECT 1 FROM sessions WHERE session_id = %s AND tenant_id = %s)" + budget_sql,
            tuple(params),
            on_integrity={n: self._duplicate(LedgerKind.HOLD.value, idem_key) for n in UNIQUE_VIOLATION},
        )
        if cur.rowcount == 1:
            return hold_id
        if not self._execute("SELECT 1 FROM sessions WHERE session_id = %s AND tenant_id = %s",
                             (session_id, self.tenant_id)).fetchone():
            raise EvaError(Code.SESSION_NOT_FOUND, "会话不存在", session_id=session_id)
        raise EvaError(Code.BUDGET_EXCEEDED, "今日预算不足", session_id=session_id, amount_cny=str(amount),
                       budget_cny=str(budget_cny), committed_cny=str(self.committed_today()),
                       resets_at=(clock.day_start() + timedelta(days=1)).isoformat())

    def committed(self, session_id: str) -> Decimal:
        """会话已用额（settle 之和）加占用额（还没 settle 或 release 的 hold 之和）。"""
        row = self._execute(
            f"SELECT ISNULL(SUM(x.amount_cny), 0) FROM ledger_events x WHERE {_COMMITTED}",
            (self.tenant_id, session_id),
        ).fetchone()
        return Decimal(row[0])

    def committed_today(self) -> Decimal:
        """本租户东八区今天已用额加占用额，所有会话合计。"""
        row = self._execute(
            f"SELECT ISNULL(SUM(x.amount_cny), 0) FROM ledger_events x WHERE {_COMMITTED_TODAY}",
            (self.tenant_id, clock.day_start()),
        ).fetchone()
        return Decimal(row[0])

    def settled(self, session_id: str, purposes: tuple) -> Decimal:
        """会话里这些用途已结算的金额之和（预扣未结算的不算）。"""
        marks = ", ".join(["%s"] * len(purposes))
        row = self._execute(
            "SELECT ISNULL(SUM(amount_cny), 0) FROM ledger_events WHERE tenant_id = %s AND session_id = %s "
            f"AND kind = 'settle' AND purpose IN ({marks})", (self.tenant_id, session_id, *purposes)).fetchone()
        return Decimal(row[0])

    def settle(self, hold_id: str, idem_key: str, amount_cny, pricing_version: str, model: Optional[str] = None,
               tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
               cache_hit_tokens: Optional[int] = None, cache_miss_tokens: Optional[int] = None,
               asr_seconds: Optional[int] = None) -> None:
        self._close(LedgerKind.SETTLE.value, hold_id, idem_key, _money(amount_cny), pricing_version, model,
                    (_count("tokens_in", tokens_in), _count("tokens_out", tokens_out),
                     _count("cache_hit_tokens", cache_hit_tokens), _count("cache_miss_tokens", cache_miss_tokens),
                     _count("asr_seconds", asr_seconds)))

    def release(self, hold_id: str, idem_key: str) -> None:
        """把整笔预扣还回去，金额、模型、定价版本沿用 hold。"""
        self._close(LedgerKind.RELEASE.value, hold_id, idem_key, None, None, None, (None,) * 5)

    def _close(self, kind, hold_id, idem_key, amount, pricing_version, model, extras) -> None:
        cur = self._execute(
            _CLOSE,
            (kind, idem_key, model, amount, *extras, pricing_version, clock.now(), self.tenant_id, hold_id),
            on_integrity={n: self._duplicate(kind, idem_key) for n in UNIQUE_VIOLATION},
        )
        if cur.rowcount == 1:
            return
        # 重试同一次调用要报"已记过"，优先于"预扣已关闭"
        if self._recorded(kind, idem_key):
            raise self._duplicate(kind, idem_key)
        row = self._execute(
            "SELECT 1 FROM ledger_events WHERE tenant_id = %s AND hold_id = %s AND kind = 'hold'",
            (self.tenant_id, hold_id),
        ).fetchone()
        if row is None:
            raise EvaError(Code.HOLD_NOT_FOUND, "预扣不存在", hold_id=hold_id)
        raise EvaError(Code.HOLD_CLOSED, "预扣已结算或释放", hold_id=hold_id)

    def _recorded(self, kind: str, idem_key: str) -> bool:
        return self._execute(
            "SELECT 1 FROM ledger_events WHERE tenant_id = %s AND idem_key = %s AND kind = %s",
            (self.tenant_id, idem_key, kind),
        ).fetchone() is not None

    def _duplicate(self, kind: str, idem_key: str) -> EvaError:
        return EvaError(Code.DUPLICATE_IDEM, "该调用的这类事件已记过", kind=kind, idem_key=idem_key)

    def session_events(self, session_id: str) -> list:
        """会话里所有账本事件，按写入顺序。没有返回空列表。给回放用，不改任何东西。"""
        names = ("kind", "hold_id", "idem_key", "purpose", "model", "amount_cny", "tokens_in", "tokens_out", "created_at")
        cur = self._execute(
            f"SELECT {', '.join(names)} FROM ledger_events WHERE tenant_id = %s AND session_id = %s ORDER BY event_id",
            (self.tenant_id, session_id),
        )
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def events(self, hold_id: str) -> list:
        """某次预扣名下的事件，按写入顺序。没有则返回空列表。"""
        cur = self._execute(
            "SELECT kind, amount_cny, idem_key, session_id, purpose, model, pricing_version FROM ledger_events "
            "WHERE tenant_id = %s AND hold_id = %s ORDER BY event_id",
            (self.tenant_id, hold_id),
        )
        names = ("kind", "amount_cny", "idem_key", "session_id", "purpose", "model", "pricing_version")
        return [dict(zip(names, r)) for r in cur.fetchall()]




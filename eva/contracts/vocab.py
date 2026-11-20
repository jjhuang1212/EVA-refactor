"""取值集合的唯一定义处。

表里存字符串、不加 CHECK 约束，校验在存储层：写入前用 check() 过一遍，不在集合里就拒绝。
AliasKind 和 HotwordSource 沿用旧库取值。
"""
import re
from enum import Enum
from typing import Type, TypeVar

from eva.contracts.errors import Code, EvaError

E = TypeVar("E", bound=Enum)


class Channel(str, Enum):
    WECOM = "wecom"
    CONSOLE = "console"


class MessageDirection(str, Enum):
    IN = "in"  # 用户发给我们
    OUT = "out"  # 我们发给用户


class MessageKind(str, Enum):
    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    VOICE = "voice"


class TenantRealm(str, Enum):
    """租户的用途，也是 tenant_id 的前缀。由启动入口决定，不从渠道用户 ID 猜。"""
    USR = "usr"  # 真实用户（企微、将来的网页）
    SIM = "sim"  # 企微模拟器
    EVAL = "eval"  # 评测
    DEV = "dev"  # 本地终端


class LedgerKind(str, Enum):
    HOLD = "hold"
    SETTLE = "settle"
    RELEASE = "release"


class Purpose(str, Enum):
    TURN = "turn"
    COMPACTION = "compaction"
    TOOL_CALL = "tool_call"
    ASR = "asr"
    USER_SIM = "user_sim"  # 评测里模型扮演用户的调用，记在它自己的会话里，不算 agent 的花费


class EvalVerdict(str, Enum):
    """评测运行的人工判定。"""
    PASS = "pass"
    FAIL = "fail"


class EvalInteraction(str, Enum):
    """评测时 agent 中途提问怎么办：NONE 不回答，这次运行就此停下；HUMAN 等人在网页上回答；MODEL 由模型扮演用户回答。"""
    NONE = "none"
    HUMAN = "human"
    MODEL = "model"


class EventType(str, Enum):
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    TRIM = "trim"
    HOOK = "hook"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    CONTEXT_RESET = "context_reset"
    ERROR = "error"


class Concurrency(str, Enum):
    SHARED = "shared"  # 可与相邻的 shared 工具并行
    EXCLUSIVE = "exclusive"  # 独占，前后都是屏障


class HookDecision(str, Enum):
    """多个 hook 命中同一点时取最严：deny > ask > allow。"""
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class TurnEnd(str, Enum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"  # 步数到上限，已禁用工具并要求文字总结
    CANCELLED = "cancelled"
    WAITING_USER = "waiting_user"  # 停放：等用户回答 ask_user，回答到来时开新 turn
    GATE_FAILED = "gate_failed"  # 完成门禁重试用尽
    OUTPUT_LIMIT = "output_limit"  # 纯文本输出撞上限，不续写
    EMPTY_RESPONSE = "empty_response"  # 既无文字也无工具调用
    ERROR = "error"  # 模型层或存储层抛了错误码，已写 error 事件


class FileStatus(str, Enum):
    UPLOADED = "uploaded"
    AVAILABLE = "available"
    DELETED = "deleted"


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    OCR = "ocr"  # 图片或扫描页经 OCR 识别出的文字，可能有错字，锚点带 confidence
    VISION = "vision"  # 图片经视觉模型看图得到的描述，是模型生成的识别结果，不是原文，不能作为引用依据
    TRANSCRIPT = "transcript"  # 录音经语音识别得到的文字，可能有错字，锚点带起止时间（毫秒）
    UNREAD = "unread"  # 这部分没有读出来（如无文本层的页），text 是给模型看的说明，不是原文，不能作为引用依据


class AliasKind(str, Enum):
    SHORT = "SHORT"
    FULL = "FULL"
    ENGLISH = "ENGLISH"
    FORMER = "FORMER"
    GROUP = "GROUP"  # 集团或品牌的口语叫法（如"阿里"），指向旗下一家境内主要运营主体；同一个叫法可以指向多家


class HotwordSource(str, Enum):
    LISTED_COMPANY = "LISTED_COMPANY"
    INDUSTRY_TERM = "INDUSTRY_TERM"


class TermKind(str, Enum):
    """租户确认过的写法属于哪一类。"""
    COMPANY = "company"
    PERSON = "person"
    TERM = "term"


class SearchEngine(str, Enum):
    """智谱 Web Search 的引擎编码。"""
    STD = "search_std"
    PRO = "search_pro"
    SOGOU = "search_pro_sogou"
    QUARK = "search_pro_quark"


class QccOutcome(str, Enum):
    """query_company 一次查询的结果，会话内留痕，交付前核对场景的维度覆盖时用。"""
    RECORDS = "records"      # 有记录
    NONE = "none"            # 数据源确认没有记录
    UNMATCHED = "unmatched"  # 数据源没匹配上
    FAILED = "failed"        # 出错没拿到结果


class SkillKind(str, Enum):
    """租户层 skill 的形态。"""
    ADDENDUM = "addendum"  # 补充：叠在同名公共 skill 的正文后面，公共层必须有这个名字
    OWN = "own"            # 租户独有：公共层没有这个名字


class SkillStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"  # 归档是追加一行，不删；再追加 active 恢复


SKILL_NAME = re.compile(r"[a-z][a-z0-9_-]*")  # skill 名：公共层是目录名，租户层是 tenant_skill_versions.name
SKILL_NAME_MAX = 64
MEMORY_TEXT_MAX = 1000  # tenant_memories.text 的列宽，配置里的字数上限不得超过


class AliasSource(str, Enum):
    LEGACY = "legacy"
    QCC = "qcc"
    OFFLINE = "offline"


def check(enum_cls: Type[E], value) -> E:
    """值在集合里就返回枚举成员，否则抛 INVALID_VALUE。大小写、空白都不宽容。"""
    try:
        return enum_cls(value)
    except ValueError:
        raise EvaError(
            Code.INVALID_VALUE,
            f"{enum_cls.__name__} 不接受该取值",
            vocab=enum_cls.__name__,
            value=value,
        ) from None


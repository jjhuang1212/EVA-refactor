"""渠道与核心之间交换的数据。渠道只认这个形状，不 import 核心的任何层。"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from eva.contracts.vocab import MessageDirection, MessageKind

CANCEL_MENU_ID = "cancel_run"  # 取消入口的菜单 ID，点击回调带回它，由渠道适配器识别，不经过模型


@dataclass(frozen=True)
class ReplyFile:
    """模型在本 turn 里要交付给用户的文件。渠道决定怎么送：能不能发、多大、占几条配额，都是渠道的事。"""
    filename: str
    mime: str
    data: bytes
    inline_if_short: bool = False  # 文本文件够短时渠道可以直接当文字发，放得下就不另发文件
    file_id: Optional[str] = None  # 文件登记里的编号；渠道自己生成的文件（长回复转成的 md）没有


@dataclass(frozen=True)
class Reply:
    text: str  # 最后一步模型的文字
    reason: str  # TurnEnd 的取值
    steps: int
    cost_cny: Decimal
    events: List[dict]  # 本 turn 写进日志的事件（从用户消息起），渠道可据此展示过程
    files: Tuple[ReplyFile, ...] = ()  # 本 turn 里 deliver_file 登记的文件，按登记先后
    options: Tuple[str, ...] = ()  # 以 ask_user 停放且带选项时的选项；渠道自己决定渲染成菜单、按钮还是编号文字


@dataclass(frozen=True)
class ChannelMessage:
    """渠道实际收到或发出的一条消息，渠道适配器写入、供重建对话和排障读取，主流程不依赖它。"""
    direction: MessageDirection
    msg_id: str  # 入站是渠道的消息 ID，出站是发送时给渠道的消息 ID；同一方向内唯一
    kind: MessageKind
    created_at: datetime
    text: Optional[str] = None
    file_id: Optional[str] = None  # 内容在文件登记里；超限被拒的文件、语音没有
    filename: Optional[str] = None
    bytes: Optional[int] = None

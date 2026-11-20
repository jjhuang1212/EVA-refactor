"""向用户提问带选项时，选项的编号写法。渠道用菜单还是文字渲染，选项前都是"序号. "，模型靠序号对回自己给的选项。"""
from typing import Sequence


def label(n: int, option: str) -> str:
    return f"{n}. {option}"


def numbered(question: str, options: Sequence[str]) -> str:
    """不支持菜单的渠道，或选项放不进菜单时的文字版。"""
    lines = "\n".join(label(i, o) for i, o in enumerate(options, 1))
    return f"{question}\n{lines}\n（回复序号，或直接说你的想法）"

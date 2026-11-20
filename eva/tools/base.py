"""工具的定义与注册。模型只能通过工具碰数据，新增能力只加工具，不改循环。"""
import difflib
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

import jsonschema

from eva.contracts.errors import Code, EvaError
from eva.contracts.tooling import ToolContext
from eva.contracts.vocab import Concurrency


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema，顶层必须是 object
    run: Optional[Callable[[dict, ToolContext], Union[str, dict]]]
    concurrency: Concurrency = Concurrency.EXCLUSIVE  # 默认保守：工具自己声明才允许并行
    # 停放工具不执行：turn 在此结束，调用的结果由用户的下一条输入提供。参数里的 question 展示给用户
    parks: bool = False
    # 结果过大时是否允许被溢出替换。读取工具自己的结果不溢出，否则会"读取 → 溢出 → 再读取"循环
    spills: bool = True
    # 成功调用后循环写 context_reset 并结束本 turn，工具返回的文字就是给用户的回复。工具自己不写日志
    resets: bool = False


class ToolRegistry:
    def __init__(self, tools: List[Tool] = ()):
        self._tools: Dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if not tool.name or tool.name in self._tools:
            raise EvaError(Code.INVALID_VALUE, "工具名为空或重复", name=tool.name)
        if tool.parameters.get("type") != "object":
            raise EvaError(Code.INVALID_VALUE, "工具参数模式顶层必须是 object", name=tool.name)
        jsonschema.Draft7Validator.check_schema(tool.parameters)
        if (tool.run is None) != tool.parks:
            raise EvaError(Code.INVALID_VALUE, "停放工具没有 run，其他工具必须有", name=tool.name)
        if tool.parks and "question" not in tool.parameters.get("required", []):
            raise EvaError(Code.INVALID_VALUE, "停放工具的 question 参数必填", name=tool.name)
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return list(self._tools)

    def parking_names(self) -> List[str]:
        return [t.name for t in self._tools.values() if t.parks]

    def get(self, name: str) -> Tool:
        """没注册抛 E6002，details 里带相近的名字，让模型改名重试。"""
        try:
            return self._tools[name]
        except KeyError:
            raise EvaError(Code.TOOL_NOT_FOUND, "没有这个工具", name=name,
                           similar=difflib.get_close_matches(str(name), list(self._tools), n=3)) from None

    def schemas(self) -> List[dict]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}} for t in self._tools.values()]

    def parse_arguments(self, tool: Tool, raw: str) -> dict:
        """模型给的参数串解析并校验。不合法抛 E6003，不带着残缺参数去执行。"""
        try:
            args = json.loads(raw) if raw else {}
        except ValueError as e:
            raise EvaError(Code.TOOL_ARGS_INVALID, "参数不是合法 JSON", tool=tool.name, reason=str(e)) from None
        if not isinstance(args, dict):
            raise EvaError(Code.TOOL_ARGS_INVALID, "参数必须是 JSON 对象", tool=tool.name)
        try:
            jsonschema.validate(args, tool.parameters)
        except jsonschema.ValidationError as e:
            raise EvaError(Code.TOOL_ARGS_INVALID, "参数不符合工具的参数模式", tool=tool.name,
                           reason=e.message, path=list(e.absolute_path)) from None
        return args

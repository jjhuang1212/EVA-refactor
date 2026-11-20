"""评测里模型扮演用户：agent 中途提问时，由模型按提示词和案例的补充信息替用户回答。
调用走同一个模型调用层，记账用 Purpose.USER_SIM，挂在它自己的会话上，不占 agent 会话的预算，也不算进案例的 max_cost_cny。"""
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import yaml

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import Purpose
from eva.model.client import ModelClient

CONFIG = Path(__file__).resolve().parent / "config.yaml"


def load_config(path: Path = CONFIG) -> dict:
    """max_sim_rounds（正整数）和 user_sim_prompt（非空文本）；缺项或类型不对抛 E0003。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        rounds, prompt = raw["max_sim_rounds"], raw["user_sim_prompt"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了评测配置的 max_sim_rounds、user_sim_prompt", path=str(path)) from e
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise EvaError(Code.CONFIG_INVALID, "max_sim_rounds 必须是正整数", value=repr(rounds))
    if not isinstance(prompt, str) or not prompt.strip():
        raise EvaError(Code.CONFIG_INVALID, "user_sim_prompt 必须是非空文本")
    return {"max_sim_rounds": rounds, "user_sim_prompt": prompt}


class UserSim:
    """一次运行一个。对话从模拟用户的视角记：它自己说的是 assistant，agent 说的是 user。"""

    def __init__(self, model: ModelClient, session_id: str, prompt: str, persona: Optional[str], first_message: str):
        system = prompt.rstrip() + (f"\n\n补充信息：\n{persona.strip()}" if persona else "")
        self._model, self._session = model, session_id
        self._messages: List[dict] = [{"role": "system", "content": system},
                                      {"role": "assistant", "content": first_message}]

    def reply(self, question: str) -> str:
        """模型调用失败直接抛；模型没给出文字也抛，不拿空串当回答发给 agent。"""
        self._messages.append({"role": "user", "content": question})
        res = self._model.call(self._session, Purpose.USER_SIM.value, self._messages)
        if not res.content or not res.content.strip():
            raise EvaError(Code.MODEL_UNAVAILABLE, "模拟用户的模型没有返回文字", finish_reason=res.finish_reason)
        self._messages.append({"role": "assistant", "content": res.content})
        return res.content.strip()

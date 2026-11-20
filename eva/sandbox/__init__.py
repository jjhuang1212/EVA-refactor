"""沙箱：执行不可信代码的通用能力。接口见 base.Sandbox，后端由 configs/sandbox.yaml 选择。"""
from eva.contracts.errors import Code, EvaError
from eva.sandbox.base import Artifact, ExecResult, Sandbox
from eva.sandbox.config import ENFORCEMENTS, SandboxConfig


def create(cfg: SandboxConfig) -> Sandbox:
    """按配置建后端。后端的隔离程度低于 require_enforcement 时启动即报错，不降级。"""
    if cfg.backend == "docker":
        from eva.sandbox.docker import DockerSandbox
        box = DockerSandbox(cfg)
    else:
        from eva.sandbox.local import LocalSandbox
        box = LocalSandbox(cfg)
    if ENFORCEMENTS.index(box.enforcement) < ENFORCEMENTS.index(cfg.require_enforcement):
        raise EvaError(Code.CONFIG_INVALID, "沙箱后端的隔离程度达不到配置要求", backend=cfg.backend,
                       enforcement=box.enforcement, required=cfg.require_enforcement)
    return box

"""全部错误码的唯一定义处。

错误码集中在这里，不散落、不就地新增（CLAUDE.md 功能权限隔离第 7 条）。
有合适的就复用，语义不符才新增；拿不准语义时先问用户。

编号按功能层分段，日志和结构化错误里只看编号，代码里用名字：
E0xxx 通用 / E1xxx 租户与权限 / E2xxx 文件与对象存储 / E3xxx 会话与 transcript /
E4xxx 账本与预算 / E5xxx 模型调用 / E6xxx 工具与沙箱 / E7xxx 渠道 / E9xxx 基础设施。
"""
from enum import Enum


class Code(Enum):
    """值是编号；每个码必须同时声明可重试性，漏写会在导入时报错。"""

    def __new__(cls, number: str, retryable: bool):
        obj = object.__new__(cls)
        obj._value_ = number
        obj.retryable = retryable
        return obj

    INVALID_VALUE = ("E0001", False)  # 取值不在集中定义的集合里
    CONFIG_INVALID = ("E0003", False)  # 非模型的配置文件缺项或取值非法
    TENANT_REQUIRED = ("E1001", False)  # 按租户的读写没带 tenant_id 或为空
    TENANT_NOT_FOUND = ("E1002", False)  # 引用了不存在的租户，先建租户
    LOGIN_REQUIRED = ("E1003", False)  # EVA测评系统：没登录、登录过期、密码不对或失败次数过多被暂时锁定，reason 在 details
    COMPANY_NOT_FOUND = ("E0002", False)  # 引用的 USCC 不在公司主数据里
    FILE_NOT_FOUND =("E2001", False)  # 文件登记不存在，或不属于当前租户
    FILE_UNSUPPORTED = ("E2002", False)  # 文件类型暂不支持读取，或超过读取大小上限，reason 在 details
    SESSION_NOT_FOUND =("E3002", False)  # 会话不存在，或不属于当前租户（对调用方一视同仁，不泄露别人的）
    SESSION_BUSY = ("E3003", True)  # 该会话已有一次在途运行，调用方排队或回固定提示
    SEQ_CONFLICT = ("E3001", True)  # transcript 期望序号不等于当前下一个序号，重读后可重试
    DUPLICATE_IDEM = ("E4001", False)  # 账本 (idem_key, kind) 重复，调用方按"已记过"处理
    HOLD_NOT_FOUND = ("E4002", False)  # settle / release 指向的预扣不存在，或不属于当前租户
    HOLD_CLOSED = ("E4003", False)  # 该预扣已经结算或释放，二选一不能再记
    BUDGET_EXCEEDED = ("E4004", False)  # 已用额加占用额再加这笔预扣超过预算，预算耗尽即停放
    MODEL_AUTH = ("E5001", False)  # 鉴权失败（401 等），配置错误，不切备选
    MODEL_UNAVAILABLE = ("E5002", True)  # 主备都因传输层或可用性失败而不可用，details 逐模型列出；没有备选的语音转写用尽重试后也抛它
    MODEL_QUOTA_EXHAUSTED = ("E5003", False)  # 主备的额度都耗尽
    MODEL_CONTEXT_OVERFLOW = ("E5004", False)  # 输入超出模型上下文，由循环压缩后重试
    MODEL_REQUEST_REJECTED = ("E5005", False)  # 厂商以 4xx 拒绝了请求内容，重试和切备选都不会变好
    MODEL_CONFIG_INVALID = ("E5006", False)  # 模型配置缺项、缺 key 环境变量或缺单价
    MODEL_BUSY = ("E5007", True)  # 全进程在途的模型请求已满，排队超时；请求没有发出，预扣已释放
    TOOL_FAILED = ("E6001", False)  # 工具内部抛了未预期的异常，类型和消息在 details
    TOOL_NOT_FOUND = ("E6002", False)  # 模型调用了没注册的工具，details 带相近的工具名
    TOOL_ARGS_INVALID = ("E6003", True)  # 参数不是合法 JSON 对象或不符合工具的参数模式，模型改参数后可重发
    TOOL_DENIED = ("E6004", False)  # hook 拒绝了这次调用，或需要用户确认而当前无法询问；不重试
    TOOL_CALL_TRUNCATED = ("E6005", True)  # 模型输出被截断，整批工具调用未执行，让模型重发
    TOOL_ABORTED = ("E6006", False)  # 取消后没来得及执行的调用，补的合成结果
    SEARCH_UNAVAILABLE = ("E6007", True)  # 联网搜索网络失败、超时、限流、5xx、返回格式不对或排队超时；搜到零条不是错误
    SEARCH_REJECTED = ("E6008", False)  # 联网搜索被服务方拒绝（鉴权、额度、请求内容），status 和服务方的说明在 details
    ENTITY_UNAVAILABLE = ("E6009", True)  # 企业数据源（QCC）网络失败、超时、限流、5xx、返回格式不对或排队超时；查无此公司不是错误
    ENTITY_REJECTED = ("E6010", False)  # 企业数据源拒绝了请求（鉴权、额度、请求内容），status 和服务方的说明在 details
    CALLBACK_REJECTED = ("E7001", False)  # 渠道回调验签或解密失败，details.status 是该回给渠道的 HTTP 状态
    CHANNEL_UNAVAILABLE = ("E7002", True)  # 渠道接口网络失败、超时、5xx 或限流
    CHANNEL_REJECTED = ("E7003", False)  # 渠道接口返回业务错误，errcode 和 errmsg 在 details
    DELIVERY_LIMITED = ("E7004", False)  # 超出渠道的下发窗口或条数配额，消息没有发出
    SANDBOX_UNAVAILABLE = ("E8001", True)  # 沙箱本身起不来（docker 不通、镜像缺失、启动失败），代码没有运行
    SANDBOX_TIMEOUT = ("E8002", False)  # 代码超过单次运行的时限，已被终止；同样的代码重跑还会超时
    SANDBOX_OUTPUT_LIMIT = ("E8003", False)  # stdout/stderr、工作目录总量或文件个数超限，reason 在 details
    SANDBOX_ARTIFACT_REJECTED = ("E8004", False)  # 产物文件类型不在白名单或单文件过大，文件清单在 details
    SANDBOX_MEMORY_EXCEEDED = ("E8005", False)  # 容器因内存超限被杀（退出码 137）
    EVAL_BUSY = ("E3005", True)  # 已有一个评测任务在跑，等它结束或取消后再启动
    FILE_CHANGED = ("E3006", False)  # EVA测评系统保存文件时，磁盘上的内容和打开时不一样（在别处改过），重新读取后再改
    COMPACTION_FAILED = ("E3004", False) # 压缩没有产出可用的摘要（为空、被截断、没有可压缩的内容），不落不完整的摘要
    STORAGE_UNAVAILABLE = ("E9001", True)  # 连不上、超时、死锁牺牲者
    STORAGE_ERROR = ("E9002", False)  # 其他数据库错误和存储配置缺失，原始异常在 details
    MIGRATION_DRIFT = ("E9003", False)  # 迁移脚本与库里的执行记录对不上
    RECONCILE_MISMATCH = ("E9004", False)  # 数据迁移后对账不平，源数据没有全部落到目标


class EvaError(Exception):
    def __init__(self, code: Code, message: str = "", **details):
        super().__init__(f"{code.value} {code.name} {message}".strip())
        self.code = code
        self.message = message
        self.details = details

    @property
    def retryable(self) -> bool:
        return self.code.retryable

    def to_dict(self) -> dict:
        """结构化形式，工具错误以此返回给模型，让它决定重试、换路或放弃。"""
        return {
            "code": self.code.value,
            "name": self.code.name,
            "retryable": self.retryable,
            "message": self.message,
            "details": self.details,
        }


"""configs/tools.yaml 的加载与校验。"""
import dataclasses
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Tuple

import yaml

from eva.contracts.document import ParseLimits
from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import MEMORY_TEXT_MAX, SearchEngine, check
from eva.sandbox.config import SandboxConfig

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "tools.yaml"


@dataclass(frozen=True)
class ReadFileConfig:
    page_bytes: int
    max_file_bytes: int
    text_extensions: Tuple[str, ...]


PARSING_PATH = Path(__file__).resolve().parents[2] / "configs" / "parsing.yaml"


@dataclass(frozen=True)
class AudioConfig:
    extensions: Tuple[str, ...]  # 当作录音读取的扩展名（小写，带点）
    sample_rate: int
    target_peak_db: float
    max_gain_db: float
    silence_peak_db: float
    block_max_seconds: int  # 转写文字按这个时长（和块字符上限，先到者为准）分块，一块一个时间段锚点


@dataclass(frozen=True)
class VisionFillConfig:
    concurrency: int  # 一份文件里同时问视觉模型的图数
    max_output_tokens: int  # 一张图的描述最多多少 token


@dataclass(frozen=True)
class ParsingConfig:
    max_file_bytes: int
    sandbox: SandboxConfig  # 解析专用：镜像、内存、时限覆盖后的沙箱配置
    limits: ParseLimits
    audio: AudioConfig
    vision: VisionFillConfig


def _audio(raw) -> AudioConfig:
    if not isinstance(raw, dict):
        raise EvaError(Code.CONFIG_INVALID, "解析配置需要 audio 小节")
    ext = raw.get("extensions")
    if not isinstance(ext, list) or not ext or not all(
            isinstance(x, str) and x.startswith(".") and x == x.lower() and len(x) > 1 for x in ext):
        raise EvaError(Code.CONFIG_INVALID, "audio.extensions 必须是小写、带点的扩展名列表", value=repr(ext))
    levels = {}
    for key in ("target_peak_db", "max_gain_db", "silence_peak_db"):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EvaError(Code.CONFIG_INVALID, f"audio.{key} 必须是数（dB）", value=repr(v))
        levels[key] = float(v)
    if not (levels["silence_peak_db"] < levels["target_peak_db"] <= 0) or levels["max_gain_db"] < 0:
        raise EvaError(Code.CONFIG_INVALID, "audio 的电平需满足 silence_peak_db < target_peak_db <= 0 且 max_gain_db >= 0", **levels)
    return AudioConfig(tuple(ext), _positive(raw, "sample_rate", "audio."), levels["target_peak_db"], levels["max_gain_db"],
                       levels["silence_peak_db"], _positive(raw, "block_max_seconds", "audio."))


def _positive(section: dict, key: str, label: str, kind=int):
    v = section.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (kind is int and v != int(v)):
        raise EvaError(Code.CONFIG_INVALID, f"{label}{key} 必须是正数" + ("（整数）" if kind is int else ""), value=repr(v))
    return kind(v)


def load_parsing(base: SandboxConfig, path: Path = PARSING_PATH) -> ParsingConfig:
    """base 是 sandbox.yaml 的配置，解析专用的覆盖项叠在它上面。解析沙箱只产出结果 json，录音另有一个标准 wav，所以只放行这两种。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了解析配置", path=str(path), error=str(e)) from e
    if not isinstance(raw, dict) or not isinstance(raw.get("sandbox"), dict) or not isinstance(raw.get("limits"), dict):
        raise EvaError(Code.CONFIG_INVALID, "解析配置需要 sandbox 和 limits 小节", path=str(path))
    box, lim = raw["sandbox"], raw["limits"]
    image = box.get("image")
    if not isinstance(image, str) or not image:
        raise EvaError(Code.CONFIG_INVALID, "sandbox.image 必须是非空字符串", value=repr(image))
    sandbox = dataclasses.replace(
        base, timeout_seconds=_positive(box, "timeout_seconds", "sandbox.", float),
        max_concurrency=_positive(box, "max_concurrency", "sandbox."), allowed_extensions=(".json", ".wav", ".png"),
        max_file_bytes=_positive(box, "max_file_bytes", "sandbox."), max_workdir_bytes=_positive(box, "max_workdir_bytes", "sandbox."),
        docker=dataclasses.replace(base.docker, image=image, memory_mb=_positive(box, "memory_mb", "sandbox."),
                                   tmpfs_mb=_positive(box, "tmpfs_mb", "sandbox.")))
    images, languages = lim.get("ocr_max_images"), lim.get("ocr_languages")
    if isinstance(images, bool) or not isinstance(images, int) or images < 0:
        raise EvaError(Code.CONFIG_INVALID, "limits.ocr_max_images 必须是非负整数（0 为关闭 OCR）", value=repr(images))
    if not isinstance(languages, str) or not re.fullmatch(r"[a-z_]+(\+[a-z_]+)*", languages):
        raise EvaError(Code.CONFIG_INVALID, "limits.ocr_languages 必须形如 chi_sim+eng", value=repr(languages))
    sizes = (_positive(lim, k, "limits.") for k in ("block_max_chars", "table_rows_per_block", "max_blocks"))
    see = lim.get("vision_max_images")
    if isinstance(see, bool) or not isinstance(see, int) or not 0 <= see < base.max_files:
        raise EvaError(Code.CONFIG_INVALID, "limits.vision_max_images 必须是非负整数（0 为不看图），且小于沙箱的 max_files（结果文件还占一个）",
                       value=repr(see), max_files=base.max_files)
    blank = lim.get("vision_blank_stddev")
    if isinstance(blank, bool) or not isinstance(blank, (int, float)) or blank < 0:
        raise EvaError(Code.CONFIG_INVALID, "limits.vision_blank_stddev 必须是非负数", value=repr(blank))
    low = lim.get("vision_low_confidence")
    if isinstance(low, bool) or not isinstance(low, int) or not 0 <= low <= 100:
        raise EvaError(Code.CONFIG_INVALID, "limits.vision_low_confidence 必须是 0 到 100 的整数（0 为不按置信度判断）", value=repr(low))
    watching = raw.get("vision")
    if not isinstance(watching, dict):
        raise EvaError(Code.CONFIG_INVALID, "解析配置需要 vision 小节", path=str(path))
    return ParsingConfig(_positive(raw, "max_file_bytes", ""), sandbox,
                         ParseLimits(*sizes, images, _positive(lim, "ocr_min_image_pixels", "limits."), _positive(lim, "ocr_dpi", "limits."),
                                     languages, see, float(blank), _positive(lim, "vision_max_image_bytes", "limits."), low),
                         _audio(raw.get("audio")),
                         VisionFillConfig(_positive(watching, "concurrency", "vision."), _positive(watching, "max_output_tokens", "vision.")))


def load_read_file(path: Path = DEFAULT_PATH) -> ReadFileConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("read_file") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 read_file 小节", path=str(path))
    values = {}
    for key in ("page_bytes", "max_file_bytes"):
        v = section.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise EvaError(Code.CONFIG_INVALID, f"read_file.{key} 必须是正整数", value=repr(v))
        values[key] = v
    ext = section.get("text_extensions")
    if not isinstance(ext, list) or not ext or not all(
            isinstance(x, str) and x.startswith(".") and x == x.lower() and len(x) > 1 for x in ext):
        raise EvaError(Code.CONFIG_INVALID, "read_file.text_extensions 必须是小写、带点的扩展名列表", value=repr(ext))
    return ReadFileConfig(values["page_bytes"], values["max_file_bytes"], tuple(ext))


@dataclass(frozen=True)
class DeliverFileConfig:
    max_file_bytes: int


def load_deliver_file(path: Path = DEFAULT_PATH) -> DeliverFileConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("deliver_file") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 deliver_file 小节", path=str(path))
    v = section.get("max_file_bytes")
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise EvaError(Code.CONFIG_INVALID, "deliver_file.max_file_bytes 必须是正整数", value=repr(v))
    return DeliverFileConfig(v)


@dataclass(frozen=True)
class ListFilesConfig:
    max_results: int


def load_list_files(path: Path = DEFAULT_PATH) -> ListFilesConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("list_files") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 list_files 小节", path=str(path))
    return ListFilesConfig(_positive(section, "max_results", "list_files."))


@dataclass(frozen=True)
class ViewImageConfig:
    max_file_bytes: int
    max_question_chars: int
    max_output_tokens: int
    max_edge: int


def load_view_image(path: Path = DEFAULT_PATH) -> ViewImageConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("view_image") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 view_image 小节", path=str(path))
    values = []
    for key in ("max_file_bytes", "max_question_chars", "max_output_tokens", "max_edge"):
        v = section.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise EvaError(Code.CONFIG_INVALID, f"view_image.{key} 必须是正整数", value=repr(v))
        values.append(v)
    return ViewImageConfig(*values)


@dataclass(frozen=True)
class ProofreadConfig:
    batch_chars: int  # 一批送去校订的块，正文合计不超过这么多字（单块更长就它自己一批）
    context_blocks: int  # 每批前后各附几块上下文，只读不改
    concurrency: int  # 同时校订几批
    max_output_tokens: int  # 一批的输出上限，要大于 batch_chars 对应的 token 数
    max_hint_chars: int  # 线索（材料、热词、公司全称）的字数上限
    max_redo: int  # 一次最多重做几块
    max_file_bytes: int  # 校订输入文件的大小上限


def load_proofread(path: Path = DEFAULT_PATH) -> ProofreadConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("proofread") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 proofread 小节", path=str(path))
    keys = ("batch_chars", "context_blocks", "concurrency", "max_output_tokens", "max_hint_chars", "max_redo", "max_file_bytes")
    values = []
    for key in keys:
        v = section.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < (0 if key == "context_blocks" else 1):
            raise EvaError(Code.CONFIG_INVALID, f"proofread.{key} 必须是{'非负' if key == 'context_blocks' else '正'}整数", value=repr(v))
        values.append(v)
    return ProofreadConfig(*values)


@dataclass(frozen=True)
class WebSearchConfig:
    engine: SearchEngine
    price_cny_per_call: Decimal
    pricing_version: str
    max_query_chars: int
    default_results: int
    max_results: int
    content_max_chars: int
    timeout_seconds: float
    max_concurrency: int


def load_web_search(path: Path = DEFAULT_PATH) -> WebSearchConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("web_search") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 web_search 小节", path=str(path))
    price = Decimal(str(_positive(section, "price_cny_per_call", "web_search.", float)))
    version = section.get("pricing_version")
    if not isinstance(version, str) or not version.strip():
        raise EvaError(Code.CONFIG_INVALID, "web_search.pricing_version 必须是非空字符串", value=repr(version))
    try:
        engine = check(SearchEngine, section.get("engine"))
    except EvaError as e:
        raise EvaError(Code.CONFIG_INVALID, "web_search.engine 不是支持的引擎", value=repr(section.get("engine"))) from e
    cfg = WebSearchConfig(
        engine, price, version.strip(), _positive(section, "max_query_chars", "web_search."),
        _positive(section, "default_results", "web_search."), _positive(section, "max_results", "web_search."),
        _positive(section, "content_max_chars", "web_search."), _positive(section, "timeout_seconds", "web_search.", float),
        _positive(section, "max_concurrency", "web_search."))
    if price != price.quantize(Decimal("0.0001")):  # 账本金额最多 4 位小数
        raise EvaError(Code.CONFIG_INVALID, "web_search.price_cny_per_call 最多 4 位小数", value=str(price))
    if cfg.max_query_chars > 70:  # 智谱的建议上限，再长召回变差
        raise EvaError(Code.CONFIG_INVALID, "web_search.max_query_chars 不能超过 70", value=cfg.max_query_chars)
    if not 1 <= cfg.default_results <= cfg.max_results <= 50:  # 50 是智谱单次上限
        raise EvaError(Code.CONFIG_INVALID, "web_search 条数需满足 1 <= default_results <= max_results <= 50",
                       default_results=cfg.default_results, max_results=cfg.max_results)
    return cfg



@dataclass(frozen=True)
class SearchHotwordsConfig:
    max_terms: int
    max_per_term: int
    min_similarity: float
    cache_seconds: float


def load_search_hotwords(path: Path = DEFAULT_PATH) -> SearchHotwordsConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("search_hotwords") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 search_hotwords 小节", path=str(path))
    cfg = SearchHotwordsConfig(_positive(section, "max_terms", "search_hotwords."), _positive(section, "max_per_term", "search_hotwords."),
                               _positive(section, "min_similarity", "search_hotwords.", float),
                               _positive(section, "cache_seconds", "search_hotwords.", float))
    if cfg.min_similarity > 1:
        raise EvaError(Code.CONFIG_INVALID, "search_hotwords.min_similarity 不能大于 1", value=cfg.min_similarity)
    return cfg


@dataclass(frozen=True)
class ConfirmTermConfig:
    max_terms_per_tenant: int
    max_term_chars: int


def load_confirm_term(path: Path = DEFAULT_PATH) -> ConfirmTermConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("confirm_term") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 confirm_term 小节", path=str(path))
    return ConfirmTermConfig(_positive(section, "max_terms_per_tenant", "confirm_term."), _positive(section, "max_term_chars", "confirm_term."))


@dataclass(frozen=True)
class MemoryConfig:
    max_memories_per_tenant: int
    max_memory_chars: int


def load_memory(path: Path = DEFAULT_PATH) -> MemoryConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("memory") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 memory 小节", path=str(path))
    cfg = MemoryConfig(_positive(section, "max_memories_per_tenant", "memory."), _positive(section, "max_memory_chars", "memory."))
    if cfg.max_memory_chars > MEMORY_TEXT_MAX:  # 超过列宽，存的时候才报错就晚了
        raise EvaError(Code.CONFIG_INVALID, "memory.max_memory_chars 不能超过存储列宽", max=MEMORY_TEXT_MAX)
    return cfg


@dataclass(frozen=True)
class QccDimension:
    server: str
    tool: str
    list_key: str
    person: bool
    about: str
    price_cny: Decimal
    alt_list_keys: tuple = ()  # 记录列表的别名：合伙企业的股东维度返回的是「合伙人信息」
    single: bool = False  # 返回本身就是一条记录（工商登记），没有记录列表，list_key 为空


@dataclass(frozen=True)
class QccConfig:
    domain: str
    server_path: str
    timeout_seconds: float
    max_concurrency: int
    max_name_chars: int
    search_price_cny: Decimal
    pricing_version: str
    cache_ttl_seconds: int
    no_record_text: str  # 服务方在"没有记录"时不返回空列表，而是在说明文字里写这句话
    dimensions: Dict[str, QccDimension]

    def url(self, server: str) -> str:
        return f"https://{self.domain}{self.server_path.format(server=server)}"


def _text(section: dict, key: str, label: str) -> str:
    v = section.get(key)
    if not isinstance(v, str) or not v.strip():
        raise EvaError(Code.CONFIG_INVALID, f"{label}{key} 必须是非空字符串", value=repr(v))
    return v.strip()


def _alt_keys(d: dict, name: str) -> tuple:
    v = d.get("alt_list_keys", [])
    if not isinstance(v, list) or not all(isinstance(k, str) and k.strip() for k in v):
        raise EvaError(Code.CONFIG_INVALID, f"qcc.dimensions.{name}.alt_list_keys 必须是非空字符串列表", value=repr(v))
    return tuple(k.strip() for k in v)


def _price(section: dict, key: str, label: str) -> Decimal:
    v = section.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise EvaError(Code.CONFIG_INVALID, f"{label}{key} 必须是不小于 0 的数", value=repr(v))
    price = Decimal(str(v))
    if price != price.quantize(Decimal("0.0001")):  # 账本金额最多 4 位小数
        raise EvaError(Code.CONFIG_INVALID, f"{label}{key} 最多 4 位小数", value=str(price))
    return price


def load_qcc(path: Path = DEFAULT_PATH) -> QccConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EvaError(Code.CONFIG_INVALID, "读不到或解析不了工具配置", path=str(path), error=str(e)) from e
    section = raw.get("qcc") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise EvaError(Code.CONFIG_INVALID, "工具配置缺 qcc 小节", path=str(path))
    domain, server_path, version = (_text(section, k, "qcc.") for k in ("domain", "server_path", "pricing_version"))
    if "/" in domain or not server_path.startswith("/") or "{server}" not in server_path:
        raise EvaError(Code.CONFIG_INVALID, "qcc.domain 只写域名，server_path 以 / 开头并含 {server}", domain=domain, server_path=server_path)
    dims = section.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        raise EvaError(Code.CONFIG_INVALID, "qcc.dimensions 必须是非空映射")
    dimensions = {}
    for name, d in dims.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z_]*", name) or not isinstance(d, dict) or not isinstance(d.get("person"), bool):
            raise EvaError(Code.CONFIG_INVALID, "qcc.dimensions 的名字用小写字母和下划线，person 必须是布尔值", dimension=repr(name))
        single = d.get("single", False)
        if not isinstance(single, bool) or (single and (d.get("list_key") or d.get("alt_list_keys"))):
            raise EvaError(Code.CONFIG_INVALID, "qcc.dimensions.single 必须是布尔值，且 single 的维度不写 list_key 和 alt_list_keys", dimension=name)
        dimensions[name] = QccDimension(*(_text(d, k, f"qcc.dimensions.{name}.") for k in ("server", "tool")),
                                        "" if single else _text(d, "list_key", f"qcc.dimensions.{name}."), d["person"],
                                        _text(d, "about", f"qcc.dimensions.{name}."), _price(d, "price_cny", f"qcc.dimensions.{name}."),
                                        _alt_keys(d, name), single)
    return QccConfig(domain, server_path, _positive(section, "timeout_seconds", "qcc.", float), _positive(section, "max_concurrency", "qcc."),
                     _positive(section, "max_name_chars", "qcc."), _price(section, "search_price_cny", "qcc."), version,
                     _positive(section, "cache_ttl_seconds", "qcc."), _text(section, "no_record_text", "qcc."), dimensions)

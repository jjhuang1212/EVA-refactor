"""文件解析结果的数据结构：一份文件解析成有序的块，每块带出处锚点。

同一份块列表三处共用：模型读取的文本、切片入库、引用校验。解析器在沙箱里跑，宿主和 hook 只认这里的结构。
"""
from dataclasses import dataclass, field
from typing import Dict, Tuple

from eva.contracts.errors import Code, EvaError
from eva.contracts.vocab import BlockKind, check


@dataclass(frozen=True)
class ParseLimits:
    """影响解析输出的上限。改了就是另一份解析结果，所以参与缓存键。"""
    block_max_chars: int  # 一个块的字符数上限
    table_rows_per_block: int  # 表格每个块最多几行数据（表头每块重复）
    max_blocks: int  # 一份文件最多几个块，超过说明文件不适合整份读取
    ocr_max_images: int  # 一份文件最多 OCR 几张图（扫描页、嵌入图、图片文件都算一张），0 为不做 OCR
    ocr_min_image_pixels: int  # 嵌入图的短边小于它不识别（logo、图标、项目符号）
    ocr_dpi: int  # 扫描页渲染成图的分辨率
    ocr_languages: str  # tesseract 语言包，如 chi_sim+eng
    vision_max_images: int = 0  # 一份文件最多留几张 OCR 没读出文字的图给视觉模型看，0 为不看
    vision_blank_stddev: float = 0.0  # 灰度标准差低于它的图当空白，不看（扫描件的空白页）
    vision_max_image_bytes: int = 8_000_000  # 留给视觉模型的图编码后的字节上限，超过就缩小
    vision_low_confidence: int = 0  # 图片文件、扫描页的 OCR 平均置信度低于它，当作没读出来交视觉模型，0 为不按置信度判断


@dataclass(frozen=True)
class AudioLimits:
    """音频转成转写用的标准格式时的参数，由宿主放进沙箱的任务里。"""
    max_seconds: int  # 超过这么长不转，抛 E2002
    sample_rate: int  # 输出 wav 的采样率，单声道 16 位
    target_peak_db: float  # 太轻的录音把峰值提到这个电平（dBFS）
    max_gain_db: float  # 提升幅度上限，免得把底噪抬成"人声"
    silence_peak_db: float  # 峰值低于这个电平就是静音，不送去转写


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


@dataclass(frozen=True)
class Block:
    ord: int  # 在文件内从 0 起的序号，翻页和引用都用它
    kind: BlockKind
    text: str
    anchor: Dict  # 必含 element，其余随格式：页码、表号、sheet 与行区间、幻灯片号、行号区间
    section_path: Tuple[str, ...]  # 所在章节的标题链，从外到内

    def __post_init__(self):
        if isinstance(self.ord, bool) or not isinstance(self.ord, int) or self.ord < 0:
            raise EvaError(Code.INVALID_VALUE, "块序号必须是非负整数", value=repr(self.ord))
        check(BlockKind, self.kind)
        if not isinstance(self.text, str) or not self.text.strip():
            raise EvaError(Code.INVALID_VALUE, "块文本不能为空", ord=self.ord)
        if not isinstance(self.anchor, dict) or not isinstance(self.anchor.get("element"), str) \
                or not self.anchor["element"]:
            raise EvaError(Code.INVALID_VALUE, "块的出处锚点缺失或没有 element", ord=self.ord, anchor=repr(self.anchor))

    def label(self) -> str:
        """给模型看的一行出处：块号、位置、章节。"""
        a = self.anchor
        parts = []
        if "page" in a:
            parts.append(f"第{a['page']}页")
        if "slide" in a:
            parts.append(f"幻灯片{a['slide']}")
        if "sheet" in a:
            parts.append(f"sheet《{a['sheet']}》")
        if "table_no" in a:
            parts.append(f"表{a['table_no']}" + (f"（在表{a['parent_table']}内）" if "parent_table" in a else ""))
        if a["element"] in ("footnote", "endnote"):
            parts.append(("脚注" if a["element"] == "footnote" else "尾注") + str(a["note_no"]))
        if a["element"] == "textbox":
            parts.append("文本框")
        if "image_no" in a:
            parts.append(f"图{a['image_no']}")
        for key, unit in (("rows", "行"), ("lines", "行"), ("paragraphs", "段")):
            if key in a:
                parts.append(f"{unit}{a[key][0]}-{a[key][1]}")
        if a["element"] == "notes":
            parts.append("备注")
        if "time_ms" in a:
            parts.append(f"录音{_clock(a['time_ms'][0])}-{_clock(a['time_ms'][1])}")
        if a.get("asr"):
            parts.append("语音识别，可能有错字，数字和专名需核对")
        if a.get("vision"):
            parts.append("看图识别，不是原文，数字和专名需核对")
        if a.get("ocr"):
            parts.append(f"OCR识别，置信度{a['confidence']}，数字和专名需核对")
        if self.section_path:
            parts.append(" > ".join(self.section_path))
        return f"[b{self.ord}] " + "，".join(parts)

    def to_dict(self) -> dict:
        return {"ord": self.ord, "kind": self.kind.value, "text": self.text, "anchor": self.anchor,
                "section_path": list(self.section_path)}

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(d["ord"], check(BlockKind, d["kind"]), d["text"], d["anchor"], tuple(d["section_path"]))


@dataclass(frozen=True)
class Parsed:
    format: str  # 实际识别出的格式（扩展名不带点），可能和文件名的扩展名不同
    parser_version: str
    blocks: Tuple[Block, ...]
    warnings: Tuple[Dict, ...] = field(default_factory=tuple)  # 没读到或读得不完整的地方，每项含 reason
    images: Dict[int, str] = field(default_factory=dict)  # 留给视觉模型的图：占位块的块序号 → 图内容的 sha256，图本身存在对象存储里

    def to_dict(self) -> dict:
        return {"format": self.format, "parser_version": self.parser_version,
                "blocks": [b.to_dict() for b in self.blocks], "warnings": list(self.warnings),
                "images": {str(o): sha for o, sha in self.images.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Parsed":
        return cls(d["format"], d["parser_version"], tuple(Block.from_dict(b) for b in d["blocks"]),
                   tuple(d["warnings"]), {int(o): sha for o, sha in d["images"].items()})

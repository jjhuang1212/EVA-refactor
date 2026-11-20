"""沙箱里的入口：读 job.json 和它指向的输入，把结果或结构化错误写进 parsed.json。

宿主靠这个文件拿结果，退出码不传错误信息；库抛出的任何异常都在这里转成"文件损坏"，不吞也不让进程崩出堆栈。
job 里有 audio 就是录音转标准 wav（结果里带 audio 小节，wav 另写 AUDIO_OUT），否则是文档解析，
留给视觉模型看的图写在 VISION_DIR 下，文件名是占位块的块序号。
"""
import json
from pathlib import Path

from eva.contracts.document import AudioLimits, ParseLimits
from eva.contracts.errors import Code, EvaError
from eva.parsing import parse_with_images

JOB = "job.json"
OUT = "parsed.json"
AUDIO_OUT = "audio.wav"
VISION_DIR = "vision"


def _run(job: dict) -> dict:
    if "audio" in job:
        from eva.parsing import audio
        return {"audio": audio.prepare(Path(job["input"]), Path(AUDIO_OUT), AudioLimits(**job["audio"]))}
    parsed, images = parse_with_images(job["filename"], Path(job["input"]).read_bytes(), ParseLimits(**job["limits"]), job["version"])
    Path(VISION_DIR).mkdir(exist_ok=True)
    for ord_, png in images.items():
        Path(VISION_DIR, f"{ord_}.png").write_bytes(png)
    return {"parsed": parsed.to_dict(), "vision": sorted(images)}


def main() -> None:
    out = Path(OUT)
    out.unlink(missing_ok=True)  # 同一工作目录里留着上一次的结果会被误读
    Path(AUDIO_OUT).unlink(missing_ok=True)
    job = json.loads(Path(JOB).read_text(encoding="utf-8"))
    try:
        result = _run(job)
    except EvaError as e:
        result = {"error": e.to_dict()}
    except Exception as e:  # noqa: BLE001 第三方解析库对坏文件抛什么都有；转成结构化错误交给宿主，不丢信息
        result = {"error": EvaError(Code.FILE_UNSUPPORTED, "文件损坏或格式不符，解析失败", reason="corrupt",
                                    error=f"{type(e).__name__}: {e}").to_dict()}
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

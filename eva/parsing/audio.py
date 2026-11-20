"""录音 → 转写用的标准 wav（单声道 16 位），只在解析沙箱里运行。

ffmpeg 解码不可信的媒体文件，所以必须在沙箱里；转写本身要联网和密钥，在宿主，不在这里。
太轻的录音先把峰值提到目标电平再送转写：同一段轻声录音，不提音量会把"扔"听成"冷"。
静音不生成 wav，由宿主当作"没有语音内容"，不花转写的钱。
"""
import audioop
import math
import subprocess
import wave
from pathlib import Path

from eva.contracts.document import AudioLimits
from eva.contracts.errors import Code, EvaError

_SAMPLE_WIDTH = 2
_STDERR_CHARS = 400


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as e:  # 镜像里没装或没带二进制：环境问题，不是用户文件的问题
        raise EvaError(Code.SANDBOX_UNAVAILABLE, "解析环境里没有 ffmpeg", error=str(e)) from e


def prepare(source: Path, out: Path, lim: AudioLimits) -> dict:
    """返回 {"seconds", "silent", "peak_db", "gain_db"}；非静音时标准 wav 写在 out。
    解不开返回 E2002 corrupt；超过 max_seconds 返回 E2002 too_long。"""
    # 只许读本地文件：录音文件里可以夹带播放列表，指向网络地址
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
           "-i", str(source), "-t", str(lim.max_seconds + 1), "-vn", "-ac", "1", "-ar", str(lim.sample_rate),
           "-c:a", "pcm_s16le", "-f", "wav", "-y", str(out)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise EvaError(Code.FILE_UNSUPPORTED, "录音无法解码，文件损坏或不是音频", reason="corrupt",
                       error=proc.stderr.decode("utf-8", "replace")[-_STDERR_CHARS:])
    with wave.open(str(out), "rb") as f:
        rate, total = f.getframerate(), f.getnframes()
        peak = max((audioop.max(c, _SAMPLE_WIDTH) for c in _chunks(f)), default=0)
    if not total:
        raise EvaError(Code.FILE_UNSUPPORTED, "录音里没有音频数据", reason="corrupt")
    seconds = total / rate
    if seconds > lim.max_seconds:
        raise EvaError(Code.FILE_UNSUPPORTED, "录音太长，暂不支持", reason="too_long", max_seconds=lim.max_seconds)
    peak_db = 20 * math.log10(peak / 32768) if peak else float("-inf")
    if peak_db < lim.silence_peak_db:
        out.unlink()
        return {"seconds": seconds, "silent": True, "peak_db": peak_db, "gain_db": 0.0}
    gain_db = min(lim.max_gain_db, max(0.0, lim.target_peak_db - peak_db))
    if gain_db > 0:
        boosted = out.with_suffix(".boost")
        with wave.open(str(out), "rb") as src, wave.open(str(boosted), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(_SAMPLE_WIDTH)
            dst.setframerate(rate)
            for chunk in _chunks(src):
                dst.writeframes(audioop.mul(chunk, _SAMPLE_WIDTH, 10 ** (gain_db / 20)))
        boosted.replace(out)
    return {"seconds": seconds, "silent": False, "peak_db": peak_db, "gain_db": gain_db}


def _chunks(f: "wave.Wave_read"):
    """一分钟一块读，长录音也不会把整段读进内存。"""
    step = f.getframerate() * 60
    while True:
        data = f.readframes(step)
        if not data:
            return
        yield data

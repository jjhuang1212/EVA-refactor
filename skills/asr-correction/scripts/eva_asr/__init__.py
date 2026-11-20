"""转写纠错的闸门：模型只给出改动清单，改动能不能应用由这里的确定性规则决定。用法见 skills/asr-correction/SKILL.md。"""
from eva_asr.clean import clean, clean_changes, clean_text
from eva_asr.diff_gate import KIND_LABELS, UNLISTED, WRONG_STAGE, GateError, apply_changes, load_gates, load_transcript, numbers, save, sound_similarity
from eva_asr.proofread import proofread_changes, same_speaker_runs

__all__ = ["clean", "clean_changes", "clean_text", "KIND_LABELS", "UNLISTED", "WRONG_STAGE", "GateError", "apply_changes", "load_gates", "load_transcript", "numbers", "proofread_changes", "same_speaker_runs", "save", "sound_similarity"]

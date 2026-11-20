"""报告组件库：拼文档树（Report），再渲染成 PDF 或 Markdown。用法见 skills/report/SKILL.md。"""
from eva_report.errors import ReportError
from eva_report.theme import load_theme
from eva_report.tree import Report

__all__ = ["Report", "ReportError", "load_theme"]

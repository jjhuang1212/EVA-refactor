"""xlsx：每个 sheet 的首个非空行当表头，其余按行段分块，块里带表头和 Excel 行号。

公式取缓存值；没有缓存值（文件不是 Excel 存的，或从没算过）的格子显示公式本身并标出未计算，不当成空格。
"""
import datetime
import io
from typing import Dict, List, Tuple

from eva.contracts.document import Block, ParseLimits
from eva.parsing.builder import Builder


def _show(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat() if value.time() == datetime.time(0) else value.isoformat(sep=" ")
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def parse(data: bytes, limits: ParseLimits) -> Tuple[Tuple[Block, ...], Tuple[dict, ...], Dict[int, bytes]]:
    from openpyxl import load_workbook

    values = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    builder, warnings = Builder(limits), []
    try:
        for sheet in values.worksheets:
            if sheet.sheet_state != "visible":
                warnings.append({"reason": "hidden_sheet", "sheet": sheet.title})
            uncomputed = 0
            header, rows = None, []
            for number, (vals, forms) in enumerate(zip(sheet.iter_rows(values_only=True),
                                                       formulas[sheet.title].iter_rows(values_only=True)), start=1):
                cells: List[str] = []
                for v, f in zip(vals, forms):
                    if v is None and isinstance(f, str) and f.startswith("="):
                        uncomputed += 1
                        cells.append(f"{f}（未计算）")
                    else:
                        cells.append(_show(v))
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    continue
                if header is None:
                    header = (number, cells)
                else:
                    rows.append((number, cells))
            if uncomputed:
                warnings.append({"reason": "formula_without_cached_value", "sheet": sheet.title, "count": uncomputed})
            if header is not None:
                builder.set_section((sheet.title,))
                anchor = {"element": "table", "sheet": sheet.title}
                if rows:
                    builder.table(header[1], rows, anchor)
                else:
                    builder.table(None, [header], anchor)  # 只有一行：没有"表头+数据"可分，当数据行显示
    finally:
        values.close()
        formulas.close()
    return builder.finish(), tuple(warnings), {}

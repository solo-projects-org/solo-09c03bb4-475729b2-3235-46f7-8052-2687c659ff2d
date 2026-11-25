"""CSV 与表单输入校验。"""

from __future__ import annotations

import csv
import io

MAX_SIDE = 4
MAX_CELL = 9999
MAX_MARGIN = 99_999_999


class ValidationError(ValueError):
    """面向用户的输入错误。"""


def parse_grid_csv(text: str) -> list[list[int]]:
    """解析区县作物面积 CSV。

    要求：
      - 1..4 行、1..4 列，矩形整齐；
      - 每格为非负整数且 ≤ 9999（至多四位）；
      - 不接受空白单元格、负号、小数或其它字符。
    """
    if text is None or not text.strip():
        raise ValidationError("CSV 内容为空，请上传至少 1×1 的数据。")
    reader = csv.reader(io.StringIO(text))
    rows: list[list[int]] = []
    for line_no, raw in enumerate(reader, start=1):
        if not raw or all(cell.strip() == "" for cell in raw):
            continue  # 容忍末尾空行
        cells = [cell.strip() for cell in raw]
        for cell in cells:
            if cell == "":
                raise ValidationError(f"第 {line_no} 行存在空单元格。")
            if not cell.isascii() or not cell.isdigit():
                raise ValidationError(
                    f"第 {line_no} 行的“{cell}”不是非负整数；"
                    "格值只能由数字组成。"
                )
            value = int(cell)
            if value > MAX_CELL:
                raise ValidationError(
                    f"第 {line_no} 行的“{cell}”超过四位（上限 {MAX_CELL}）。"
                )
        rows.append([int(cell) for cell in cells])

    if not rows:
        raise ValidationError("CSV 中没有有效数据行。")
    if len(rows) > MAX_SIDE:
        raise ValidationError(f"行数 {len(rows)} 超过上限 {MAX_SIDE}。")
    width = len(rows[0])
    if width > MAX_SIDE:
        raise ValidationError(f"列数 {width} 超过上限 {MAX_SIDE}。")
    for idx, row in enumerate(rows, start=1):
        if len(row) != width:
            raise ValidationError(f"第 {idx} 行列数与首行不一致，表格不是矩形。")
    return rows


def parse_margin(value: str) -> int | None:
    """合计输入框：空表示未录入，否则要求 0..99_999_999 的非负整数。"""
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    if not value.isascii() or not value.isdigit():
        raise ValidationError(f"合计“{value}”不是非负整数。")
    parsed = int(value)
    if parsed > MAX_MARGIN:
        raise ValidationError(f"合计“{value}”超出允许范围（0..{MAX_MARGIN}）。")
    return parsed


def parse_proposed(value: str) -> int:
    value = (value or "").strip()
    if not value.isascii() or not value.isdigit():
        raise ValidationError(f"拟改值“{value}”不是非负整数。")
    parsed = int(value)
    if parsed > MAX_CELL:
        raise ValidationError(f"拟改值“{value}”超过四位（上限 {MAX_CELL}）。")
    return parsed


MAX_BATCH = 2_147_483_647  # SQLite INTEGER 上限


def parse_library(value: str) -> str:
    """馆名：去首尾空白后 1..80 个字符。"""
    name = (value or "").strip()
    if not name:
        raise ValidationError("馆名不能为空。")
    if len(name) > 80:
        raise ValidationError("馆名过长（上限 80 个字符）。")
    return name


def parse_batch_no(value: str) -> int:
    """馆方入库批次号：非负整数，按馆独立递增。"""
    text = (value or "").strip()
    if not text.isascii() or not text.isdigit():
        raise ValidationError(f"批次号“{text}”必须是非负整数。")
    parsed = int(text)
    if parsed > MAX_BATCH:
        raise ValidationError(f"批次号“{text}”超出允许范围（0..{MAX_BATCH}）。")
    return parsed


def parse_voucher_ids(values: list[str] | None) -> list[int]:
    """当前采用的全部凭据号；允许为空（表示该馆已撤下全部勘误）。"""
    out: list[int] = []
    for raw in values or []:
        text = (raw or "").strip()
        if not text:
            continue
        if not text.isascii() or not text.isdigit():
            raise ValidationError(f"凭据号“{text}”必须是非负整数。")
        out.append(int(text))
    return sorted(set(out))

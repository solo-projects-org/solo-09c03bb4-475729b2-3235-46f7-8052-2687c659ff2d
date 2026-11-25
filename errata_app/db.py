"""SQLite 持久化层。

凭据失效规则（核心业务约束）：
  - 凭据保存原值、拟改值以及所引用的合计（引用时的值快照）；
  - 一旦 *被引用* 的合计被修改，相关凭据立即标记为失效（同一事务内完成）；
  - 未被任何凭据引用的合计更正，不影响已有凭据。

馆方回执规则：
  - 每个数据集内，馆名唯一；每馆按自己的批次号独立更新；
  - 批次号小于当前值 = 迟到的旧批次，不得替换；等于当前值时，内容相同
    沿用原回执、内容不同则拒收；大于当前值才替换；
  - 任何登记失败都回滚，原回执原样保留；
  - 保存合计更正（update_margins）*不* 读取或改写任何回执；回查清单是在
    读取时按现行凭据状态即时计算的。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    grid_json   TEXT NOT NULL,
    rows_n      INTEGER NOT NULL,
    cols_n      INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS margins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    entry       INTEGER NOT NULL,           -- 0..rows-1 行合计；rows..rows+cols-1 列合计
    kind        TEXT NOT NULL,              -- 'row' | 'col'
    idx         INTEGER NOT NULL,           -- 行号/列号（0 起）
    value       INTEGER,                    -- NULL 表示尚未录入
    updated_at  TEXT NOT NULL,
    UNIQUE(dataset_id, entry)
);

CREATE TABLE IF NOT EXISTS vouchers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    target_row    INTEGER NOT NULL,
    target_col    INTEGER NOT NULL,
    original_value INTEGER NOT NULL,
    proposed_value INTEGER NOT NULL,
    status        TEXT NOT NULL,            -- 'valid' | 'invalid'
    result_json   TEXT NOT NULL,            -- solver.analyze 的完整结论
    created_at    TEXT NOT NULL,
    invalidated_at TEXT
);

CREATE TABLE IF NOT EXISTS voucher_margins (
    voucher_id   INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    margin_id    INTEGER NOT NULL REFERENCES margins(id) ON DELETE CASCADE,
    value_at_cite INTEGER NOT NULL,         -- 引用瞬间的合计值（凭据快照）
    PRIMARY KEY (voucher_id, margin_id)
);

-- 馆方回执：每馆（在本表内）的当前采用凭据集合。
-- 采用值由所报凭据的拟改值导出；登记时即校验“同一格只有一个采用值”。
-- 回执只记录“报了哪些凭据号”，不复制凭据快照；回查清单在读取时按
-- 现行凭据状态即时计算，因此保存合计更正无需遍历回执。
CREATE TABLE IF NOT EXISTS receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    library       TEXT NOT NULL,
    batch_no      INTEGER NOT NULL,          -- 馆方入库批次号（同馆独立递增）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(dataset_id, library)
);

CREATE TABLE IF NOT EXISTS receipt_vouchers (
    receipt_id    INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    voucher_id    INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    PRIMARY KEY (receipt_id, voucher_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #
def create_dataset(conn, name: str, grid: list[list[int]]) -> int:
    cur = conn.execute(
        "INSERT INTO datasets(name, grid_json, rows_n, cols_n, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, json.dumps(grid, ensure_ascii=False), len(grid), len(grid[0]), now_iso()),
    )
    dataset_id = cur.lastrowid
    r, c = len(grid), len(grid[0])
    for k in range(r + c):
        kind = "row" if k < r else "col"
        idx = k if k < r else k - r
        conn.execute(
            "INSERT INTO margins(dataset_id, entry, kind, idx, value, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (dataset_id, k, kind, idx, now_iso()),
        )
    conn.commit()
    return dataset_id


def get_dataset(conn, dataset_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
    ).fetchone()


def list_datasets(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM datasets ORDER BY id DESC"
    ).fetchall()


def get_margins(conn, dataset_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM margins WHERE dataset_id = ? ORDER BY entry",
        (dataset_id,),
    ).fetchall()


def update_margins(conn, dataset_id: int, values_by_entry: dict[int, int | None]) -> dict:
    """更新合计，并把引用了被改动合计的有效凭据在同一事务内立即标失效。

    仅当新值与现值不同（含 NULL↔非 NULL）才算“修改”。
    返回 {'changed': [...], 'invalidated_voucher_ids': [...]}。
    """
    changed_entries: list[int] = []
    conn.execute("BEGIN")
    try:
        for entry, new_value in values_by_entry.items():
            row = conn.execute(
                "SELECT id, value FROM margins WHERE dataset_id = ? AND entry = ?",
                (dataset_id, entry),
            ).fetchone()
            if row is None:
                raise ValueError(f"合计条目 {entry} 不存在")
            if row["value"] != new_value:
                conn.execute(
                    "UPDATE margins SET value = ?, updated_at = ? WHERE id = ?",
                    (new_value, now_iso(), row["id"]),
                )
                changed_entries.append(entry)

        invalidated: list[int] = []
        if changed_entries:
            placeholders = ",".join("?" for _ in changed_entries)
            hits = conn.execute(
                f"""
                SELECT DISTINCT v.id
                FROM vouchers v
                JOIN voucher_margins vm ON vm.voucher_id = v.id
                JOIN margins m ON m.id = vm.margin_id
                WHERE v.dataset_id = ? AND v.status = 'valid'
                  AND m.entry IN ({placeholders})
                """,
                (dataset_id, *changed_entries),
            ).fetchall()
            invalidated = [h["id"] for h in hits]
            for vid in invalidated:
                conn.execute(
                    "UPDATE vouchers SET status = 'invalid', invalidated_at = ? "
                    "WHERE id = ?",
                    (now_iso(), vid),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"changed": changed_entries, "invalidated_voucher_ids": invalidated}


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
def save_voucher(conn, dataset_id: int, target_row: int, target_col: int,
                 original_value: int, proposed_value: int, result: dict) -> int:
    cur = conn.execute(
        "INSERT INTO vouchers(dataset_id, target_row, target_col, original_value, "
        "proposed_value, status, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'valid', ?, ?)",
        (dataset_id, target_row, target_col, original_value, proposed_value,
         json.dumps(result, ensure_ascii=False), now_iso()),
    )
    voucher_id = cur.lastrowid
    for cited in result.get("cited_entries", []):
        margin = conn.execute(
            "SELECT id FROM margins WHERE dataset_id = ? AND entry = ?",
            (dataset_id, cited["entry"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO voucher_margins(voucher_id, margin_id, value_at_cite) "
            "VALUES (?, ?, ?)",
            (voucher_id, margin["id"], cited["value"]),
        )
    conn.commit()
    return voucher_id


def get_voucher(conn, voucher_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()


def get_voucher_citations(conn, voucher_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT vm.value_at_cite, m.entry, m.kind, m.idx, m.value AS current_value,
               m.updated_at
        FROM voucher_margins vm
        JOIN margins m ON m.id = vm.margin_id
        WHERE vm.voucher_id = ?
        ORDER BY m.entry
        """,
        (voucher_id,),
    ).fetchall()


def list_vouchers(conn, dataset_id: int | None = None) -> list[sqlite3.Row]:
    if dataset_id is None:
        return conn.execute(
            "SELECT * FROM vouchers ORDER BY id DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM vouchers WHERE dataset_id = ? ORDER BY id DESC",
        (dataset_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# 馆方回执
# --------------------------------------------------------------------------- #
class ReceiptError(ValueError):
    """回执登记被拒。调用方须保证原回执保持不变。"""


def _load_receipt_voucher_ids(conn, receipt_id: int) -> list[int]:
    return [
        r["voucher_id"]
        for r in conn.execute(
            "SELECT voucher_id FROM receipt_vouchers WHERE receipt_id = ? "
            "ORDER BY voucher_id",
            (receipt_id,),
        )
    ]


def get_receipts(conn, dataset_id: int) -> list[dict]:
    """按馆名排序列出本表全部当前回执（每馆一条）及其所报凭据号。"""
    out: list[dict] = []
    for r in conn.execute(
        "SELECT * FROM receipts WHERE dataset_id = ? ORDER BY library",
        (dataset_id,),
    ):
        item = dict(r)
        item["voucher_ids"] = _load_receipt_voucher_ids(conn, r["id"])
        out.append(item)
    return out


def get_receipt(conn, dataset_id: int, library: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM receipts WHERE dataset_id = ? AND library = ?",
        (dataset_id, library),
    ).fetchone()
    if r is None:
        return None
    item = dict(r)
    item["voucher_ids"] = _load_receipt_voucher_ids(conn, r["id"])
    return item


def _receipt_conflict(vouchers: dict[int, sqlite3.Row], wanted: list[int]) -> str | None:
    """同一格只允许一个采用值；返回人类可读的冲突说明或 None。"""
    seen: dict[tuple[int, int], tuple[int, int]] = {}
    for vid in wanted:
        v = vouchers[vid]
        cell = (v["target_row"], v["target_col"])
        value = v["proposed_value"]
        if cell in seen and seen[cell][1] != value:
            other_vid, other_val = seen[cell]
            return (
                f"同一格只能报一个采用值：第 {cell[0] + 1} 行第 {cell[1] + 1} 列"
                f"同时报了凭据 #{other_vid}（{other_val}）与凭据 #{vid}（{value}）。"
            )
        seen[cell] = (vid, value)
    return None


def register_receipt(conn, dataset_id: int, library: str, batch_no: int,
                     voucher_ids: list[int]) -> dict:
    """登记/更新一馆的当前回执。

    返回 {'status': 'created'|'replaced'|'reused', ...}；任何拒收都抛
    ReceiptError，且事务回滚，原回执原样保留。
    """
    library = (library or "").strip()
    if not library:
        raise ReceiptError("馆名不能为空。")
    wanted = sorted({int(v) for v in voucher_ids})

    conn.execute("BEGIN IMMEDIATE")
    try:
        if get_dataset(conn, dataset_id) is None:
            raise ReceiptError("数据集不存在。")

        vouchers: dict[int, sqlite3.Row] = {}
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            rows = conn.execute(
                f"SELECT id, target_row, target_col, proposed_value FROM vouchers "
                f"WHERE dataset_id = ? AND id IN ({placeholders})",
                (dataset_id, *wanted),
            ).fetchall()
            vouchers = {r["id"]: r for r in rows}
            missing = [vid for vid in wanted if vid not in vouchers]
            if missing:
                listed = "、".join(f"#{v}" for v in missing)
                raise ReceiptError(
                    f"凭据 {listed} 不存在或不属于本表，不能登记。"
                )
            conflict = _receipt_conflict(vouchers, wanted)
            if conflict:
                raise ReceiptError(conflict)

        current = conn.execute(
            "SELECT * FROM receipts WHERE dataset_id = ? AND library = ?",
            (dataset_id, library),
        ).fetchone()

        now = now_iso()
        if current is None:
            cur = conn.execute(
                "INSERT INTO receipts(dataset_id, library, batch_no, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (dataset_id, library, batch_no, now, now),
            )
            receipt_id = cur.lastrowid
            for vid in wanted:
                conn.execute(
                    "INSERT INTO receipt_vouchers(receipt_id, voucher_id) "
                    "VALUES (?, ?)",
                    (receipt_id, vid),
                )
            conn.commit()
            return {"status": "created", "receipt_id": receipt_id,
                    "library": library, "batch_no": batch_no,
                    "voucher_ids": wanted}

        current_ids = _load_receipt_voucher_ids(conn, current["id"])
        if batch_no < current["batch_no"]:
            raise ReceiptError(
                f"迟到的旧批次 #{batch_no} 不能替换当前回执"
                f"（当前批次 #{current['batch_no']}）。"
            )
        if batch_no == current["batch_no"]:
            if current_ids == wanted:
                conn.commit()
                return {"status": "reused", "receipt_id": current["id"],
                        "library": library, "batch_no": batch_no,
                        "voucher_ids": wanted}
            raise ReceiptError(
                f"批次 #{batch_no} 已登记过且内容不同，不能替换当前回执；"
                "请使用更大的批次号重新登记。"
            )

        # 更新的批次：替换当前回执
        conn.execute(
            "UPDATE receipts SET batch_no = ?, updated_at = ? WHERE id = ?",
            (batch_no, now, current["id"]),
        )
        conn.execute(
            "DELETE FROM receipt_vouchers WHERE receipt_id = ?", (current["id"],)
        )
        for vid in wanted:
            conn.execute(
                "INSERT INTO receipt_vouchers(receipt_id, voucher_id) VALUES (?, ?)",
                (current["id"], vid),
            )
        conn.commit()
        return {"status": "replaced", "receipt_id": current["id"],
                "library": library, "batch_no": batch_no, "voucher_ids": wanted}
    except Exception:
        conn.rollback()
        raise


def _credential_has_current_totals(conn, voucher_id: int,
                                   current_margins: dict[int, int | None]) -> bool:
    """凭据引用的每条合计是否都仍等于当前值。"""
    citations = conn.execute(
        "SELECT m.entry, vm.value_at_cite FROM voucher_margins vm "
        "JOIN margins m ON m.id = vm.margin_id WHERE vm.voucher_id = ?",
        (voucher_id,),
    ).fetchall()
    return all(current_margins.get(c["entry"]) == c["value_at_cite"]
               for c in citations)


def build_recheck_list(conn, dataset_id: int) -> dict:
    """生成回查清单。

    在同一只读事务内读取回执、凭据状态与现行合计，保证“回执和现行依据”
    来自同一个已提交状态。无需回查的判据：同表、同格、同值仍存在一份
    有效凭据，且该凭据的引用合计与现值全部相符。
    """
    conn.execute("BEGIN")
    try:
        dataset = get_dataset(conn, dataset_id)
        if dataset is None:
            conn.rollback()
            raise ReceiptError("数据集不存在。")

        current_margins = {
            r["entry"]: r["value"]
            for r in conn.execute(
                "SELECT entry, value FROM margins WHERE dataset_id = ?",
                (dataset_id,),
            )
        }
        vouchers = conn.execute(
            "SELECT id, target_row, target_col, proposed_value, status "
            "FROM vouchers WHERE dataset_id = ? ORDER BY id",
            (dataset_id,),
        ).fetchall()
        cred_by_id = {v["id"]: v for v in vouchers}

        # 同表同格同值的“存活依据”集合
        surviving: set[tuple[int, int, int]] = set()
        for v in vouchers:
            if v["status"] != "valid":
                continue
            if _credential_has_current_totals(conn, v["id"], current_margins):
                surviving.add(
                    (v["target_row"], v["target_col"], v["proposed_value"])
                )

        libraries: list[dict] = []
        for receipt in conn.execute(
            "SELECT * FROM receipts WHERE dataset_id = ? ORDER BY library",
            (dataset_id,),
        ):
            voucher_ids = _load_receipt_voucher_ids(conn, receipt["id"])
            cells: dict[tuple[int, int], dict] = {}
            for vid in voucher_ids:
                v = cred_by_id.get(vid)
                if v is None:
                    continue
                key = (v["target_row"], v["target_col"])
                slot = cells.setdefault(key, {
                    "row": v["target_row"], "col": v["target_col"],
                    "value": v["proposed_value"], "voucher_ids": [],
                    "credentials": [],
                })
                slot["voucher_ids"].append(vid)
                slot["credentials"].append({
                    "id": vid,
                    "status": v["status"],
                })

            flagged_cells = []
            for key in sorted(cells):
                slot = cells[key]
                has_basis = (slot["row"], slot["col"], slot["value"]) in surviving
                if not has_basis:
                    slot["voucher_ids"].sort()
                    slot["credentials"].sort(key=lambda c: c["id"])
                    flagged_cells.append(slot)
            libraries.append({
                "receipt_id": receipt["id"],
                "library": receipt["library"],
                "batch_no": receipt["batch_no"],
                "adopted_cells": len(cells),
                "cells": flagged_cells,
                "needs_recheck": bool(flagged_cells),
            })

        conn.commit()
        return {"dataset": dict(dataset), "libraries": libraries}
    except Exception:
        conn.rollback()
        raise

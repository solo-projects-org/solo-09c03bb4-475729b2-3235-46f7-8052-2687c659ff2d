"""馆方回执与回查清单的单元测试与 Web 全流程测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from errata_app import db as dbmod
from errata_app.app import create_app

TRUE_GRID = [
    [1200, 340, 260],
    [560, 780, 410],
    [190, 620, 330],
]
OCR_GRID = [row[:] for row in TRUE_GRID]
OCR_GRID[0][0] = 1300
TRUE_MARGINS = [sum(row) for row in TRUE_GRID] + [
    sum(TRUE_GRID[i][j] for i in range(3)) for j in range(3)
]


class ReceiptTestBase(unittest.TestCase):
    """在临时库里准备：OCR 表、核准合计、凭据 #1（引用 entry 0）。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.app = create_app(self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def conn(self):
        return dbmod.get_db(self.tmp.name)

    def make_proven_dataset(self):
        """上传 OCR 表、录真合计、分析生成凭据 #1。返回 (dataset_id, voucher_id)。"""
        text = "\n".join(",".join(str(v) for v in row) for row in OCR_GRID)
        self.client.post(
            "/datasets/new", data={"name": "回执测试表", "csv_text": text},
            follow_redirects=True)
        data = {f"margin_{k}": str(v) for k, v in enumerate(TRUE_MARGINS)}
        self.client.post("/datasets/1/margins", data=data, follow_redirects=True)
        self.client.post(
            "/datasets/1/analyze",
            data={"target_row": "1", "target_col": "1", "proposed": "1200"},
            follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = [v["id"] for v in dbmod.list_vouchers(conn, 1)]
            finally:
                conn.close()
        self.assertEqual(len(vids), 1)
        return 1, vids[0]


class TestRegisterReceipt(ReceiptTestBase):
    def test_register_creates_receipt(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                out = dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                self.assertEqual(out["status"], "created")
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 1)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_credential_must_belong_to_table(self):
        ds, vid = self.make_proven_dataset()
        # 另建一张表并生成凭据
        text = "\n".join(",".join(str(v) for v in row) for row in OCR_GRID)
        self.client.post("/datasets/new",
                         data={"name": "第二张表", "csv_text": text},
                         follow_redirects=True)
        data = {f"margin_{k}": str(v) for k, v in enumerate(TRUE_MARGINS)}
        self.client.post("/datasets/2/margins", data=data, follow_redirects=True)
        self.client.post("/datasets/2/analyze",
                         data={"target_row": "1", "target_col": "1",
                               "proposed": "1200"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                other = [v["id"] for v in dbmod.list_vouchers(conn, 2)][0]
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 2, [other])
                # 原回执保持不变
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 1)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_invalid_credential_may_be_registered(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.update_margins(conn, ds, {0: TRUE_MARGINS[0] + 5})
                self.assertEqual(dbmod.get_voucher(conn, vid)["status"], "invalid")
                out = dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                self.assertEqual(out["status"], "created")
                self.assertEqual(dbmod.get_receipt(conn, ds, "甲馆")["voucher_ids"],
                                 [vid])
            finally:
                conn.close()

    def test_one_value_per_cell(self):
        ds, vid = self.make_proven_dataset()
        # 同一格、不同拟改值的第二张凭据：先把合计改成另一套真值再分析
        other_margins = [
            sum([1310, 340, 260]), sum([560, 780, 410]), sum([190, 620, 330]),
            sum([1310, 560, 190]), sum([340, 780, 620]), sum([260, 410, 330]),
        ]
        data = {f"margin_{k}": str(v) for k, v in enumerate(other_margins)}
        self.client.post(f"/datasets/{ds}/margins", data=data,
                         follow_redirects=True)
        self.client.post(f"/datasets/{ds}/analyze",
                         data={"target_row": "1", "target_col": "1",
                               "proposed": "1310"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = sorted(v["id"] for v in dbmod.list_vouchers(conn, ds))
                self.assertEqual(len(vids), 2)
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 1, vids)
                self.assertIsNone(dbmod.get_receipt(conn, ds, "甲馆"))
            finally:
                conn.close()

    def test_empty_list_means_withdrawn(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                out = dbmod.register_receipt(conn, ds, "甲馆", 2, [])
                self.assertEqual(out["status"], "replaced")
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["voucher_ids"], [])
                recheck = dbmod.build_recheck_list(conn, ds)
                lib = [x for x in recheck["libraries"] if x["library"] == "甲馆"][0]
                self.assertFalse(lib["needs_recheck"])
                self.assertEqual(lib["cells"], [])
            finally:
                conn.close()

    def test_late_old_batch_rejected(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 5, [vid])
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 4, [])
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 5)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_same_batch_same_content_reused(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                first = dbmod.register_receipt(conn, ds, "甲馆", 3, [vid])
                before = dbmod.get_receipt(conn, ds, "甲馆")["updated_at"]
                again = dbmod.register_receipt(conn, ds, "甲馆", 3, [vid])
                self.assertEqual(again["status"], "reused")
                self.assertEqual(again["receipt_id"], first["receipt_id"])
                after = dbmod.get_receipt(conn, ds, "甲馆")["updated_at"]
                self.assertEqual(before, after)
            finally:
                conn.close()

    def test_same_batch_different_content_rejected(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 3, [vid])
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 3, [])
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 3)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_higher_batch_replaces(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [])
                out = dbmod.register_receipt(conn, ds, "甲馆", 2, [vid])
                self.assertEqual(out["status"], "replaced")
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 2)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_libraries_update_independently(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                dbmod.register_receipt(conn, ds, "乙馆", 9, [vid])
                # 甲馆的旧批次 2 合法（甲馆当前 1），乙馆的批次 2 是迟到
                self.assertEqual(
                    dbmod.register_receipt(conn, ds, "甲馆", 2, [])["status"],
                    "replaced")
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "乙馆", 2, [])
                self.assertEqual(
                    dbmod.get_receipt(conn, ds, "乙馆")["batch_no"], 9)
            finally:
                conn.close()


class TestRecheckList(ReceiptTestBase):
    def test_last_basis_invalid_flags_library(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                # 修改凭据 #1 引用的合计 → 凭据失效
                cited = dbmod.get_voucher_citations(conn, vid)[0]["entry"]
                dbmod.update_margins(conn, ds, {cited: TRUE_MARGINS[cited] + 5})
                recheck = dbmod.build_recheck_list(conn, ds)
                lib = recheck["libraries"][0]
                self.assertTrue(lib["needs_recheck"])
                self.assertEqual(len(lib["cells"]), 1)
                item = lib["cells"][0]
                self.assertEqual((item["row"], item["col"]), (0, 0))
                self.assertEqual(item["value"], 1200)
                self.assertEqual(item["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_surviving_same_value_credential_avoids_recheck(self):
        """同表同格同值仍有有效凭据（且引用合计与现值相符）时无需回查。

        登记的是已失效的 #1；但另一张同格同值、未被回执所报的 #2 仍有效，
        因此采用值仍有依据，无需回查。
        """
        ds, vid1 = self.make_proven_dataset()
        # 清空 entry 0 后分析，生成引用 1..5 的凭据 #2（同格同值）
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.update_margins(conn, ds, {0: None})
            finally:
                conn.close()
        self.client.post("/datasets/1/analyze",
                         data={"target_row": "1", "target_col": "1",
                               "proposed": "1200"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = sorted(v["id"] for v in dbmod.list_vouchers(conn, ds))
                self.assertEqual(len(vids), 2)
                vid2 = max(vids)
                # 恢复 entry 0；#2 未引用 entry 0，保持有效
                dbmod.update_margins(conn, ds, {0: TRUE_MARGINS[0]})
                self.assertEqual(dbmod.get_voucher(conn, vid1)["status"], "invalid")
                self.assertEqual(dbmod.get_voucher(conn, vid2)["status"], "valid")
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid1])
                recheck = dbmod.build_recheck_list(conn, ds)
                lib = recheck["libraries"][0]
                self.assertFalse(lib["needs_recheck"])
                self.assertEqual(lib["cells"], [])
            finally:
                conn.close()

    def test_invalid_same_value_credential_does_not_avoid_recheck(self):
        """同格同值但凭据本身已失效时，不能作为免回查依据。"""
        ds, vid1 = self.make_proven_dataset()
        # 同格同值的第二张凭据（同样引用 entry 0）
        self.client.post("/datasets/1/analyze",
                         data={"target_row": "1", "target_col": "1",
                               "proposed": "1200"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = [v["id"] for v in dbmod.list_vouchers(conn, ds)]
                self.assertEqual(len(vids), 2)
                dbmod.register_receipt(conn, ds, "甲馆", 1, vids)
                cited = dbmod.get_voucher_citations(conn, vid1)[0]["entry"]
                dbmod.update_margins(conn, ds, {cited: TRUE_MARGINS[cited] + 5})
                for vid in vids:
                    self.assertEqual(
                        dbmod.get_voucher(conn, vid)["status"], "invalid")
                recheck = dbmod.build_recheck_list(conn, ds)
                lib = recheck["libraries"][0]
                self.assertTrue(lib["needs_recheck"])
                self.assertEqual(lib["cells"][0]["voucher_ids"], sorted(vids))
            finally:
                conn.close()

    def test_margin_save_does_not_scan_receipts(self):
        """保存合计更正时不读取/改写任何回执表。"""
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                trace = []
                conn.set_trace_callback(trace.append)
                dbmod.update_margins(conn, ds, {0: TRUE_MARGINS[0] + 5})
                conn.set_trace_callback(None)
                self.assertFalse(
                    any("receipt" in sql.lower() for sql in trace),
                    "update_margins 不应访问回执表")
            finally:
                conn.close()

    def test_recheck_reads_in_single_transaction(self):
        """回执与现行依据须取自同一个已提交状态（单一读事务）。"""
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                trace = []
                conn.set_trace_callback(trace.append)
                dbmod.build_recheck_list(conn, ds)
                conn.set_trace_callback(None)
                self.assertEqual(trace[0].strip().upper(), "BEGIN")
                self.assertEqual(trace[-1].strip().upper(), "COMMIT")
                body = " ".join(trace).lower()
                self.assertIn("receipts", body)
                self.assertIn("vouchers", body)
                self.assertIn("margins", body)
            finally:
                conn.close()

    def test_multiple_libraries_listed(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "乙馆", 1, [vid])
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid])
                dbmod.register_receipt(conn, ds, "丙馆", 1, [])
                dbmod.update_margins(conn, ds, {0: TRUE_MARGINS[0] + 5})
                recheck = dbmod.build_recheck_list(conn, ds)
                names = [x["library"] for x in recheck["libraries"]]
                self.assertEqual(names, ["丙馆", "乙馆", "甲馆"])
                flagged = [x["library"] for x in recheck["libraries"]
                           if x["needs_recheck"]]
                self.assertEqual(flagged, ["乙馆", "甲馆"])
            finally:
                conn.close()


class TestReceiptWebFlow(ReceiptTestBase):
    def test_dataset_page_registers_and_shows_receipt(self):
        ds, vid = self.make_proven_dataset()
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "1",
                  "voucher_ids": [str(vid)]},
            follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("已登记馆“甲馆”的回执", body)
        self.assertIn("甲馆", body)
        self.assertIn(f"#{vid}", body)

    def test_web_rejects_late_batch_and_keeps_old(self):
        ds, vid = self.make_proven_dataset()
        self.client.post(f"/datasets/{ds}/receipts",
                         data={"library": "甲馆", "batch_no": "5",
                               "voucher_ids": [str(vid)]},
                         follow_redirects=True)
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "4", "voucher_ids": []},
            follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("登记被拒", body)
        self.assertIn("迟到的旧批次", body)
        with self.app.app_context():
            conn = self.conn()
            try:
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], 5)
                self.assertEqual(got["voucher_ids"], [vid])
            finally:
                conn.close()

    def test_web_rejects_two_values_same_cell(self):
        ds, vid = self.make_proven_dataset()
        self.client.post("/datasets/1/analyze",
                         data={"target_row": "1", "target_col": "1",
                               "proposed": "1201"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = [str(v["id"]) for v in dbmod.list_vouchers(conn, ds)]
            finally:
                conn.close()
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "1", "voucher_ids": vids},
            follow_redirects=True)
        self.assertIn("同一格只能报一个采用值", resp.get_data(as_text=True))

    def test_web_recheck_page_after_last_basis_invalidated(self):
        ds, vid = self.make_proven_dataset()
        self.client.post(f"/datasets/{ds}/receipts",
                         data={"library": "甲馆", "batch_no": "1",
                               "voucher_ids": [str(vid)]},
                         follow_redirects=True)
        # 先改一条未被引用合计：清单不应标记
        with self.app.app_context():
            conn = self.conn()
            try:
                cited = {c["entry"] for c in
                         dbmod.get_voucher_citations(conn, vid)}
                uncited = [k for k in range(6) if k not in cited][0]
            finally:
                conn.close()
        data = {f"margin_{k}": str(v) for k, v in enumerate(TRUE_MARGINS)}
        data[f"margin_{uncited}"] = str(TRUE_MARGINS[uncited] + 1)
        self.client.post(f"/datasets/{ds}/margins", data=data,
                         follow_redirects=True)
        body = self.client.get(f"/datasets/{ds}/recheck").get_data(as_text=True)
        self.assertIn("没有馆需要回查", body)
        # 再改凭据引用的合计 → 最后一份依据失效
        with self.app.app_context():
            conn = self.conn()
            try:
                cited_entry = dbmod.get_voucher_citations(conn, vid)[0]["entry"]
            finally:
                conn.close()
        data[f"margin_{cited_entry}"] = str(TRUE_MARGINS[cited_entry] + 5)
        self.client.post(f"/datasets/{ds}/margins", data=data,
                         follow_redirects=True)
        body = self.client.get(f"/datasets/{ds}/recheck").get_data(as_text=True)
        self.assertIn("甲馆", body)
        self.assertIn("第 1 行第 1 列", body)
        self.assertIn("1200", body)
        self.assertIn(f"#{vid}", body)
        self.assertIn("已失效", body)

    def test_web_empty_list_clears_recheck(self):
        ds, vid = self.make_proven_dataset()
        self.client.post(f"/datasets/{ds}/receipts",
                         data={"library": "甲馆", "batch_no": "1",
                               "voucher_ids": [str(vid)]},
                         follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.update_margins(conn, ds, {0: TRUE_MARGINS[0] + 5})
            finally:
                conn.close()
        self.assertIn("甲馆",
                      self.client.get(f"/datasets/{ds}/recheck").get_data(as_text=True))
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "2"},
            follow_redirects=True)
        self.assertIn("已用批次 #2 更新", resp.get_data(as_text=True))
        self.assertIn("没有馆需要回查",
                      self.client.get(f"/datasets/{ds}/recheck").get_data(as_text=True))


class TestFailedRegisterPreservesOld(ReceiptTestBase):
    """任何登记失败都必须保留原回执（值、批次、所报凭据都不变）。"""

    def _assert_old_intact(self, ds, want_batch, want_vids):
        with self.app.app_context():
            conn = self.conn()
            try:
                got = dbmod.get_receipt(conn, ds, "甲馆")
                self.assertEqual(got["batch_no"], want_batch)
                self.assertEqual(got["voucher_ids"], want_vids)
            finally:
                conn.close()

    def test_failures_keep_old_receipt(self):
        ds, vid = self.make_proven_dataset()
        with self.app.app_context():
            conn = self.conn()
            try:
                dbmod.register_receipt(conn, ds, "甲馆", 3, [vid])
                # 1) 不属于本表的凭据
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 9, [999])
                self.assertEqual(dbmod.get_receipt(conn, ds, "甲馆")["batch_no"], 3)
                # 2) 迟到批次
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 2, [])
                self.assertEqual(dbmod.get_receipt(conn, ds, "甲馆")["batch_no"], 3)
                # 3) 同批次不同内容
                with self.assertRaises(dbmod.ReceiptError):
                    dbmod.register_receipt(conn, ds, "甲馆", 3, [])
            finally:
                conn.close()
        self._assert_old_intact(ds, 3, [vid])


class TestMultiCellRecheck(ReceiptTestBase):
    def test_only_unbacked_cells_listed(self):
        """一馆报多格；只列出失去依据的格，有依据的格不出现。"""
        ds, vid1 = self.make_proven_dataset()
        # 造第二张凭据：格 (0,1) 由 340 改 350；把合计改成对应真表后分析。
        TRUE2 = [[1300, 350, 260], [560, 780, 410], [190, 620, 330]]
        m2 = [sum(r) for r in TRUE2] + [
            sum(TRUE2[i][j] for i in range(3)) for j in range(3)]
        data = {f"margin_{k}": str(v) for k, v in enumerate(m2)}
        self.client.post(f"/datasets/{ds}/margins", data=data,
                         follow_redirects=True)
        self.client.post(f"/datasets/{ds}/analyze",
                         data={"target_row": "1", "target_col": "2",
                               "proposed": "350"}, follow_redirects=True)
        with self.app.app_context():
            conn = self.conn()
            try:
                vids = sorted(v["id"] for v in dbmod.list_vouchers(conn, ds))
                self.assertEqual(len(vids), 2)
                vid2 = [v for v in vids if v != vid1][0]
                target = dbmod.get_voucher(conn, vid2)
                self.assertEqual((target["target_row"], target["target_col"]), (0, 1))
                cited1 = [c["entry"] for c in
                          dbmod.get_voucher_citations(conn, vid1)]
                cited2 = [c["entry"] for c in
                          dbmod.get_voucher_citations(conn, vid2)]
                # 找一条只被 #1 引用、不被 #2 引用的合计
                only1 = [e for e in cited1 if e not in cited2]
                self.assertTrue(only1)
                dbmod.register_receipt(conn, ds, "甲馆", 1, [vid1, vid2])
                # 只失效 #1 引用的那条合计 → (0,0) 需回查，(0,1) 仍有 #2 支撑
                dbmod.update_margins(conn, ds, {only1[0]: m2[only1[0]] + 5})
                recheck = dbmod.build_recheck_list(conn, ds)
                lib = recheck["libraries"][0]
                self.assertTrue(lib["needs_recheck"])
                self.assertEqual(len(lib["cells"]), 1)
                cell = lib["cells"][0]
                self.assertEqual((cell["row"], cell["col"]), (0, 0))
                self.assertEqual(cell["voucher_ids"], [vid1])
                self.assertEqual(lib["adopted_cells"], 2)
            finally:
                conn.close()


class TestReceiptValidation(ReceiptTestBase):
    def test_bad_batch_and_library_rejected(self):
        ds, vid = self.make_proven_dataset()
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "  ", "batch_no": "1", "voucher_ids": []},
            follow_redirects=True)
        self.assertIn("馆名不能为空", resp.get_data(as_text=True))
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "-1", "voucher_ids": []},
            follow_redirects=True)
        self.assertIn("必须是非负整数", resp.get_data(as_text=True))
        resp = self.client.post(
            f"/datasets/{ds}/receipts",
            data={"library": "甲馆", "batch_no": "1", "voucher_ids": ["abc"]},
            follow_redirects=True)
        self.assertIn("必须是非负整数", resp.get_data(as_text=True))
        with self.app.app_context():
            conn = self.conn()
            try:
                self.assertIsNone(dbmod.get_receipt(conn, ds, "甲馆"))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

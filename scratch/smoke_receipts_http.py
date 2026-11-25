"""真实 HTTP 全链路：回执登记、迟到批次、最后依据失效、回查清单。"""
import http.cookiejar
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5073"
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(path):
    with opener.open(BASE + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8")


def post(path, fields):
    data = []
    for k, vals in fields.items():
        for v in (vals if isinstance(vals, list) else [vals]):
            data.append((k, str(v)))
    req = urllib.request.Request(
        BASE + path, data=urllib.parse.urlencode(data).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=120) as r:
        return r.status, r.read().decode("utf-8"), r.url


def check(cond, msg):
    print(("ok - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


OCR = "1300,340,260\n560,780,410\n190,620,330\n"
TRUE_MARGINS = [1800, 1750, 1140, 1950, 1740, 1000]

# 建表
_, body, url = post("/datasets/new", {"name": "HTTP回执表", "csv_text": OCR})
check("/datasets/1" in url, "数据集创建")
post("/datasets/1/margins", {f"margin_{k}": str(v)
                            for k, v in enumerate(TRUE_MARGINS)})
_, body, _ = post("/datasets/1/analyze",
                  {"target_row": "1", "target_col": "1", "proposed": "1200"})
check("证明成立" in body, "生成凭据 #1")

# 数据集页含登记表单
_, body = get("/datasets/1")
check("馆方回执登记" in body and "batch_no" in body, "数据集页含回执登记表单")

# 登记甲馆批 1，采用凭据 #1
_, body, _ = post("/datasets/1/receipts",
                  {"library": "甲馆", "batch_no": "1", "voucher_ids": ["1"]})
check("已登记馆“甲馆”的回执" in body, "甲馆回执登记成功")
_, body = get("/datasets/1/recheck")
check("没有馆需要回查" in body, "凭据有效时无需回查")

# 迟到旧批次：批 0 无凭据，应被拒且保留原回执
_, body, _ = post("/datasets/1/receipts",
                  {"library": "甲馆", "batch_no": "0"})
check("登记被拒" in body and "迟到的旧批次" in body, "迟到旧批次被拒")
_, body = get("/datasets/1")
check("#1" in body and "甲馆" in body, "旧回执仍保留")

# 最后一份依据失效：修改凭据引用的行 1 合计 1800 -> 1805
fields = {f"margin_{k}": str(v) for k, v in enumerate(TRUE_MARGINS)}
fields["margin_0"] = "1805"
post("/datasets/1/margins", fields)
_, body = get("/datasets/1/recheck")
check("甲馆" in body and "第 1 行第 1 列" in body and "1200" in body,
      "最后依据失效后清单列出该馆的格与采用值")
check("#1" in body and "已失效" in body, "清单显示所报凭据号与失效现状")

# 数据集页给出回查提示
_, body = get("/datasets/1")
check("需回查" in body, "数据集页提示回查")

# 撤下全部勘误（批 2 空名单）→ 清单清空
_, body, _ = post("/datasets/1/receipts",
                  {"library": "甲馆", "batch_no": "2"})
check("已用批次 #2 更新" in body, "批次 2 空名单更新成功")
_, body = get("/datasets/1/recheck")
check("没有馆需要回查" in body, "撤下全部勘误后无需回查")

print("\n全部 HTTP 回执检查通过。")

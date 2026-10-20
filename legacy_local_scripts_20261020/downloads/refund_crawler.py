# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V1.0
- 读取 shops.json
- 连接各店已登录 Chrome CDP
- 捕获真实 MTop disputelistv2 请求，不自行计算 sign/token
- 自动按当天日期、pageNo 分页抓取退款列表
- 输出 data/<店铺>/refund_detail_YYYYMMDD.csv
       data/<店铺>/refund_summary_YYYYMMDD.csv
       logs/<店铺>/refund_raw_YYYYMMDD.json

已确认接口：
mtop.alibaba.refundface2.disputeservice.qianniu.pc.disputelistv2

当前退款金额口径：
优先使用 disputeBodyVO.refundFee。
同时保留 buyerActualFee / refundFeeOptions，便于后续核对。
"""

import asyncio, csv, json, re, sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("缺少 playwright，请先运行：pip install playwright")
    raise

BASE_DIR = Path(__file__).resolve().parent
SHOPS_FILE = BASE_DIR / "shops.json"
DATA_ROOT = BASE_DIR / "data"
LOG_ROOT = BASE_DIR / "logs"

API_KEY = "mtop.alibaba.refundface2.disputeservice.qianniu.pc.disputelistv2"
REFUND_URL = "https://myseller.taobao.com/home.htm/trade-platform/tp/refund-list"
PAGE_TIMEOUT = 45
MAX_PAGES = 100


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()


def money(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else 0.0


def load_shops():
    with open(SHOPS_FILE, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    out = []
    for x in cfg.get("shops", []):
        if not x.get("enabled", True):
            continue
        name = str(x.get("name", "")).strip()
        port = x.get("port")
        if name and port:
            out.append({"name": name, "port": int(port)})
    return out


def today_ms():
    now = datetime.now()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def decode_post_data(post_data):
    if not post_data:
        return None
    try:
        q = parse_qs(post_data, keep_blank_values=True)
        raw = q.get("data", [None])[0]
        return json.loads(raw) if raw else None
    except Exception:
        return None


def find_lists(obj):
    """递归寻找最像退款单列表的 list。"""
    candidates = []
    def walk(x, path=""):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, list) and v:
                    score = 0
                    sample = v[0]
                    text = json.dumps(sample, ensure_ascii=False)[:6000] if isinstance(sample, (dict, list)) else ""
                    for key in ("bizOrderId", "refundFee", "disputeBodyVO", "bizClaimType"):
                        if key in text:
                            score += 1
                    if score:
                        candidates.append((score, p, v))
                walk(v, p)
        elif isinstance(x, list):
            for i, v in enumerate(x[:3]):
                walk(v, f"{path}[{i}]")
    walk(obj)
    if not candidates:
        return []
    candidates.sort(key=lambda z: (z[0], len(z[2])), reverse=True)
    return candidates[0][2]


def recursive_get(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = recursive_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = recursive_get(v, key)
            if r is not None:
                return r
    return None


def parse_record(rec):
    body = rec.get("disputeBodyVO") if isinstance(rec, dict) else None
    if not isinstance(body, dict):
        body = {}

    order_id = rec.get("bizOrderId") or recursive_get(rec, "bizOrderId") or ""
    refund_fee_raw = body.get("refundFee")
    if refund_fee_raw is None:
        refund_fee_raw = recursive_get(rec, "refundFee")

    buyer_actual_raw = body.get("buyerActualFee")
    if buyer_actual_raw is None:
        buyer_actual_raw = recursive_get(rec, "buyerActualFee")

    options = body.get("refundFeeOptions")
    if options is None:
        options = recursive_get(rec, "refundFeeOptions")

    status = (
        recursive_get(rec, "refundStatus")
        or recursive_get(rec, "statusDesc")
        or recursive_get(rec, "status")
        or ""
    )
    title = (
        recursive_get(rec, "itemTitle")
        or recursive_get(rec, "title")
        or recursive_get(rec, "auctionTitle")
        or ""
    )

    return {
        "订单号": str(order_id),
        "退款金额": money(refund_fee_raw),
        "refundFee原值": "" if refund_fee_raw is None else str(refund_fee_raw),
        "买家实际支付货款": money(buyer_actual_raw),
        "buyerActualFee原值": "" if buyer_actual_raw is None else str(buyer_actual_raw),
        "退款状态": str(status),
        "商品标题": str(title),
        "退款金额选项": json.dumps(options, ensure_ascii=False) if options is not None else "",
        "原始记录": json.dumps(rec, ensure_ascii=False),
    }


async def crawl_shop(pw, shop):
    name, port = shop["name"], shop["port"]
    sname = safe_name(name)
    data_dir = DATA_ROOT / sname
    log_dir = LOG_ROOT / sname
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    day = datetime.now().strftime("%Y%m%d")
    detail_path = data_dir / f"refund_detail_{day}.csv"
    summary_path = data_dir / f"refund_summary_{day}.csv"
    raw_path = log_dir / f"refund_raw_{day}.json"

    print("\n" + "=" * 72)
    print(f"店铺：{name} / CDP {port}")
    print("=" * 72)

    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    if not browser.contexts:
        raise RuntimeError("Chrome 没有可用 context")
    context = browser.contexts[0]
    pages = context.pages
    page = pages[0] if pages else await context.new_page()

    captured = {"url": None, "template": None}
    first_future = asyncio.get_running_loop().create_future()

    async def on_request(req):
        if API_KEY in req.url and req.method.upper() == "POST":
            tpl = decode_post_data(req.post_data)
            if tpl:
                captured["url"] = req.url
                captured["template"] = tpl
                if not first_future.done():
                    first_future.set_result(True)

    page.on("request", on_request)

    print("正在打开退款管理页并捕获真实退款请求...")
    try:
        await page.goto(REFUND_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    # 给页面一次自然请求机会
    try:
        await asyncio.wait_for(first_future, timeout=20)
    except asyncio.TimeoutError:
        print("未自动捕获，尝试刷新页面...")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            await asyncio.wait_for(first_future, timeout=25)
        except asyncio.TimeoutError:
            raise RuntimeError("45秒内未捕获 disputelistv2 请求；请确认退款管理页已登录且无验证码")

    template = captured["template"]
    start_ms, end_ms = today_ms()

    # 找到 params 容器；截图显示 data.params 为 JSON 字符串/对象两种都兼容
    params_raw = template.get("params", {})
    params_is_string = isinstance(params_raw, str)
    if params_is_string:
        try:
            params = json.loads(params_raw)
        except Exception:
            params = {}
    elif isinstance(params_raw, dict):
        params = deepcopy(params_raw)
    else:
        params = {}

    print(f"已捕获真实接口：{API_KEY}")
    print(f"抓取日期：{datetime.now():%Y-%m-%d}")

    all_raw = []
    all_rows = []
    seen = set()

    # 在页面上下文 fetch 同源 MTop；保留浏览器 cookie，URL 中动态 sign/token 使用刚捕获真实请求。
    for page_no in range(1, MAX_PAGES + 1):
        p = deepcopy(params)
        p["pageNo"] = page_no
        p["payDateTimePickerStartTime"] = start_ms
        p["payDateTimePickerEndTime"] = end_ms

        payload = deepcopy(template)
        payload["params"] = json.dumps(p, ensure_ascii=False, separators=(",", ":")) if params_is_string else p
        body = "data=" + __import__("urllib.parse", fromlist=["quote_plus"]).quote_plus(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

        js = """async ({url, body}) => {
          const r = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {'content-type':'application/x-www-form-urlencoded;charset=UTF-8'},
            body
          });
          return await r.text();
        }"""

        print(f"抓取第 {page_no} 页...", end=" ", flush=True)
        try:
            text = await page.evaluate(js, {"url": captured["url"], "body": body})
            obj = json.loads(text)
        except Exception as e:
            print(f"失败：{e}")
            raise RuntimeError(f"第{page_no}页退款接口抓取失败：{e}")

        # MTop token/sign 失效时停止，避免写假数据
        ret = obj.get("ret", []) if isinstance(obj, dict) else []
        ret_text = " ".join(map(str, ret))
        if ret and "SUCCESS" not in ret_text.upper():
            print(f"MTop返回异常：{ret_text}")
            raise RuntimeError(f"MTop退款接口返回异常：{ret_text}")

        records = find_lists(obj)
        if not records:
            print("0 条，结束")
            break

        new_count = 0
        for rec in records:
            row = parse_record(rec)
            key = (row["订单号"], row["refundFee原值"], row["原始记录"])
            if key in seen:
                continue
            seen.add(key)
            new_count += 1
            all_rows.append(row)
            all_raw.append(rec)

        print(f"{len(records)} 条，新增 {new_count} 条")
        if new_count == 0:
            break

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(all_raw, f, ensure_ascii=False, indent=2)

    fields = ["店铺","订单号","退款金额","refundFee原值","买家实际支付货款",
              "buyerActualFee原值","退款状态","商品标题","退款金额选项","原始记录"]
    with open(detail_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_rows:
            x = dict(r); x["店铺"] = name
            w.writerow(x)

    # 按订单号汇总。若同订单出现多条退款记录，先累加；后续可根据真实业务状态进一步去重。
    sums = {}
    for r in all_rows:
        oid = r["订单号"]
        if not oid:
            continue
        sums[oid] = sums.get(oid, 0.0) + r["退款金额"]

    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["店铺","订单号","退款金额"])
        w.writeheader()
        for oid, amt in sums.items():
            w.writerow({"店铺": name, "订单号": oid, "退款金额": round(amt, 2)})

    print(f"退款记录：{len(all_rows)} 条")
    print(f"退款金额合计：¥{sum(r['退款金额'] for r in all_rows):.2f}")
    print(f"明细：{detail_path}")
    print(f"汇总：{summary_path}")
    return {"shop": name, "rows": len(all_rows), "amount": sum(r["退款金额"] for r in all_rows)}


async def main():
    print("=" * 72)
    print("千牛三店退款抓取器 V1.0")
    print("真实浏览器请求 + 当天退款 + 自动分页")
    print("=" * 72)

    shops = load_shops()
    if not shops:
        raise RuntimeError("shops.json 没有启用店铺")

    results, errors = [], []
    async with async_playwright() as pw:
        for shop in shops:
            try:
                results.append(await crawl_shop(pw, shop))
            except Exception as e:
                errors.append((shop["name"], str(e)))
                print(f"× [{shop['name']}] 退款抓取失败：{e}")

    print("\n" + "=" * 72)
    print("退款抓取结束")
    print("=" * 72)
    print(f"成功：{len(results)} 家 / 失败：{len(errors)} 家")
    print(f"退款记录合计：{sum(x['rows'] for x in results)}")
    print(f"退款金额合计：¥{sum(x['amount'] for x in results):.2f}")
    if errors:
        print("失败店铺：")
        for n, e in errors:
            print(f" - {n}: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n程序异常：{type(e).__name__}: {e}")
    input("\n按 Enter 退出...")

# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V1.3
核心改法：
- 不再重放/修改 MTop disputelistv2 请求（会导致 FAIL_SYS_ILLEGAL_ACCESS）
- 完全让千牛页面自己发真实请求
- 监听真实 Response
- 第一页：刷新当前已打开的退款管理页
- 后续页：自动点击页码 / 下一页
- 不碰 sign / token / data，因此保持淘宝真实请求合法

使用前：
1. 三个端口的 Chrome 都登录好
2. 每家都打开“退款管理”并确保列表可见
3. 在页面里把日期筛选到“今天”
4. 保持退款页打开，再运行本脚本
"""

import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
SHOPS_FILE = BASE_DIR / "shops.json"
DATA_ROOT = BASE_DIR / "data"
LOG_ROOT = BASE_DIR / "logs"

API_KEY = "mtop.alibaba.refundface2.disputeservice.qianniu.pc.disputelistv2"
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

    shops = []
    for x in cfg.get("shops", []):
        if not x.get("enabled", True):
            continue

        name = str(x.get("name", "")).strip()
        port = x.get("port")

        if name and port:
            shops.append({
                "name": name,
                "port": int(port),
            })

    return shops


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


def find_refund_records(obj):
    """
    优先寻找已确认的：
    disputeGridInfoVO.disputeGridVOList
    找不到时再递归寻找包含 bizOrderId/refundFee 的列表。
    """
    if isinstance(obj, dict):
        grid = obj.get("disputeGridInfoVO")

        if isinstance(grid, dict):
            rows = grid.get("disputeGridVOList")

            if isinstance(rows, list):
                return rows

    candidates = []

    def walk(x, path=""):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}" if path else str(k)

                if isinstance(v, list) and v:
                    sample = v[0]

                    if isinstance(sample, dict):
                        text = json.dumps(
                            sample,
                            ensure_ascii=False
                        )[:8000]

                        score = 0

                        for key in [
                            "bizOrderId",
                            "refundFee",
                            "disputeBodyVO",
                            "bizClaimType",
                        ]:
                            if key in text:
                                score += 1

                        if score:
                            candidates.append(
                                (score, len(v), p, v)
                            )

                walk(v, p)

        elif isinstance(x, list):
            for i, v in enumerate(x[:5]):
                walk(v, f"{path}[{i}]")

    walk(obj)

    if not candidates:
        return []

    candidates.sort(
        key=lambda z: (z[0], z[1]),
        reverse=True
    )

    return candidates[0][3]


def parse_record(rec):
    body = (
        rec.get("disputeBodyVO")
        if isinstance(rec, dict)
        else None
    )

    if not isinstance(body, dict):
        body = {}

    order_id = (
        rec.get("bizOrderId")
        if isinstance(rec, dict)
        else None
    )

    if not order_id:
        order_id = recursive_get(
            rec,
            "bizOrderId"
        ) or ""

    refund_fee_raw = body.get(
        "refundFee"
    )

    if refund_fee_raw is None:
        refund_fee_raw = recursive_get(
            rec,
            "refundFee"
        )

    buyer_actual_raw = body.get(
        "buyerActualFee"
    )

    if buyer_actual_raw is None:
        buyer_actual_raw = recursive_get(
            rec,
            "buyerActualFee"
        )

    options = body.get(
        "refundFeeOptions"
    )

    if options is None:
        options = recursive_get(
            rec,
            "refundFeeOptions"
        )

    refund_status = (
        recursive_get(
            rec,
            "refundStatus"
        )
        or
        recursive_get(
            rec,
            "statusDesc"
        )
        or
        ""
    )

    claim_type = (
        rec.get("bizClaimType")
        if isinstance(rec, dict)
        else ""
    )

    return {
        "订单号":
            str(order_id),

        "退款金额":
            money(refund_fee_raw),

        "refundFee原值":
            "" if refund_fee_raw is None
            else str(refund_fee_raw),

        "买家实际支付货款":
            money(buyer_actual_raw),

        "buyerActualFee原值":
            "" if buyer_actual_raw is None
            else str(buyer_actual_raw),

        "退款状态":
            str(refund_status),

        "退款类型":
            str(claim_type or ""),

        "退款金额选项":
            json.dumps(
                options,
                ensure_ascii=False
            )
            if options is not None
            else "",

        "原始记录":
            json.dumps(
                rec,
                ensure_ascii=False
            ),
    }


def extract_page_no_from_request(req):
    """
    仅用于日志/校验。
    不修改请求。
    """
    try:
        post = req.post_data or ""

        m = re.search(
            r'pageNo(?:%22|["\\:,\s])+(\d+)',
            post
        )

        if m:
            return int(m.group(1))

    except Exception:
        pass

    return None


async def find_refund_page(context):
    """
    必须复用用户已打开的真实退款管理页。
    """
    candidates = []

    for p in context.pages:
        u = (p.url or "").lower()

        if (
            "refund-list" in u
            or
            "/refund" in u
            or
            "dispute" in u
        ):
            candidates.append(p)

    if candidates:
        return candidates[-1]

    return None


class RefundPager:
    def __init__(self, page):
        self.page = page
        self.future = None
        self.target_page = 1

    async def on_response(self, response):
        try:
            if (
                self.future is None
                or
                self.future.done()
            ):
                return

            if API_KEY not in response.url:
                return

            text = await response.text()

            try:
                obj = json.loads(text)
            except Exception:
                return

            ret = obj.get(
                "ret",
                []
            ) if isinstance(obj, dict) else []

            ret_text = " ".join(
                map(str, ret)
            )

            if (
                ret
                and
                "SUCCESS"
                not in ret_text.upper()
            ):
                # 浏览器自己的真实请求如果异常，直接报出来
                self.future.set_exception(
                    RuntimeError(
                        f"MTop真实请求异常：{ret_text}"
                    )
                )
                return

            rows = find_refund_records(
                obj
            )

            self.future.set_result(
                {
                    "json": obj,
                    "rows": rows,
                    "request_page":
                        extract_page_no_from_request(
                            response.request
                        ),
                }
            )

        except Exception as e:
            if (
                self.future is not None
                and
                not self.future.done()
            ):
                self.future.set_exception(
                    e
                )

    async def start(self):
        self.page.on(
            "response",
            self.on_response
        )

    async def stop(self):
        try:
            self.page.remove_listener(
                "response",
                self.on_response
            )
        except Exception:
            pass

    async def wait_one(self, timeout=35):
        return await asyncio.wait_for(
            asyncio.shield(
                self.future
            ),
            timeout=timeout
        )

    async def first_page(self):
        self.target_page = 1
        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        print(
            "抓取第 1 页...",
            end=" ",
            flush=True
        )

        # 当前页面已经是用户手动打开的真实退款页，
        # 直接刷新即可让页面重新发合法 MTop 请求。
        try:
            await self.page.reload(
                wait_until="domcontentloaded",
                timeout=60000
            )
        except Exception:
            pass

        try:
            return await self.wait_one(
                35
            )

        except asyncio.TimeoutError:
            raise RuntimeError(
                "刷新退款管理页后未捕获 disputelistv2。"
                "请确认当前退款列表页面能正常显示，并已筛选到今天。"
            )

    async def next_page(self, page_no):
        self.target_page = page_no
        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        clicked = False

        # 先点具体页码
        try:
            loc = self.page.get_by_text(
                str(page_no),
                exact=True
            )

            count = await loc.count()

            for i in range(
                count - 1,
                -1,
                -1
            ):
                item = loc.nth(i)

                try:
                    if await item.is_visible():
                        await item.click(
                            timeout=5000
                        )
                        clicked = True
                        break
                except Exception:
                    continue

        except Exception:
            pass

        # 再点“下一页”
        if not clicked:
            for selector in [
                "a:has-text('下一页')",
                "button:has-text('下一页')",
                "[role='button']:has-text('下一页')",
                "text=下一页",
            ]:
                try:
                    loc = self.page.locator(
                        selector
                    )

                    count = await loc.count()

                    for i in range(
                        count - 1,
                        -1,
                        -1
                    ):
                        item = loc.nth(i)

                        try:
                            if await item.is_visible():
                                await item.click(
                                    timeout=5000
                                )
                                clicked = True
                                break
                        except Exception:
                            continue

                    if clicked:
                        break

                except Exception:
                    pass

        if not clicked:
            return None

        try:
            return await self.wait_one(
                30
            )

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"点击第{page_no}页后未捕获 disputelistv2"
            )


async def crawl_shop(pw, shop):
    name = shop["name"]
    port = shop["port"]

    data_dir = (
        DATA_ROOT
        /
        safe_name(name)
    )

    log_dir = (
        LOG_ROOT
        /
        safe_name(name)
    )

    data_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    log_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    day = datetime.now().strftime(
        "%Y%m%d"
    )

    detail_path = (
        data_dir
        /
        f"refund_detail_{day}.csv"
    )

    summary_path = (
        data_dir
        /
        f"refund_summary_{day}.csv"
    )

    raw_path = (
        log_dir
        /
        f"refund_raw_{day}.json"
    )

    print()
    print("=" * 72)
    print(
        f"店铺：{name} / CDP {port}"
    )
    print("=" * 72)

    browser = (
        await pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}"
        )
    )

    if not browser.contexts:
        raise RuntimeError(
            "Chrome 没有可用 context"
        )

    context = browser.contexts[0]

    page = await find_refund_page(
        context
    )

    if page is None:
        raise RuntimeError(
            "没有找到已打开的退款管理页面。"
            "请先在该店 Chrome 手动打开【退款管理】并保持页面不要关闭。"
        )

    print(
        f"退款页：{page.url}"
    )

    pager = RefundPager(
        page
    )

    await pager.start()

    raw_pages = []
    all_rows = []
    seen = set()

    try:
        result = await pager.first_page()

        rows = result[
            "rows"
        ]

        raw_pages.append(
            result[
                "json"
            ]
        )

        new_count = 0

        for rec in rows:
            row = parse_record(
                rec
            )

            key = (
                row[
                    "订单号"
                ],
                row[
                    "refundFee原值"
                ],
                row[
                    "原始记录"
                ],
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            all_rows.append(
                row
            )

            new_count += 1

        print(
            f"{len(rows)} 条，新增 {new_count} 条"
        )

        # 后续分页
        for page_no in range(
            2,
            MAX_PAGES + 1
        ):
            print(
                f"抓取第 {page_no} 页...",
                end=" ",
                flush=True
            )

            result = await pager.next_page(
                page_no
            )

            if result is None:
                print(
                    "找不到下一页，结束"
                )
                break

            rows = result[
                "rows"
            ]

            raw_pages.append(
                result[
                    "json"
                ]
            )

            if not rows:
                print(
                    "0 条，结束"
                )
                break

            new_count = 0

            for rec in rows:
                row = parse_record(
                    rec
                )

                key = (
                    row[
                        "订单号"
                    ],
                    row[
                        "refundFee原值"
                    ],
                    row[
                        "原始记录"
                    ],
                )

                if key in seen:
                    continue

                seen.add(
                    key
                )

                all_rows.append(
                    row
                )

                new_count += 1

            print(
                f"{len(rows)} 条，新增 {new_count} 条"
            )

            if new_count == 0:
                print(
                    "本页无新增退款记录，结束"
                )
                break

    finally:
        await pager.stop()

    with open(
        raw_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            raw_pages,
            f,
            ensure_ascii=False,
            indent=2
        )

    detail_fields = [
        "店铺",
        "订单号",
        "退款金额",
        "refundFee原值",
        "买家实际支付货款",
        "buyerActualFee原值",
        "退款状态",
        "退款类型",
        "退款金额选项",
        "原始记录",
    ]

    with open(
        detail_path,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=detail_fields
        )

        writer.writeheader()

        for row in all_rows:
            out = dict(
                row
            )

            out[
                "店铺"
            ] = name

            writer.writerow(
                out
            )

    # 先按订单号汇总。
    # 第一版保留所有明细，后续正式接盈亏前再核对退款状态口径。
    summary = {}

    for row in all_rows:
        oid = row[
            "订单号"
        ]

        if not oid:
            continue

        summary[
            oid
        ] = (
            summary.get(
                oid,
                0.0
            )
            +
            row[
                "退款金额"
            ]
        )

    with open(
        summary_path,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "店铺",
                "订单号",
                "退款金额",
            ]
        )

        writer.writeheader()

        for oid, amount in summary.items():
            writer.writerow({
                "店铺":
                    name,

                "订单号":
                    oid,

                "退款金额":
                    round(
                        amount,
                        2
                    ),
            })

    total_amount = sum(
        x[
            "退款金额"
        ]
        for x in all_rows
    )

    print(
        f"退款记录：{len(all_rows)} 条"
    )

    print(
        f"退款金额合计：¥{total_amount:.2f}"
    )

    print(
        f"退款明细：{detail_path}"
    )

    print(
        f"订单退款汇总：{summary_path}"
    )

    return {
        "shop":
            name,

        "rows":
            len(
                all_rows
            ),

        "amount":
            total_amount,
    }


async def main():
    print("=" * 72)
    print("千牛三店退款抓取器 V1.3")
    print("浏览器真实Response + 不改MTop签名 + 自动翻页")
    print("=" * 72)
    print("运行前请确保三家退款管理页都已经打开，并筛选日期为今天。")

    shops = load_shops()

    results = []
    errors = []

    async with async_playwright() as pw:
        for shop in shops:
            try:
                result = await crawl_shop(
                    pw,
                    shop
                )

                results.append(
                    result
                )

            except Exception as e:
                errors.append(
                    (
                        shop[
                            "name"
                        ],
                        str(
                            e
                        )
                    )
                )

                print(
                    f"× [{shop['name']}] 退款抓取失败：{e}"
                )

    print()
    print("=" * 72)
    print("退款抓取结束")
    print("=" * 72)

    print(
        f"成功：{len(results)} 家 / "
        f"失败：{len(errors)} 家"
    )

    print(
        f"退款记录合计："
        f"{sum(x['rows'] for x in results)}"
    )

    print(
        f"退款金额合计："
        f"¥{sum(x['amount'] for x in results):.2f}"
    )

    if errors:
        print()
        print("失败店铺：")

        for name, err in errors:
            print(
                f" - {name}: {err}"
            )


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )

    except Exception as e:
        print(
            f"\n程序异常：{type(e).__name__}: {e}"
        )

    input(
        "\n按 Enter 退出..."
    )

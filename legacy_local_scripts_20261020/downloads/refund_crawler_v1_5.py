# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V1.5
功能：
- 自动连接 shops.json 中启用的三家店
- 自动进入/复用退款管理页
- 自动把日期调整为“今天”
- 自动把订单状态/退款状态相关筛选切到“全部”
- 自动点击查询
- 监听真实 disputelistv2 Response（不改 MTop sign/token/data）
- 自动分页
- 输出退款明细与订单退款汇总

说明：
1. 本版不再要求手动选日期。
2. 本版不修改 MTop 请求参数，因此不会触发 FAIL_SYS_ILLEGAL_ACCESS。
3. 页面筛选完全通过浏览器 UI 操作，让千牛自己生成合法请求。
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
REFUND_URL = "https://myseller.taobao.com/home.htm/trade-platform/refund-list"

MAX_PAGES = 100
WAIT_RESPONSE = 35


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()


def money(v):
    if v is None:
        return 0.0

    if isinstance(v, (int, float)):
        return float(v)

    s = str(v).replace(",", "")

    m = re.search(
        r"-?\d+(?:\.\d+)?",
        s
    )

    return float(m.group()) if m else 0.0


def load_shops():
    with open(
        SHOPS_FILE,
        "r",
        encoding="utf-8-sig"
    ) as f:
        cfg = json.load(f)

    shops = []

    for x in cfg.get("shops", []):

        if not x.get(
            "enabled",
            True
        ):
            continue

        name = str(
            x.get(
                "name",
                ""
            )
        ).strip()

        port = x.get(
            "port"
        )

        if name and port:

            shops.append({
                "name":
                    name,

                "port":
                    int(port),
            })

    return shops


def recursive_get(obj, key):
    if isinstance(obj, dict):

        if key in obj:
            return obj[key]

        for v in obj.values():

            r = recursive_get(
                v,
                key
            )

            if r is not None:
                return r

    elif isinstance(obj, list):

        for v in obj:

            r = recursive_get(
                v,
                key
            )

            if r is not None:
                return r

    return None


def find_refund_records(obj):
    """
    优先：
    disputeGridInfoVO.disputeGridVOList
    """

    if isinstance(obj, dict):

        grid = obj.get(
            "disputeGridInfoVO"
        )

        if isinstance(
            grid,
            dict
        ):

            rows = grid.get(
                "disputeGridVOList"
            )

            if isinstance(
                rows,
                list
            ):
                return rows

    candidates = []

    def walk(x):

        if isinstance(x, dict):

            for v in x.values():

                if isinstance(v, list) and v:

                    sample = v[0]

                    if isinstance(
                        sample,
                        dict
                    ):

                        text = json.dumps(
                            sample,
                            ensure_ascii=False
                        )[:8000]

                        score = 0

                        for k in [
                            "bizOrderId",
                            "refundFee",
                            "disputeBodyVO",
                        ]:

                            if k in text:
                                score += 1

                        if score:

                            candidates.append(
                                (
                                    score,
                                    len(v),
                                    v
                                )
                            )

                walk(v)

        elif isinstance(x, list):

            for v in x[:5]:
                walk(v)

    walk(obj)

    if not candidates:
        return []

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1]
        ),
        reverse=True
    )

    return candidates[0][2]


def parse_record(rec):
    body = (
        rec.get(
            "disputeBodyVO"
        )
        if isinstance(
            rec,
            dict
        )
        else None
    )

    if not isinstance(
        body,
        dict
    ):
        body = {}

    order_id = (
        rec.get(
            "bizOrderId"
        )
        if isinstance(
            rec,
            dict
        )
        else ""
    )

    if not order_id:

        order_id = (
            recursive_get(
                rec,
                "bizOrderId"
            )
            or
            ""
        )

    refund_raw = body.get(
        "refundFee"
    )

    if refund_raw is None:

        refund_raw = recursive_get(
            rec,
            "refundFee"
        )

    buyer_raw = body.get(
        "buyerActualFee"
    )

    if buyer_raw is None:

        buyer_raw = recursive_get(
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

    status = (
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
        recursive_get(
            rec,
            "statusText"
        )
        or
        ""
    )

    claim_type = (
        rec.get(
            "bizClaimType"
        )
        if isinstance(
            rec,
            dict
        )
        else ""
    )

    return {
        "订单号":
            str(order_id),

        "退款金额":
            money(
                refund_raw
            ),

        "refundFee原值":
            ""
            if refund_raw is None
            else str(
                refund_raw
            ),

        "买家实际支付货款":
            money(
                buyer_raw
            ),

        "buyerActualFee原值":
            ""
            if buyer_raw is None
            else str(
                buyer_raw
            ),

        "退款状态":
            str(status),

        "退款类型":
            str(
                claim_type
                or
                ""
            ),

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


async def find_refund_page(context):

    for p in context.pages:

        u = (
            p.url
            or
            ""
        ).lower()

        if (
            "refund-list"
            in u
            or
            "/refund"
            in u
            or
            "dispute"
            in u
        ):
            return p

    # 没有现成退款页就新开
    page = (
        await context.new_page()
    )

    try:

        await page.goto(
            REFUND_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except Exception:
        pass

    await page.wait_for_timeout(
        7000
    )

    return page


async def set_today_date(page):
    """
    自动把【申请时间】设置为今天。

    兼容策略：
    1. 找“申请时间”附近的 input，直接设置 YYYY-MM-DD
    2. 找所有可见日期 input
    3. 点击日期控件后选择“今天”
    """

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"自动设置日期：{today}")

    async def force_fill(locator, value):
        try:
            # 普通 fill
            await locator.fill(value)
            await locator.press("Tab")
            return True
        except Exception:
            pass

        try:
            # readonly/date组件：JS 强制写值并触发事件
            handle = await locator.element_handle()

            if handle:
                await page.evaluate(
                    """({el, value}) => {
                        const proto = Object.getPrototypeOf(el);
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) {
                            desc.set.call(el, value);
                        } else {
                            el.value = value;
                        }
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.dispatchEvent(new Event('blur', {bubbles:true}));
                    }""",
                    {
                        "el": handle,
                        "value": value,
                    }
                )
                return True
        except Exception:
            pass

        return False

    # --------------------------------------------------------
    # 方案1：从“申请时间”标签附近找 input
    # --------------------------------------------------------
    try:
        labels = page.get_by_text(
            "申请时间",
            exact=True
        )

        count = await labels.count()

        for i in range(count):
            label = labels.nth(i)

            if not await label.is_visible():
                continue

            container = label

            for _ in range(5):
                container = container.locator("xpath=..")

                try:
                    inputs = container.locator("input")
                    ic = await inputs.count()

                    if ic:
                        filled = 0

                        for j in range(min(ic, 4)):
                            inp = inputs.nth(j)

                            if await inp.is_visible():
                                if await force_fill(inp, today):
                                    filled += 1

                                if filled >= 2:
                                    break

                        if filled:
                            await page.wait_for_timeout(500)
                            return True

                except Exception:
                    pass

    except Exception:
        pass

    # --------------------------------------------------------
    # 方案2：全局日期类 input
    # --------------------------------------------------------
    selectors = [
        "input[placeholder*='日期']",
        "input[placeholder*='开始']",
        "input[placeholder*='结束']",
        "input[placeholder*='时间']",
        "input[class*='date']",
        "input[class*='picker']",
    ]

    filled = 0

    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()

            for i in range(count):
                inp = loc.nth(i)

                try:
                    if await inp.is_visible():
                        if await force_fill(inp, today):
                            filled += 1

                        if filled >= 2:
                            await page.wait_for_timeout(500)
                            return True
                except Exception:
                    continue
        except Exception:
            continue

    # --------------------------------------------------------
    # 方案3：点击申请时间区域，再选“今天”
    # --------------------------------------------------------
    try:
        labels = page.get_by_text(
            "申请时间",
            exact=True
        )

        if await labels.count():
            label = labels.first
            parent = label.locator("xpath=..")

            try:
                await parent.click(timeout=3000)
            except Exception:
                pass

            await page.wait_for_timeout(300)

            today_loc = page.get_by_text(
                "今天",
                exact=True
            )

            tc = await today_loc.count()

            for i in range(tc - 1, -1, -1):
                item = today_loc.nth(i)

                if await item.is_visible():
                    await item.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    return True

    except Exception:
        pass

    print("⚠ 未识别到日期控件，将继续使用页面当前日期。")
    return False


async def set_status_refund_success(page):
    """
    自动把【售后状态】设置为【退款成功】。

    优先基于页面可见文字定位：
    售后状态 -> 当前值（如 全部） -> 下拉项 退款成功

    兼容 /refund-list 与 /tp/refund-list 两种页面。
    """

    print("自动设置售后状态：退款成功")

    # --------------------------------------------------------
    # 方案1：根据“售后状态”标签定位同一行的可点击控件
    # --------------------------------------------------------
    try:
        labels = page.get_by_text(
            "售后状态",
            exact=True
        )

        label_count = await labels.count()

        for i in range(label_count):
            label = labels.nth(i)

            try:
                if not await label.is_visible():
                    continue

                # 向上寻找几层容器，尝试点击包含当前值“全部”的可点击区域
                container = label

                for _ in range(4):
                    container = container.locator("xpath=..")

                    try:
                        current_all = container.get_by_text(
                            "全部",
                            exact=True
                        )

                        count = await current_all.count()

                        for j in range(count):
                            item = current_all.nth(j)

                            if await item.is_visible():
                                await item.click(timeout=4000)
                                await page.wait_for_timeout(400)

                                success = page.get_by_text(
                                    "退款成功",
                                    exact=True
                                )

                                success_count = await success.count()

                                for k in range(success_count - 1, -1, -1):
                                    opt = success.nth(k)

                                    if await opt.is_visible():
                                        await opt.click(timeout=4000)
                                        await page.wait_for_timeout(500)
                                        return True
                    except Exception:
                        pass

            except Exception:
                continue

    except Exception:
        pass

    # --------------------------------------------------------
    # 方案2：直接找页面上像 select/combobox 的“全部”，展开后选退款成功
    # --------------------------------------------------------
    for selector in [
        "[role='combobox']",
        ".next-select",
        "[class*='select']",
        "button",
    ]:
        try:
            loc = page.locator(selector)
            count = await loc.count()

            for i in range(count):
                item = loc.nth(i)

                try:
                    if not await item.is_visible():
                        continue

                    text = (
                        await item.inner_text()
                    ).strip()

                    if text != "全部" and "全部" not in text:
                        continue

                    await item.click(timeout=3000)
                    await page.wait_for_timeout(300)

                    success = page.get_by_text(
                        "退款成功",
                        exact=True
                    )

                    success_count = await success.count()

                    for j in range(success_count - 1, -1, -1):
                        opt = success.nth(j)

                        if await opt.is_visible():
                            await opt.click(timeout=3000)
                            await page.wait_for_timeout(500)
                            return True

                except Exception:
                    continue

        except Exception:
            continue

    # --------------------------------------------------------
    # 方案3：如果“退款成功”已经直接显示在页面并可点击，直接点击
    # --------------------------------------------------------
    try:
        success = page.get_by_text(
            "退款成功",
            exact=True
        )

        count = await success.count()

        for i in range(count - 1, -1, -1):
            item = success.nth(i)

            try:
                if await item.is_visible():
                    await item.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                continue

    except Exception:
        pass

    print("⚠ 未识别到售后状态控件，将继续使用页面当前状态。")
    return False


async def click_query(page):
    """
    自动触发筛选查询。
    兼容：查询 / 搜索 / 组合查询 / 确定。
    """

    # 先尝试真正的 button / role=button
    texts = [
        "查询",
        "搜索",
        "组合查询",
        "确定",
    ]

    for text in texts:
        for selector in [
            f"button:has-text('{text}')",
            f"a:has-text('{text}')",
            f"[role='button']:has-text('{text}')",
        ]:
            try:
                loc = page.locator(selector)
                count = await loc.count()

                for i in range(count):
                    item = loc.nth(i)

                    try:
                        if await item.is_visible():
                            await item.click(timeout=5000)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

    # 再尝试 exact text，限制尺寸，避免点到普通说明文字
    for text in texts:
        try:
            loc = page.get_by_text(
                text,
                exact=True
            )

            count = await loc.count()

            for i in range(count):
                item = loc.nth(i)

                try:
                    if not await item.is_visible():
                        continue

                    box = await item.bounding_box()

                    if (
                        box
                        and
                        box["width"] < 260
                        and
                        box["height"] < 100
                    ):
                        await item.click(timeout=5000)
                        return True

                except Exception:
                    continue

        except Exception:
            continue

    # 最后尝试 Enter
    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


class RefundPager:

    def __init__(
        self,
        page
    ):
        self.page = page
        self.future = None

    async def on_response(
        self,
        response
    ):

        try:

            if (
                self.future is None
                or
                self.future.done()
            ):
                return

            if API_KEY not in response.url:
                return

            text = (
                await response.text()
            )

            try:

                obj = json.loads(
                    text
                )

            except Exception:
                return

            ret = (
                obj.get(
                    "ret",
                    []
                )
                if isinstance(
                    obj,
                    dict
                )
                else []
            )

            ret_text = " ".join(
                map(
                    str,
                    ret
                )
            )

            if (
                ret
                and
                "SUCCESS"
                not in ret_text.upper()
            ):

                self.future.set_exception(
                    RuntimeError(
                        f"MTop真实请求异常：{ret_text}"
                    )
                )

                return

            rows = find_refund_records(
                obj
            )

            self.future.set_result({
                "json":
                    obj,

                "rows":
                    rows,
            })

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

    async def prepare_and_query(self):
        """
        自动日期 + 售后状态退款成功 + 查询
        """

        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        await set_today_date(
            self.page
        )

        await set_status_refund_success(
            self.page
        )

        await self.page.wait_for_timeout(
            500
        )

        clicked = await click_query(
            self.page
        )

        if not clicked:

            # 如果找不到查询按钮，尝试回车
            try:

                await self.page.keyboard.press(
                    "Enter"
                )

            except Exception:
                pass

        try:

            return await asyncio.wait_for(
                asyncio.shield(
                    self.future
                ),
                timeout=WAIT_RESPONSE
            )

        except asyncio.TimeoutError:

            # 再刷新一次，筛选值通常会保留
            self.future = (
                asyncio.get_running_loop()
                .create_future()
            )

            print(
                "查询未触发接口，尝试刷新退款页..."
            )

            try:

                await self.page.reload(
                    wait_until="domcontentloaded",
                    timeout=60000
                )

            except Exception:
                pass

            try:

                return await asyncio.wait_for(
                    asyncio.shield(
                        self.future
                    ),
                    timeout=WAIT_RESPONSE
                )

            except asyncio.TimeoutError:

                raise RuntimeError(
                    "自动设置今天/退款成功并查询后，"
                    "仍未捕获 disputelistv2"
                )

    async def next_page(
        self,
        page_no
    ):

        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        clicked = False

        # 优先页码
        try:

            loc = self.page.get_by_text(
                str(page_no),
                exact=True
            )

            count = (
                await loc.count()
            )

            for i in range(
                count - 1,
                -1,
                -1
            ):

                item = loc.nth(
                    i
                )

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

        # 下一页
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

                    count = (
                        await loc.count()
                    )

                    for i in range(
                        count - 1,
                        -1,
                        -1
                    ):

                        item = loc.nth(
                            i
                        )

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
                    continue

        if not clicked:

            return None

        try:

            return await asyncio.wait_for(
                asyncio.shield(
                    self.future
                ),
                timeout=30
            )

        except asyncio.TimeoutError:

            # 有些分页点击只改页码但不立即查询，
            # 补一次查询按钮
            self.future = (
                asyncio.get_running_loop()
                .create_future()
            )

            await click_query(
                self.page
            )

            try:

                return await asyncio.wait_for(
                    asyncio.shield(
                        self.future
                    ),
                    timeout=20
                )

            except asyncio.TimeoutError:

                raise RuntimeError(
                    f"第{page_no}页未捕获 disputelistv2"
                )


async def crawl_shop(
    pw,
    shop
):

    name = shop[
        "name"
    ]

    port = shop[
        "port"
    ]

    data_dir = (
        DATA_ROOT
        /
        safe_name(
            name
        )
    )

    log_dir = (
        LOG_ROOT
        /
        safe_name(
            name
        )
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
            "Chrome 无可用 context"
        )

    context = (
        browser.contexts[0]
    )

    page = (
        await find_refund_page(
            context
        )
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

        print(
            "自动设置今天 + 售后状态退款成功 + 查询..."
        )

        result = (
            await pager.prepare_and_query()
        )

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
            f"第1页：{len(rows)} 条，新增 {new_count} 条"
        )

        for page_no in range(
            2,
            MAX_PAGES + 1
        ):

            print(
                f"第{page_no}页...",
                end=" ",
                flush=True
            )

            result = (
                await pager.next_page(
                    page_no
                )
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
                    "无新增退款记录，结束"
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

    total = sum(
        row[
            "退款金额"
        ]
        for row in all_rows
    )

    print(
        f"退款记录：{len(all_rows)} 条"
    )

    print(
        f"退款金额合计：¥{total:.2f}"
    )

    print(
        f"明细：{detail_path}"
    )

    print(
        f"汇总：{summary_path}"
    )

    return {
        "shop":
            name,

        "rows":
            len(
                all_rows
            ),

        "amount":
            total,
    }


async def main():

    print("=" * 72)
    print("千牛三店退款抓取器 V1.5")
    print("自动今天 + 售后状态=退款成功 + 真实MTop请求 + 自动分页")
    print("=" * 72)

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
        print(
            "失败店铺："
        )

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

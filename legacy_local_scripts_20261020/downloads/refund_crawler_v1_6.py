# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V1.6
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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

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


def decode_mtop_params_from_request(request):
    """
    从真实 disputelistv2 POST 中读取 data.params，
    只用于验证页面筛选是否真的生效；绝不修改请求。
    """
    try:
        post_data = request.post_data or ""
        form = parse_qs(post_data, keep_blank_values=True)
        raw_data = form.get("data", [None])[0]

        if not raw_data:
            return {}

        outer = json.loads(raw_data)

        params = outer.get("params", {})

        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                return {}

        return params if isinstance(params, dict) else {}

    except Exception:
        return {}


def today_range_ms():
    now = datetime.now()
    start = datetime(
        now.year,
        now.month,
        now.day
    )
    end = (
        start
        +
        timedelta(days=1)
        -
        timedelta(milliseconds=1)
    )

    return (
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000)
    )


def params_are_today(params):
    start_ms, end_ms = today_range_ms()

    try:
        actual_start = int(
            params.get(
                "payDateTimePickerStartTime",
                0
            )
            or
            0
        )
    except Exception:
        actual_start = 0

    try:
        actual_end = int(
            params.get(
                "payDateTimePickerEndTime",
                0
            )
            or
            0
        )
    except Exception:
        actual_end = 0

    # 千牛有时 end 会是当天23:59:59，也可能是次日00:00附近，
    # 允许少量偏差，但必须落在当天范围附近。
    start_ok = abs(
        actual_start - start_ms
    ) <= 5 * 60 * 1000

    end_ok = (
        abs(
            actual_end - end_ms
        )
        <= 5 * 60 * 1000
        or
        actual_end == 0
    )

    return (
        start_ok
        and
        end_ok
    )


def params_look_refund_success(params):
    """
    不把某个内部枚举值写死。
    只要请求参数中出现“退款成功”对应的非空筛选状态，就视为候选。
    后续 Response 仍会保留退款状态字段供核对。
    """

    status_keys = [
        "refundStatusSelect",
        "refundStatus",
        "csStatusAndDutySelect",
        "statusSelect",
        "disputeStatusSelect",
    ]

    seen_non_empty = []

    for key in status_keys:
        value = params.get(
            key
        )

        if value not in (
            None,
            "",
            "ALL",
            "all",
            []
        ):
            seen_non_empty.append(
                (
                    key,
                    value
                )
            )

    return (
        len(
            seen_non_empty
        )
        >
        0
    ), seen_non_empty


async def dump_filter_debug(page, shop_name):
    """
    筛选失败时输出可见 input/button/select 文本，方便继续适配，
    避免再盲猜页面结构。
    """
    try:
        data = await page.evaluate(
            """() => {
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== 'hidden' &&
                           s.display !== 'none';
                };

                const els = Array.from(
                    document.querySelectorAll(
                        'input,button,[role=button],[role=combobox],[class*=select]'
                    )
                );

                return els
                    .filter(visible)
                    .slice(0, 300)
                    .map((el, i) => ({
                        i,
                        tag: el.tagName,
                        text: (el.innerText || el.textContent || '').trim(),
                        value: el.value || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        cls: el.className || '',
                        role: el.getAttribute('role') || ''
                    }));
            }"""
        )

        debug_dir = (
            LOG_ROOT
            /
            safe_name(
                shop_name
            )
        )

        debug_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        path = (
            debug_dir
            /
            (
                "refund_filter_debug_"
                +
                datetime.now().strftime(
                    "%Y%m%d_%H%M%S"
                )
                +
                ".json"
            )
        )

        path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        return path

    except Exception:
        return None


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
    更严格地设置【申请时间】为今天。
    不再只凭“点击成功”就判定成功。
    真正是否生效，会在查询后通过真实 MTop 请求参数二次校验。
    """

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    print(
        f"自动设置申请时间：{today}"
    )

    # 先找“申请时间”标签后的 input
    try:
        label = page.get_by_text(
            "申请时间",
            exact=True
        ).first

        if await label.count():

            # 从标签祖先容器中找 input
            node = label

            for _ in range(6):
                node = node.locator(
                    "xpath=.."
                )

                inputs = node.locator(
                    "input"
                )

                count = await inputs.count()

                visible_inputs = []

                for i in range(count):
                    inp = inputs.nth(i)

                    try:
                        if await inp.is_visible():
                            visible_inputs.append(
                                inp
                            )
                    except Exception:
                        pass

                if visible_inputs:

                    # 日期区间常见1个range输入或2个input
                    for inp in visible_inputs[:2]:
                        try:
                            await inp.click(
                                timeout=3000
                            )
                        except Exception:
                            pass

                        try:
                            await inp.press(
                                "Control+A"
                            )
                        except Exception:
                            pass

                        try:
                            await inp.fill(
                                today
                            )
                        except Exception:
                            # readonly 组件：移除 readonly 后再填
                            try:
                                handle = await inp.element_handle()

                                if handle:
                                    await page.evaluate(
                                        """el => el.removeAttribute('readonly')""",
                                        handle
                                    )

                                    await inp.fill(
                                        today
                                    )
                            except Exception:
                                pass

                        try:
                            await inp.press(
                                "Tab"
                            )
                        except Exception:
                            pass

                    await page.wait_for_timeout(
                        500
                    )

                    return True

    except Exception:
        pass

    # 全局兜底：找值像日期的可见 input
    try:
        inputs = page.locator(
            "input"
        )

        count = await inputs.count()

        candidates = []

        for i in range(count):
            inp = inputs.nth(i)

            try:
                if not await inp.is_visible():
                    continue

                val = await inp.input_value()
                ph = (
                    await inp.get_attribute(
                        "placeholder"
                    )
                    or
                    ""
                )

                if (
                    re.search(
                        r"\d{4}-\d{2}-\d{2}",
                        val or ""
                    )
                    or
                    "日期" in ph
                    or
                    "时间" in ph
                    or
                    "开始" in ph
                    or
                    "结束" in ph
                ):
                    candidates.append(
                        inp
                    )

            except Exception:
                continue

        for inp in candidates[:2]:
            try:
                await inp.click(
                    timeout=3000
                )
            except Exception:
                pass

            try:
                await inp.press(
                    "Control+A"
                )
            except Exception:
                pass

            try:
                await inp.fill(
                    today
                )
            except Exception:
                try:
                    handle = await inp.element_handle()

                    if handle:
                        await page.evaluate(
                            """el => el.removeAttribute('readonly')""",
                            handle
                        )

                        await inp.fill(
                            today
                        )
                except Exception:
                    pass

            try:
                await inp.press(
                    "Tab"
                )
            except Exception:
                pass

        if candidates:
            await page.wait_for_timeout(
                500
            )
            return True

    except Exception:
        pass

    print(
        "⚠ 没有找到申请时间输入框。"
    )

    return False


async def set_status_refund_success(page):
    """
    自动设置【售后状态】=【退款成功】。
    查询后会通过真实请求参数校验是否真的不是 ALL。
    """

    print(
        "自动设置售后状态：退款成功"
    )

    # 方案1：围绕“售后状态”标签，找同一行中的当前值（通常是“全部”）
    try:
        labels = page.get_by_text(
            "售后状态",
            exact=True
        )

        lc = await labels.count()

        for i in range(lc):
            label = labels.nth(i)

            if not await label.is_visible():
                continue

            node = label

            for _ in range(6):
                node = node.locator(
                    "xpath=.."
                )

                # 优先在这个局部容器里找“全部”
                all_loc = node.get_by_text(
                    "全部",
                    exact=True
                )

                ac = await all_loc.count()

                for j in range(ac):
                    current = all_loc.nth(j)

                    try:
                        if not await current.is_visible():
                            continue

                        await current.click(
                            timeout=4000
                        )

                        await page.wait_for_timeout(
                            400
                        )

                        success_loc = page.get_by_text(
                            "退款成功",
                            exact=True
                        )

                        sc = await success_loc.count()

                        for k in range(
                            sc - 1,
                            -1,
                            -1
                        ):
                            option = success_loc.nth(k)

                            if await option.is_visible():
                                await option.click(
                                    timeout=4000
                                )

                                await page.wait_for_timeout(
                                    500
                                )

                                return True

                    except Exception:
                        continue

    except Exception:
        pass

    # 方案2：全局寻找“退款成功”，如果下拉已打开则直接点
    try:
        success_loc = page.get_by_text(
            "退款成功",
            exact=True
        )

        sc = await success_loc.count()

        for i in range(
            sc - 1,
            -1,
            -1
        ):
            option = success_loc.nth(i)

            try:
                if await option.is_visible():
                    await option.click(
                        timeout=4000
                    )

                    await page.wait_for_timeout(
                        500
                    )

                    return True
            except Exception:
                continue

    except Exception:
        pass

    print(
        "⚠ 没有找到售后状态=退款成功控件。"
    )

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

    async def prepare_and_query(self, shop_name):
        """
        自动：
        - 申请时间=今天
        - 售后状态=退款成功
        - 查询

        并通过真实 disputelistv2 Request 参数确认筛选确实生效。
        """

        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        request_future = (
            asyncio.get_running_loop()
            .create_future()
        )

        async def on_request(req):
            if (
                API_KEY in req.url
                and
                req.method.upper() == "POST"
            ):
                if not request_future.done():
                    request_future.set_result(
                        req
                    )

        self.page.on(
            "request",
            on_request
        )

        try:
            date_ok = await set_today_date(
                self.page
            )

            status_ok = await set_status_refund_success(
                self.page
            )

            await self.page.wait_for_timeout(
                700
            )

            clicked = await click_query(
                self.page
            )

            if not clicked:
                raise RuntimeError(
                    "没有找到查询按钮"
                )

            try:
                request = await asyncio.wait_for(
                    asyncio.shield(
                        request_future
                    ),
                    timeout=20
                )

            except asyncio.TimeoutError:
                debug_path = await dump_filter_debug(
                    self.page,
                    shop_name
                )

                raise RuntimeError(
                    "点击查询后没有触发 disputelistv2。"
                    +
                    (
                        f" 页面控件调试已保存：{debug_path}"
                        if debug_path
                        else ""
                    )
                )

            params = decode_mtop_params_from_request(
                request
            )

            today_ok = params_are_today(
                params
            )

            status_valid, status_values = (
                params_look_refund_success(
                    params
                )
            )

            print(
                "筛选请求校验："
                f"日期={'OK' if today_ok else '失败'}，"
                f"状态={'OK' if status_valid else '失败'}"
            )

            if not today_ok:
                print(
                    "实际日期参数："
                    f"start={params.get('payDateTimePickerStartTime')} "
                    f"end={params.get('payDateTimePickerEndTime')}"
                )

            if not status_valid:
                print(
                    "实际状态参数仍为空/ALL。"
                )
            else:
                print(
                    f"实际状态参数：{status_values}"
                )

            if (
                not today_ok
                or
                not status_valid
            ):
                debug_path = await dump_filter_debug(
                    self.page,
                    shop_name
                )

                raise RuntimeError(
                    "页面虽然执行了自动筛选，但真实请求参数没有变成"
                    "【今天 + 退款成功】；为防止把历史退款误计入盈亏，"
                    "本店已停止抓取。"
                    +
                    (
                        f" 调试文件：{debug_path}"
                        if debug_path
                        else ""
                    )
                )

            try:
                return await asyncio.wait_for(
                    asyncio.shield(
                        self.future
                    ),
                    timeout=WAIT_RESPONSE
                )

            except asyncio.TimeoutError:
                raise RuntimeError(
                    "筛选请求已正确发出，但未捕获退款Response"
                )

        finally:
            try:
                self.page.remove_listener(
                    "request",
                    on_request
                )
            except Exception:
                pass

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
            "自动设置今天 + 售后状态退款成功 + 查询并校验..."
        )

        result = (
            await pager.prepare_and_query(
            name
        )
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
    print("千牛三店退款抓取器 V1.6")
    print("自动今天 + 退款成功 + 请求参数校验 + 自动分页")
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

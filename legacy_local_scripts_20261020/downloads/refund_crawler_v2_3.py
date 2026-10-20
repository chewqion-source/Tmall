# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V2.3
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


def _flatten_value(value):
    """
    把 list / tuple / 单值统一成字符串列表。
    """
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


def discover_date_params(params):
    """
    V1.7 不再写死 payDateTimePickerStartTime。
    自动扫描请求里所有可能的日期/时间参数。
    """
    found = []

    for key, value in params.items():
        lk = str(key).lower()

        if (
            "time" in lk
            or
            "date" in lk
        ):
            vals = _flatten_value(value)

            for val in vals:
                text = str(val).strip()

                if re.fullmatch(r"\d{12,13}", text):
                    try:
                        found.append(
                            (
                                str(key),
                                int(text)
                            )
                        )
                    except Exception:
                        pass

    return found


def params_are_today(params):
    """
    只要真实请求中存在一组日期参数落在今天范围，
    就判定日期筛选成功。

    兼容：
    payDateTimePickerStartTime
    applyDateTimePickerStartTime
    createDateTime...
    以及后续页面可能变化的字段名。
    """

    start_ms, end_ms = today_range_ms()

    date_params = discover_date_params(
        params
    )

    if not date_params:
        return False, []

    # 允许当天起点/终点附近 10 分钟误差
    tolerance = (
        10
        *
        60
        *
        1000
    )

    start_matches = []
    end_matches = []

    for key, value in date_params:

        if abs(
            value
            -
            start_ms
        ) <= tolerance:

            start_matches.append(
                (
                    key,
                    value
                )
            )

        if (
            abs(
                value
                -
                end_ms
            )
            <= tolerance
            or
            abs(
                value
                -
                (
                    end_ms
                    +
                    1
                )
            )
            <= tolerance
        ):

            end_matches.append(
                (
                    key,
                    value
                )
            )

    # 某些页面只传开始时间，此时只要开始时间就是今天，也接受。
    ok = bool(
        start_matches
    )

    return (
        ok,
        date_params
    )


def params_look_refund_success(params):
    """
    V2.2：
    refundStatusSelect 在不同页面里可能是：
      3
      5
      ['3']
      ['-1', '5']
      "['3']"
      "['-1','5']"

    统一从真实值中提取数字。
    已确认：
      3 = 退款成功（旧版）
      5 = 退款成功（新版）
    -1 / -9999 仅为默认/父级哨兵。
    """

    raw = params.get(
        "refundStatusSelect"
    )

    if raw is None:
        return (
            False,
            []
        )

    # 统一变成字符串后提取整数 token
    text = str(raw)

    tokens = re.findall(
        r"-?\d+",
        text
    )

    candidates = [
        (
            "refundStatusSelect",
            token
        )
        for token in tokens
    ]

    meaningful = [
        token
        for token in tokens
        if token not in (
            "-1",
            "-9999",
        )
    ]

    ok = any(
        token in (
            "3",
            "5",
        )
        for token in meaningful
    )

    return (
        ok,
        candidates
    )


def get_page_info(params):
    """
    从真实请求中读取 pageNo/pageSize。
    """
    def first_int(value, default=0):
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else default

        m = re.search(
            r"-?\d+",
            str(value)
        )

        return int(m.group()) if m else default

    return (
        first_int(
            params.get(
                "pageNo",
                1
            ),
            1
        ),
        first_int(
            params.get(
                "pageSize",
                20
            ),
            20
        )
    )


def request_is_target_refund_query(request):
    """
    只接受真正符合：
    - 今天
    - 退款成功
    的 disputelistv2 请求。
    """
    try:
        params = decode_mtop_params_from_request(
            request
        )

        today_ok, _ = params_are_today(
            params
        )

        status_ok, _ = params_look_refund_success(
            params
        )

        page_no, page_size = get_page_info(
            params
        )

        return (
            today_ok
            and
            status_ok,
            params,
            page_no,
            page_size
        )

    except Exception:
        return (
            False,
            {},
            0,
            20
        )


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


async def find_refund_scope(page):
    """
    V1.9：
    /tp/refund-list 在部分店铺中只是外层千牛壳，
    真正退款列表可能渲染在 iframe/frame 内。

    返回真正包含【申请时间 / 售后状态 / 退款成功】的 Page 或 Frame。
    """
    candidates = [page] + list(page.frames)

    best = None
    best_score = -1

    for scope in candidates:
        try:
            score = 0

            for text in [
                "申请时间",
                "售后状态",
                "退款成功",
                "组合查询",
                "批量同意退款",
                "批量审核",
            ]:
                try:
                    loc = scope.get_by_text(
                        text,
                        exact=True
                    )

                    if await loc.count():
                        score += 2
                except Exception:
                    pass

            # URL 有 refund/dispute 也加分
            try:
                u = (scope.url or "").lower()

                if (
                    "refund" in u
                    or
                    "dispute" in u
                ):
                    score += 1
            except Exception:
                pass

            if score > best_score:
                best_score = score
                best = scope

        except Exception:
            continue

    return best or page


async def dump_frame_debug(page, shop_name):
    """
    保存所有 frame URL 和退款关键词命中情况。
    """
    rows = []

    for idx, frame in enumerate(page.frames):
        item = {
            "index": idx,
            "url": frame.url,
            "name": frame.name,
            "hits": {},
        }

        for text in [
            "申请时间",
            "售后状态",
            "退款成功",
            "组合查询",
            "批量同意退款",
            "批量审核",
        ]:
            try:
                item["hits"][text] = await frame.get_by_text(
                    text,
                    exact=True
                ).count()
            except Exception:
                item["hits"][text] = -1

        rows.append(item)

    debug_dir = (
        LOG_ROOT
        /
        safe_name(shop_name)
    )
    debug_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    path = (
        debug_dir
        /
        (
            "refund_frame_debug_"
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
            rows,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    return path


async def ensure_combination_query_open(scope):
    """
    部分 /tp/refund-list 页面默认折叠高级筛选。
    先点击【组合查询】，让申请时间/售后状态控件真正渲染出来。
    """
    try:
        loc = scope.get_by_text(
            "组合查询",
            exact=True
        )

        count = await loc.count()

        for i in range(count):
            item = loc.nth(i)

            try:
                if not await item.is_visible():
                    continue

                await item.click(
                    timeout=4000
                )

                await scope.wait_for_timeout(
                    500
                )

                # 展开成功的判断：至少出现申请时间或售后状态
                for text in [
                    "申请时间",
                    "售后状态",
                ]:
                    try:
                        if await scope.get_by_text(
                            text,
                            exact=True
                        ).count():
                            print("已展开组合查询。")
                            return True
                    except Exception:
                        pass

            except Exception:
                continue

    except Exception:
        pass

    # 如果本来就已经展开，也算成功
    try:
        if (
            await scope.get_by_text(
                "售后状态",
                exact=True
            ).count()
            or
            await scope.get_by_text(
                "申请时间",
                exact=True
            ).count()
        ):
            return True
    except Exception:
        pass

    return False


async def set_today_date(page):
    # V1.9: page 参数也可以是 Frame，Playwright Locator API 相同。
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
    # V2.0: page 参数也可以是 Frame，Playwright Locator API 相同。
    """
    严格设置【售后状态】=【退款成功】。
    成功与否最终由真实 MTop 参数 refundStatusSelect=3 校验。
    """

    print("自动设置售后状态：退款成功")

    await ensure_combination_query_open(
        page
    )

    # --------------------------------------------------------
    # 先锁定“售后状态”这一行，再点击当前下拉值。
    # --------------------------------------------------------
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

            # 向上找一个同时包含“售后状态”和当前值的局部容器
            for _ in range(6):
                node = node.locator("xpath=..")

                # 常见当前值：全部、退款成功、待处理等
                clickable_candidates = node.locator(
                    "[role='combobox'],"
                    "[class*='select'],"
                    "[class*='picker'],"
                    "button,"
                    "input"
                )

                cc = await clickable_candidates.count()

                for j in range(cc):
                    current = clickable_candidates.nth(j)

                    try:
                        if not await current.is_visible():
                            continue

                        # 避免误点太大的整个容器
                        box = await current.bounding_box()

                        if (
                            box
                            and
                            box["width"] > 400
                        ):
                            continue

                        await current.click(
                            timeout=3000
                        )

                        await page.wait_for_timeout(
                            300
                        )

                        success = page.get_by_text(
                            "退款成功",
                            exact=True
                        )

                        sc = await success.count()

                        for k in range(
                            sc - 1,
                            -1,
                            -1
                        ):
                            opt = success.nth(k)

                            if await opt.is_visible():
                                await opt.click(
                                    timeout=4000
                                )

                                await page.wait_for_timeout(
                                    500
                                )

                                print("已选择售后状态：退款成功")
                                return True

                    except Exception:
                        continue

                # 另外一种结构：当前值“全部”本身是 span/div
                try:
                    all_loc = node.get_by_text(
                        "全部",
                        exact=True
                    )

                    ac = await all_loc.count()

                    for j in range(ac):
                        current = all_loc.nth(j)

                        if not await current.is_visible():
                            continue

                        await current.click(
                            timeout=3000
                        )

                        await page.wait_for_timeout(
                            300
                        )

                        success = page.get_by_text(
                            "退款成功",
                            exact=True
                        )

                        sc = await success.count()

                        for k in range(
                            sc - 1,
                            -1,
                            -1
                        ):
                            opt = success.nth(k)

                            if await opt.is_visible():
                                await opt.click(
                                    timeout=4000
                                )
                                await page.wait_for_timeout(
                                    500
                                )
                                print("已选择售后状态：退款成功")
                                return True

                except Exception:
                    pass

    except Exception:
        pass

    print("⚠ 未能点击售后状态=退款成功；稍后真实请求校验会拦截错误口径。")
    return False


async def get_query_candidates(page):
    # V1.9: page 参数也可以是 Frame，Playwright Locator API 相同。
    """
    返回所有可能的“查询/搜索/确定”可点击节点。
    V1.8 不再点到第一个就停，而是逐个尝试，直到真实 disputelistv2 被触发。
    """
    candidates = []
    seen = set()

    selectors = [
        "button:has-text('查询')",
        "button:has-text('搜索')",
        "button:has-text('确定')",
        "a:has-text('查询')",
        "a:has-text('搜索')",
        "[role='button']:has-text('查询')",
        "[role='button']:has-text('搜索')",
        "[role='button']:has-text('确定')",
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = await loc.count()

            for i in range(count):
                item = loc.nth(i)

                try:
                    if not await item.is_visible():
                        continue

                    box = await item.bounding_box()

                    if not box:
                        continue

                    key = (
                        round(box["x"]),
                        round(box["y"]),
                        round(box["width"]),
                        round(box["height"]),
                    )

                    if key in seen:
                        continue

                    seen.add(key)
                    candidates.append(item)

                except Exception:
                    continue

        except Exception:
            continue

    return candidates


async def click_query(page):
    # V1.9: page 参数也可以是 Frame，Playwright Locator API 相同。
    """
    兼容旧调用：点击第一个候选。
    """
    candidates = await get_query_candidates(page)

    for item in candidates:
        try:
            await item.click(timeout=4000)
            return True
        except Exception:
            continue

    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


class RefundPager:

    def __init__(
        self,
        page,
        context,
        scope=None
    ):
        self.page = page
        self.context = context
        self.scope = scope or page
        self.future = None

    async def on_response(
        self,
        response
    ):
        """
        V2.3：
        Context 里可能同时出现多个 disputelistv2。
        只接受“今天 + 退款成功”的真实请求对应 Response。
        """

        try:
            if (
                self.future is None
                or
                self.future.done()
            ):
                return

            if API_KEY not in response.url:
                return

            ok, params, page_no, page_size = (
                request_is_target_refund_query(
                    response.request
                )
            )

            if not ok:
                return

            text = await response.text()

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
                return

            rows = find_refund_records(
                obj
            )

            self.future.set_result({
                "json":
                    obj,

                "rows":
                    rows,

                "params":
                    params,

                "page_no":
                    page_no,

                "page_size":
                    page_size,
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
        # V1.8：监听整个 BrowserContext，而不是只监听当前退款页。
        # 某些千牛版本会由其他标签/内部页面发起 MTop 请求。
        self.context.on(
            "response",
            self.on_response
        )

    async def stop(self):

        try:
            self.context.remove_listener(
                "response",
                self.on_response
            )
        except Exception:
            pass

    async def prepare_and_query(self, shop_name):
        """
        V2.2 核心修复：

        之前 request 监听器在“设置日期/状态之前”就开始监听，
        页面修改日期或展开筛选时可能先发出一次 disputelistv2，
        于是程序拿到的是“中间态/旧状态”的请求参数。

        现在顺序改为：
        1. 先完成日期=今天
        2. 再完成售后状态=退款成功
        3. 等页面稳定
        4. 此时才开始监听 Request/Response
        5. 再点击最终查询按钮
        6. 只校验这一次“最终查询”的真实请求参数
        """

        # ----------------------------------------------------
        # 1. 先设置筛选，不监听请求
        # ----------------------------------------------------
        await ensure_combination_query_open(
            self.scope
        )

        await set_today_date(
            self.scope
        )

        await self.page.wait_for_timeout(
            500
        )

        await set_status_refund_success(
            self.scope
        )

        # 给组件足够时间把内部状态写入表单
        await self.page.wait_for_timeout(
            1200
        )

        # ----------------------------------------------------
        # 2. 筛选设置完毕后，才开始监听最终查询请求
        # ----------------------------------------------------
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

        self.context.on(
            "request",
            on_request
        )

        # Response 也重新创建，避免吃到前面筛选时的旧 response
        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        try:
            # ------------------------------------------------
            # 3. 点击最终查询
            # ------------------------------------------------
            candidates = await get_query_candidates(
                self.scope
            )

            print(
                f"找到查询候选按钮：{len(candidates)} 个"
            )

            request = None

            for idx, item in enumerate(
                candidates,
                start=1
            ):
                try:
                    txt = (
                        await item.inner_text()
                    ).strip()
                except Exception:
                    txt = ""

                print(
                    f"尝试最终查询按钮 {idx}/{len(candidates)}："
                    f"{txt or '(无文字)'}"
                )

                try:
                    await item.click(
                        timeout=4000
                    )
                except Exception:
                    try:
                        handle = await item.element_handle()

                        if handle:
                            await self.page.evaluate(
                                "(el) => el.click()",
                                handle
                            )
                    except Exception:
                        continue

                try:
                    request = await asyncio.wait_for(
                        asyncio.shield(
                            request_future
                        ),
                        timeout=5
                    )
                    break
                except asyncio.TimeoutError:
                    continue

            if request is None:
                print(
                    "查询候选均未触发最终接口，尝试 Enter..."
                )

                try:
                    await self.page.keyboard.press(
                        "Enter"
                    )
                except Exception:
                    pass

                try:
                    request = await asyncio.wait_for(
                        asyncio.shield(
                            request_future
                        ),
                        timeout=6
                    )
                except asyncio.TimeoutError:
                    pass

            if request is None:
                debug_path = await dump_filter_debug(
                    self.scope,
                    shop_name
                )

                frame_debug = await dump_frame_debug(
                    self.page,
                    shop_name
                )

                raise RuntimeError(
                    "设置完【今天 + 退款成功】后，"
                    "没有触发最终 disputelistv2 查询。"
                    f" 控件：{debug_path}"
                    f" Frame：{frame_debug}"
                )

            # ------------------------------------------------
            # 4. 只校验最终查询这一次请求
            # ------------------------------------------------
            params = decode_mtop_params_from_request(
                request
            )

            today_ok, date_values = params_are_today(
                params
            )

            status_valid, status_values = (
                params_look_refund_success(
                    params
                )
            )

            print(
                "最终查询请求校验："
                f"日期={'OK' if today_ok else '失败'}，"
                f"状态={'OK' if status_valid else '失败'}"
            )

            print(
                f"实际日期参数：{date_values}"
            )

            print(
                f"实际状态参数：{status_values}"
            )

            if status_valid:
                print(
                    "退款成功状态校验通过 "
                    "（旧版=3 / 新版=5）"
                )

            if (
                not today_ok
                or
                not status_valid
            ):
                debug_path = await dump_filter_debug(
                    self.scope,
                    shop_name
                )

                param_debug_dir = (
                    LOG_ROOT
                    /
                    safe_name(
                        shop_name
                    )
                )

                param_debug_dir.mkdir(
                    parents=True,
                    exist_ok=True
                )

                param_debug_path = (
                    param_debug_dir
                    /
                    (
                        "refund_request_params_"
                        +
                        datetime.now().strftime(
                            "%Y%m%d_%H%M%S"
                        )
                        +
                        ".json"
                    )
                )

                param_debug_path.write_text(
                    json.dumps(
                        params,
                        ensure_ascii=False,
                        indent=2
                    ),
                    encoding="utf-8"
                )

                raise RuntimeError(
                    "最终查询请求没有同时确认【今天 + 退款成功】；"
                    "本店停止，避免误计历史/非成功退款。"
                    f" 参数：{param_debug_path}"
                    f" 控件：{debug_path}"
                )

            # 最终 Request 已确认正确。
            # 重新创建 Future，丢弃筛选过程中的旧 Response；
            # 从这里开始只接收目标条件的真实返回。
            self.future = (
                asyncio.get_running_loop()
                .create_future()
            )

            # ------------------------------------------------
            # 5. 等待与最终查询对应的 Response
            # ------------------------------------------------
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(
                        self.future
                    ),
                    timeout=WAIT_RESPONSE
                )

                print(
                    f"捕获目标Response：pageNo={result.get('page_no')} "
                    f"pageSize={result.get('page_size')} "
                    f"records={len(result.get('rows', []))}"
                )

                return result

            except asyncio.TimeoutError:
                raise RuntimeError(
                    "最终查询参数正确，但未捕获对应退款Response"
                )

        finally:
            try:
                self.context.remove_listener(
                    "request",
                    on_request
                )
            except Exception:
                pass

    async def next_page(
        self,
        page_no
    ):
        """
        V2.3 安全分页：
        - 只在确定需要下一页时调用
        - 优先点击退款列表自己的 next-pagination-item next-next
        - Response 必须仍满足 今天+退款成功
        - 且真实 pageNo 必须等于目标页
        """

        self.future = (
            asyncio.get_running_loop()
            .create_future()
        )

        clicked = False

        # 新版页面明确有 next-pagination-item next-next
        for selector in [
            "button.next-pagination-item.next-next",
            ".next-pagination-item.next-next",
            "button:has-text('下一页')",
            "a:has-text('下一页')",
        ]:
            try:
                loc = self.scope.locator(
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
                        if not await item.is_visible():
                            continue

                        disabled = (
                            await item.get_attribute(
                                "disabled"
                            )
                        )

                        cls = (
                            await item.get_attribute(
                                "class"
                            )
                            or
                            ""
                        )

                        if (
                            disabled is not None
                            or
                            "disabled" in cls.lower()
                        ):
                            continue

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
            result = await asyncio.wait_for(
                asyncio.shield(
                    self.future
                ),
                timeout=30
            )

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"点击下一页后未捕获目标退款Response"
            )

        actual_page = int(
            result.get(
                "page_no",
                0
            )
            or
            0
        )

        if (
            actual_page
            and
            actual_page != page_no
        ):
            raise RuntimeError(
                f"分页请求页码异常：期望{page_no}，实际{actual_page}"
            )

        return result


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

    scope = await find_refund_scope(
        page
    )

    try:
        scope_url = scope.url
    except Exception:
        scope_url = page.url

    print(
        f"实际退款控件所在Frame：{scope_url}"
    )

    # 如果只是外层壳，没有找到真正退款控件，先尝试点击左侧“退款管理”
    try:
        test_count = 0

        for text in [
            "申请时间",
            "售后状态",
            "退款成功",
        ]:
            try:
                test_count += await scope.get_by_text(
                    text,
                    exact=True
                ).count()
            except Exception:
                pass

        if test_count == 0:
            menu = page.get_by_text(
                "退款管理",
                exact=True
            )

            mc = await menu.count()

            for i in range(mc - 1, -1, -1):
                item = menu.nth(i)

                try:
                    if await item.is_visible():
                        print("当前只是交易外层页面，自动点击左侧【退款管理】...")
                        await item.click(timeout=5000)
                        await page.wait_for_timeout(5000)
                        break
                except Exception:
                    continue

            scope = await find_refund_scope(
                page
            )

            try:
                scope_url = scope.url
            except Exception:
                scope_url = page.url

            print(
                f"点击退款管理后实际Frame：{scope_url}"
            )

    except Exception:
        pass

    pager = RefundPager(
        page,
        context,
        scope
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

        page_size = int(
            result.get(
                "page_size",
                20
            )
            or
            20
        )

        print(
            f"第1页：{len(rows)} 条，新增 {new_count} 条"
        )

        # V2.3：
        # 第一页少于 pageSize 就一定是最后一页。
        # 以前 0 条还继续点第2页，会误点到其他表格分页并抓入错误金额。
        if len(rows) < page_size:
            print(
                f"第1页不足 {page_size} 条，确认已到最后一页。"
            )

        else:
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
    print("千牛三店退款抓取器 V2.3")
    print("自动今天 + 退款成功 + 精确Response匹配 + 安全分页")
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

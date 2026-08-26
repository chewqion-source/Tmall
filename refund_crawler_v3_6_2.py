# -*- coding: utf-8 -*-
"""
千牛三店退款抓取器 V3.6.2
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

# 稳定后缩短等待；真正退款列表正常通常几秒内返回。
WAIT_RESPONSE = 18

# 三家店使用不同 CDP 端口，可以并行抓取。
PARALLEL_SHOPS = True
MAX_CONCURRENT_SHOPS = 3

# 正常运行关闭重型诊断，失败时仍会输出必要摘要。
VERBOSE_DIAGNOSTICS = False

# 原始 JSON 保留，但改为紧凑格式，减少磁盘写入耗时。
SAVE_RAW_JSON = True


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

        if name == "国货严选":
            print("ℹ️ 国货严选使用淘工厂专用适配器，跳过千牛退款抓取")
            continue

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



def looks_like_refund_record(item):
    """
    严格判断一条对象是否像退款单。
    至少需要：
      - bizOrderId / orderId 类订单字段
      - refundFee / disputeBodyVO / buyerActualFee 之一
    """
    if not isinstance(item, dict):
        return False

    text = json.dumps(
        item,
        ensure_ascii=False
    )

    has_order = any(
        key in text
        for key in [
            "bizOrderId",
            "mainOrderId",
            "orderId",
        ]
    )

    has_refund = any(
        key in text
        for key in [
            "refundFee",
            "buyerActualFee",
            "disputeBodyVO",
        ]
    )

    return (
        has_order
        and
        has_refund
    )


def discover_refund_list_anywhere(obj):
    """
    V3.1 用于咖时光：
    有些店铺最终列表可能不在 disputelistv2 的标准路径，
    或真正列表来自另一个JSON接口。

    递归扫描所有 list，但只有“元素明确像退款单”的数组才接受。
    """
    matches = []

    def walk(x, path=""):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}" if path else str(k)

                if isinstance(v, list) and v:
                    sample_count = 0

                    for item in v[:8]:
                        if looks_like_refund_record(
                            item
                        ):
                            sample_count += 1

                    if sample_count:
                        matches.append(
                            (
                                sample_count,
                                len(v),
                                p,
                                v
                            )
                        )

                walk(
                    v,
                    p
                )

        elif isinstance(x, list):
            for i, v in enumerate(x[:10]):
                walk(
                    v,
                    f"{path}[{i}]"
                )

    walk(obj)

    if not matches:
        return [], ""

    matches.sort(
        key=lambda z: (
            z[0],
            z[1]
        ),
        reverse=True
    )

    _, _, path, rows = matches[0]

    return (
        rows,
        f"{path} [dynamic-discovery]"
    )

def find_refund_records(obj):
    """
    V3.0：
    先精确找 disputeGridVOList；
    如果某店新版 Response 结构不同，再启用“安全回退”：
    仅接受列表中元素同时具备 bizOrderId + refundFee/disputeBodyVO 的数组。

    这样兼容咖时光的不同 Response 结构，同时避免重新抓错其它页面数组。
    """

    exact_matches = []
    fallback_matches = []

    def walk(x, path=""):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}" if path else str(k)

                if (
                    k == "disputeGridVOList"
                    and
                    isinstance(v, list)
                ):
                    exact_matches.append(
                        (
                            p,
                            v
                        )
                    )

                if isinstance(v, list) and v:
                    valid = 0

                    for sample in v[:5]:
                        if not isinstance(sample, dict):
                            continue

                        text = json.dumps(
                            sample,
                            ensure_ascii=False
                        )

                        has_order = (
                            "bizOrderId" in text
                        )

                        has_refund = (
                            "refundFee" in text
                            or
                            "disputeBodyVO" in text
                        )

                        if has_order and has_refund:
                            valid += 1

                    if valid:
                        fallback_matches.append(
                            (
                                valid,
                                len(v),
                                p,
                                v
                            )
                        )

                walk(v, p)

        elif isinstance(x, list):
            for i, v in enumerate(x[:10]):
                walk(
                    v,
                    f"{path}[{i}]"
                )

    walk(obj)

    if exact_matches:
        exact_matches.sort(
            key=lambda item: len(item[1]),
            reverse=True
        )

        path, rows = exact_matches[0]

        return rows, path

    if fallback_matches:
        fallback_matches.sort(
            key=lambda item: (
                item[0],
                item[1]
            ),
            reverse=True
        )

        _, _, path, rows = fallback_matches[0]

        return rows, f"{path} [safe-fallback]"

    return [], ""



def find_total_fields(obj):
    """
    仅用于调试输出：递归寻找可能的 total/count/pagination 字段。
    不参与退款记录筛选，也不参与退款金额计算。
    """
    found = []

    def walk(x, path=""):
        if len(found) >= 30:
            return

        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}" if path else str(k)
                kl = str(k).lower()

                if (
                    "total" in kl
                    or kl in {"count", "recordcount", "totalnumber"}
                ):
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        found.append((p, v))

                walk(v, p)

        elif isinstance(x, list):
            for i, v in enumerate(x[:10]):
                walk(v, f"{path}[{i}]")

    walk(obj)
    return found

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



def expected_refund_success_token(page_url):
    """
    V2.9：
    三个店铺当前都已升级为新版退款管理页。
    新版【售后状态 -> 退款成功】统一使用：
        refundStatusSelect = 5

    不再根据 URL 是否带 /tp/ 判断版本。
    """
    return "5"


def params_look_refund_success(params, expected_token=None):
    """
    V2.9：
    三个店铺均按新版退款管理页处理。

    正确状态必须是：
        refundStatusSelect = 5

    以下都不接受：
        []
        [-1]
        [-9999]
        [3]
        [-1, 5]
        [-9999, 5]
        [3, 5]

    真实请求必须最终只剩一个 5。
    """

    raw = params.get(
        "refundStatusSelect"
    )

    if raw is None:
        return (
            False,
            []
        )

    tokens = re.findall(
        r"-?\d+",
        str(raw)
    )

    candidates = [
        (
            "refundStatusSelect",
            token
        )
        for token in tokens
    ]

    ok = (
        tokens == ["5"]
    )

    return (
        ok,
        candidates
    )


def get_refund_status_tokens(params):
    raw = params.get(
        "refundStatusSelect"
    )

    if raw is None:
        return []

    return re.findall(
        r"-?\d+",
        str(raw)
    )


async def open_after_sale_status_dropdown(scope):
    """
    找到【售后状态】所在筛选控件并展开。
    """
    try:
        labels = scope.get_by_text(
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

                # 当前控件通常会显示“退款成功 / 已选择x/14项”
                candidates = node.locator(
                    "[role='combobox'],"
                    "[class*='select'],"
                    "[class*='picker'],"
                    "input"
                )

                cc = await candidates.count()

                for j in range(cc):
                    item = candidates.nth(j)

                    try:
                        if not await item.is_visible():
                            continue

                        box = await item.bounding_box()

                        if (
                            box
                            and
                            box["width"] < 500
                            and
                            box["height"] < 120
                        ):
                            await item.click(
                                timeout=3000
                            )
                            await scope.wait_for_timeout(
                                300
                            )
                            return True

                    except Exception:
                        continue

                # 兜底：点同一行里显示的当前值
                for text in [
                    "退款成功",
                    "全部",
                    "进行中的订单",
                ]:
                    try:
                        loc = node.get_by_text(
                            text,
                            exact=True
                        )

                        c = await loc.count()

                        for j in range(c):
                            item = loc.nth(j)

                            if await item.is_visible():
                                await item.click(
                                    timeout=3000
                                )
                                await scope.wait_for_timeout(
                                    300
                                )
                                return True
                    except Exception:
                        pass

    except Exception:
        pass

    return False


async def repair_refund_status(
    scope,
    status_tokens,
    expected_token="5"
):
    """
    V2.9：
    三店统一新版，只保留退款成功 token=5。

    其它状态全部清除：
      -1    进行中的订单
      -9999 全部
      -2    退款完结
      3     旧状态残留

    如果当前没有 5，则最后重新选择一次【退款成功】。
    """

    if status_tokens == ["5"]:
        return True

    extras = [
        token
        for token in status_tokens
        if token != "5"
    ]

    if extras:
        print(
            f"检测到额外售后状态：{extras}，目标只保留退款成功 token=5"
        )

    mapping = {
        "-1": "进行中的订单",
        "-9999": "全部",
        "-2": "退款完结",
        "3": "退款成功",
    }

    for token in extras:
        label_text = mapping.get(token)

        if not label_text:
            print(
                f"⚠ 未识别额外状态 token={token}，跳过"
            )
            continue

        opened = await open_after_sale_status_dropdown(
            scope
        )

        if not opened:
            print(
                f"⚠ 无法展开售后状态下拉，不能清除 token={token}"
            )
            continue

        try:
            option = scope.get_by_text(
                label_text,
                exact=True
            )

            count = await option.count()

            clicked = False

            for i in range(
                count - 1,
                -1,
                -1
            ):
                item = option.nth(i)

                try:
                    if not await item.is_visible():
                        continue

                    await item.click(
                        timeout=4000
                    )

                    await scope.wait_for_timeout(
                        500
                    )

                    clicked = True

                    print(
                        f"已尝试取消额外状态：{label_text} ({token})"
                    )

                    break

                except Exception:
                    continue

            if not clicked:
                print(
                    f"⚠ 未找到可点击状态项：{label_text}"
                )

        except Exception:
            pass

    # 如果原请求里没有5，则确保重新点一次退款成功
    if "5" not in status_tokens:
        try:
            print("当前未选中退款成功，重新选择【退款成功】...")
            await set_status_refund_success(
                scope
            )
        except Exception:
            pass

    await scope.wait_for_timeout(
        800
    )

    return True


def response_selected_refund_statuses(obj):
    """
    从真实 Response 的配置里读取：
      data.resultData.refundStatusSelectVO.selectedId

    这是页面最终已选状态的权威回显。
    """
    try:
        data = obj.get(
            "data",
            {}
        )

        result_data = data.get(
            "resultData",
            {}
        )

        vo = result_data.get(
            "refundStatusSelectVO",
            {}
        )

        selected = vo.get(
            "selectedId",
            ""
        )

        return re.findall(
            r"-?\d+",
            str(selected)
        )

    except Exception:
        return []


def get_page_info(params):
    """
    从真实退款查询请求中读取 pageNo / pageSize。
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


def request_is_target_refund_query(request, expected_token=None):
    """
    只接受：
      日期 = 今天
      售后状态 = 当前页面版本对应的“退款成功”
    """
    try:
        params = decode_mtop_params_from_request(
            request
        )

        today_ok, _ = params_are_today(
            params
        )

        status_ok, _ = params_look_refund_success(
            params,
            expected_token
        )

        page_no, page_size = get_page_info(
            params
        )

        return (
            today_ok and status_ok,
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


async def scrape_refund_rows_from_dom(page, preferred_scope=None):
    """
    V3.4：
    只在标准 API 没有退款明细时启用。

    与 V3.3 不同：
    - 不再依赖 document.querySelector；
    - 改用 Playwright Locator 搜索文字，能穿透 open shadow DOM；
    - 扫描 Page + 所有 Frame；
    - 从“退款总额/退款金额/买家实付”等文字节点向上找最小退款行。

    这是为了兼容咖时光页面结构；另外两家仍走已验证正确的 API。
    """

    scopes = []

    if preferred_scope is not None:
        scopes.append(preferred_scope)

    scopes.append(page)

    try:
        scopes.extend(list(page.frames))
    except Exception:
        pass

    # 去重
    uniq = []
    seen = set()

    for scope in scopes:
        key = id(scope)

        if key in seen:
            continue

        seen.add(key)
        uniq.append(scope)

    candidate_texts = []

    anchors = [
        "退款总额",
        "退款金额",
        "买家实付",
        "售后状态",
        "售后单号",
        "订单编号",
    ]

    for scope in uniq:
        # 方案A：从关键文字节点向上找行
        for anchor in anchors:
            try:
                loc = scope.get_by_text(
                    anchor,
                    exact=False
                )

                count = await loc.count()

                for i in range(
                    min(
                        count,
                        50
                    )
                ):
                    node = loc.nth(i)

                    try:
                        if not await node.is_visible():
                            continue
                    except Exception:
                        continue

                    cur = node

                    # 向上最多8层，寻找同时包含订单+金额+退款成功的最小容器
                    for _ in range(8):
                        try:
                            text = (
                                await cur.inner_text(
                                    timeout=2000
                                )
                            ).strip()
                        except Exception:
                            text = ""

                        if text:
                            has_order = (
                                "订单编号" in text
                                or
                                "订单号" in text
                                or
                                "售后单号" in text
                            )

                            has_amount = (
                                "退款总额" in text
                                or
                                "退款金额" in text
                                or
                                "买家实付" in text
                            )

                            has_status = (
                                "退款成功" in text
                            )

                            if (
                                has_order
                                and
                                has_amount
                                and
                                has_status
                            ):
                                candidate_texts.append(
                                    text
                                )
                                break

                        try:
                            cur = cur.locator(
                                "xpath=.."
                            )
                        except Exception:
                            break

            except Exception:
                continue

        # 方案B：直接读取当前 scope 可见正文，按退款行块切割
        try:
            body_text = (
                await scope.locator(
                    "body"
                ).inner_text(
                    timeout=3000
                )
            )

            if body_text:
                # 用“售后单号”切块，适配当前退款列表页面
                blocks = re.split(
                    r"(?=售后单号\s*\d+)",
                    body_text
                )

                for block in blocks:
                    if (
                        "退款成功" in block
                        and
                        (
                            "退款总额" in block
                            or
                            "退款金额" in block
                        )
                        and
                        (
                            "订单编号" in block
                            or
                            "订单号" in block
                        )
                    ):
                        candidate_texts.append(
                            block[:5000]
                        )

        except Exception:
            pass

    # 去重
    candidate_texts = list(
        dict.fromkeys(
            candidate_texts
        )
    )

    rows = []

    for text in candidate_texts:
        # 订单号
        order_match = re.search(
            r"(?:订单编号|订单号)\s*[:：]?\s*(\d{10,})",
            text
        )

        # 售后单号作为补充信息
        aftersale_match = re.search(
            r"售后单号\s*[:：]?\s*(\d{10,})",
            text
        )

        # 金额优先级：
        # 页面截图中“退款总额”就是用户手工核对口径
        amount = None

        for pattern in [
            r"退款总额\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
            r"退款金额\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        ]:
            m = re.search(
                pattern,
                text
            )

            if m:
                amount = float(
                    m.group(1)
                )
                break

        if amount is None:
            continue

        rows.append({
            "订单号":
                order_match.group(1)
                if order_match
                else "",

            "退款金额":
                amount,

            "refundFee原值":
                f"¥{amount:.2f}",

            "买家实际支付货款":
                0.0,

            "buyerActualFee原值":
                "",

            "退款状态":
                "退款成功",

            "退款类型":
                "",

            "退款金额选项":
                "",

            "原始记录":
                json.dumps(
                    {
                        "source":
                            "PLAYWRIGHT_DOM_FALLBACK_V3_4",

                        "aftersale_id":
                            aftersale_match.group(1)
                            if aftersale_match
                            else "",

                        "text":
                            text,
                    },
                    ensure_ascii=False
                ),
        })

    # 严格去重
    dedup = []
    seen_rows = set()

    for row in rows:
        key = (
            row["订单号"],
            row["退款金额"],
            row["原始记录"]
            if not row["订单号"]
            else ""
        )

        if key in seen_rows:
            continue

        seen_rows.add(
            key
        )

        dedup.append(
            row
        )

    return dedup


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
        self.expected_token = expected_refund_success_token(
            page.url
        )
        self.matching_response_count = 0
        self.last_matching_response_summary = []
        self.query_verified = False

    async def on_response(
        self,
        response
    ):
        """
        V3.5：
        最终查询 Request 已确认：
          日期=今天
          refundStatusSelect=5

        确认以后，同一个 disputelistv2 后续 Response
        不再要求自己的 Request 再重复携带完整筛选参数。

        咖时光会先返回 donutVO 汇总，再返回真正退款列表；
        之前后者被严格 request 校验过滤掉了。
        """

        try:
            if (
                self.future is None
                or
                self.future.done()
            ):
                return

            if not self.query_verified:
                return

            if API_KEY.lower() not in (
                response.url
                or
                ""
            ).lower():
                return

            text = await response.text()

            try:
                obj = json.loads(
                    text
                )
            except Exception:
                return

            self.matching_response_count += 1

            rows, rows_path = find_refund_records(
                obj
            )

            response_statuses = response_selected_refund_statuses(
                obj
            )

            debug_totals = (
                find_total_fields(obj)[:12]
                if VERBOSE_DIAGNOSTICS
                else []
            )

            self.last_matching_response_summary = [
                f"match#{self.matching_response_count}",
                f"rows_path={rows_path or '(none)'}",
                f"rows={len(rows)}",
                f"response_statuses={response_statuses}",
                f"totals={debug_totals}",
                f"url={response.url}",
            ]

            if not rows_path:
                print(
                    f"收到汇总/初始化Response #{self.matching_response_count}，"
                    "未含退款明细，继续等待真正列表..."
                )
                return

            valid_rows = [
                r
                for r in rows
                if looks_like_refund_record(
                    r
                )
            ]

            if rows and not valid_rows:
                return

            try:
                params = decode_mtop_params_from_request(
                    response.request
                )

                page_no, page_size = get_page_info(
                    params
                )

            except Exception:
                params = {}
                page_no = 1
                page_size = 20

            self.future.set_result({
                "json":
                    obj,

                "rows":
                    valid_rows if rows else [],

                "rows_path":
                    rows_path,

                "response_statuses":
                    response_statuses,

                "total_fields":
                    (
                        find_total_fields(
                            obj
                        )
                        if VERBOSE_DIAGNOSTICS
                        else []
                    ),

                "params":
                    params,

                "page_no":
                    page_no,

                "page_size":
                    page_size,

                "response_url":
                    response.url,

                "standard_api":
                    True,
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
        V2.5：
        先设置日期与退款成功；
        如果真实请求仍出现 [-1,5] / [-9999,5]，
        根据请求里真实的额外状态自动回到 UI 取消，再重新查询。
        最多修复 3 轮。
        """

        await ensure_combination_query_open(
            self.scope
        )

        await set_today_date(
            self.scope
        )

        await self.page.wait_for_timeout(
            250
        )

        await set_status_refund_success(
            self.scope
        )

        await self.page.wait_for_timeout(
            450
        )

        for attempt in range(
            1,
            4
        ):
            self.query_verified = False

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

            # 丢弃上一轮 Response
            self.future = (
                asyncio.get_running_loop()
                .create_future()
            )

            try:
                candidates = await get_query_candidates(
                    self.scope
                )

                print(
                    f"最终查询第{attempt}轮：找到按钮 {len(candidates)} 个"
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
                        f"尝试查询按钮 {idx}/{len(candidates)}："
                        f"{txt or '(无文字)'}"
                    )

                    try:
                        await item.click(
                            timeout=4000
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
                    raise RuntimeError(
                        "最终查询没有触发 disputelistv2"
                    )

                params = decode_mtop_params_from_request(
                    request
                )

                today_ok, date_values = params_are_today(
                    params
                )

                status_tokens = get_refund_status_tokens(
                    params
                )

                status_ok, status_values = params_look_refund_success(
                    params,
                    self.expected_token
                )

                print(
                    f"第{attempt}轮请求："
                    f"日期={'OK' if today_ok else '失败'}，"
                    f"状态token={status_tokens}，"
                    f"新版退款成功应为={self.expected_token}"
                )

                print(
                    f"实际日期参数：{date_values}"
                )

                if not today_ok:
                    raise RuntimeError(
                        "最终查询日期不是今天"
                    )

                if status_ok:
                    print(
                        f"售后状态已确认：退款成功(token=5)"
                    )

                    # 最终查询Request已经验证正确。
                    # 从现在开始接收同API的后续列表Response。
                    self.query_verified = True

                    # 丢弃前面筛选动作产生的旧Response。
                    self.future = (
                        asyncio.get_running_loop()
                        .create_future()
                    )

                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(
                                self.future
                            ),
                            timeout=WAIT_RESPONSE
                        )

                    except asyncio.TimeoutError:
                        print(
                            "已确认查询条件，但等待真正退款列表Response超时，最后尝试页面兜底..."
                        )

                        dom_rows = await scrape_refund_rows_from_dom(
                            self.page,
                            self.scope
                        )

                        if dom_rows:
                            print(
                                f"Playwright页面兜底成功：读取到 {len(dom_rows)} 条退款记录"
                            )

                            result = {
                                "json": {},
                                "rows": dom_rows,
                                "rows_path": "PAGE_DOM_FALLBACK",
                                "response_statuses": ["5"],
                                "total_fields": [],
                                "params": params,
                                "page_no": 1,
                                "page_size": 20,
                                "response_url": self.page.url,
                                "standard_api": False,
                                "dom_fallback": True,
                            }

                        else:
                            raise RuntimeError(
                                "请求条件正确，但标准API只有汇总没有明细；Playwright 已扫描 Page/所有Frame/Shadow DOM，仍未识别到退款行。"
                                f" 已看到匹配Response={self.matching_response_count}个；"
                                f" 最后一条摘要={self.last_matching_response_summary}"
                            )

                    print(
                        f"捕获目标数据：pageNo={result.get('page_no')} "
                        f"pageSize={result.get('page_size')} "
                        f"records={len(result.get('rows', []))}"
                    )

                    print(
                        f"Response售后状态回显："
                        f"{result.get('response_statuses', [])}"
                    )

                    print(
                        f"退款列表路径："
                        f"{result.get('rows_path') or '(未找到退款列表)'}"
                    )

                    print(
                        f"退款数据Response：{result.get('response_url', '(unknown)')}"
                    )

                    print(
                        f"Response汇总候选："
                        f"{result.get('total_fields', [])}"
                    )

                    return result

                # 请求中混入了其它状态，自动修复后重试
                print(
                    f"当前还混有其他售后状态，状态参数：{status_values}"
                )

                await repair_refund_status(
                    self.scope,
                    status_tokens,
                    self.expected_token
                )

                await self.page.wait_for_timeout(
                    500
                )

            finally:
                try:
                    self.context.remove_listener(
                        "request",
                        on_request
                    )
                except Exception:
                    pass

        debug_path = await dump_filter_debug(
            self.scope,
            shop_name
        )

        raise RuntimeError(
            "连续3轮仍无法把售后状态修正为【退款成功】（新版=5）。"
            f" 控件调试：{debug_path}"
        )

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


async def hard_reset_refund_page(page, shop_name):
    """
    V3.6.2：
    某些店铺浏览器长时间运行后，退款筛选控件会处于脏状态：
      页面文字看似已经选择退款成功，
      但真实请求仍反复带 -1/5 或其它组合。

    这时继续点同一套控件没有意义。
    直接重新进入退款管理页，重新获取 Frame/Scope，再设置筛选。
    """

    print(
        f"⚠ [{shop_name}] 退款筛选连续修复失败，执行一次退款页硬重置..."
    )

    try:
        await page.goto(
            REFUND_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )
    except Exception:
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=60000
            )
        except Exception:
            pass

    await page.wait_for_timeout(
        6500
    )

    scope = await find_refund_scope(
        page
    )

    # 如果只落在外壳，再主动点一次退款管理
    try:
        hit = 0

        for text in [
            "申请时间",
            "售后状态",
            "退款成功",
        ]:
            try:
                hit += await scope.get_by_text(
                    text,
                    exact=True
                ).count()
            except Exception:
                pass

        if hit == 0:
            menu = page.get_by_text(
                "退款管理",
                exact=True
            )

            count = await menu.count()

            for i in range(
                count - 1,
                -1,
                -1
            ):
                item = menu.nth(i)

                try:
                    if await item.is_visible():
                        await item.click(
                            timeout=5000
                        )
                        await page.wait_for_timeout(
                            5000
                        )
                        break
                except Exception:
                    continue

            scope = await find_refund_scope(
                page
            )

    except Exception:
        pass

    return scope


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

    print(
        f"当前统一新版退款成功状态 token={pager.expected_token}"
    )

    raw_pages = []
    all_rows = []
    seen = set()

    try:

        print(
            "自动设置今天 + 售后状态退款成功 + 查询并校验..."
        )

        try:
            result = (
                await pager.prepare_and_query(
                    name
                )
            )

        except RuntimeError as e:
            msg = str(e)

            # 只针对“状态连续3轮修复失败”做一次硬重置。
            # 其它异常仍原样抛出，避免掩盖真实错误。
            if (
                "连续3轮仍无法把售后状态修正为【退款成功】"
                not in msg
            ):
                raise

            await pager.stop()

            scope = await hard_reset_refund_page(
                page,
                name
            )

            try:
                scope_url = scope.url
            except Exception:
                scope_url = page.url

            print(
                f"硬重置后退款控件所在Frame：{scope_url}"
            )

            pager = RefundPager(
                page,
                context,
                scope
            )

            await pager.start()

            print(
                "重新设置今天 + 退款成功并查询..."
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

            if result.get(
                "dom_fallback",
                False
            ):
                row = rec
            else:
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

                print(
                    f"退款列表路径：{result.get('rows_path') or '(未找到 disputeGridVOList)'}"
                )

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

    if SAVE_RAW_JSON:
        with open(
            raw_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                raw_pages,
                f,
                ensure_ascii=False,
                separators=(",", ":")
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
    print("千牛三店退款抓取器 V3.6.2")
    print("自动今天 + 退款成功=5 + 稳定加速版 + 安全分页")
    print("=" * 72)

    shops = load_shops()

    results = []
    errors = []

    async with async_playwright() as pw:

        async def run_one(shop, sem):
            async with sem:
                try:
                    result = await crawl_shop(
                        pw,
                        shop
                    )

                    return (
                        True,
                        result
                    )

                except Exception as e:
                    return (
                        False,
                        (
                            shop["name"],
                            str(e)
                        )
                    )

        if PARALLEL_SHOPS:
            sem = asyncio.Semaphore(
                MAX_CONCURRENT_SHOPS
            )

            tasks = [
                asyncio.create_task(
                    run_one(
                        shop,
                        sem
                    )
                )
                for shop in shops
            ]

            finished = await asyncio.gather(
                *tasks
            )

            for ok, payload in finished:
                if ok:
                    results.append(
                        payload
                    )
                else:
                    errors.append(
                        payload
                    )

                    print(
                        f"× [{payload[0]}] 退款抓取失败：{payload[1]}"
                    )

        else:
            sem = asyncio.Semaphore(1)

            for shop in shops:
                ok, payload = await run_one(
                    shop,
                    sem
                )

                if ok:
                    results.append(
                        payload
                    )
                else:
                    errors.append(
                        payload
                    )

                    print(
                        f"× [{payload[0]}] 退款抓取失败：{payload[1]}"
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

    # V3.6.1：把本轮真实结果直接返回给主程序，
    # 避免主程序再依赖文件修改时间判断。
    return {
        "results": results,
        "errors": errors,
    }


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

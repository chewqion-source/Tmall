from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import json
import math
import time
import re
import traceback
import copy

import numpy as np
import pandas as pd
from playwright.sync_api import sync_playwright


# ============================================================
# 千牛 / 生意参谋 / 万相 多店铺实时盈亏
# V4.2
# ============================================================

VERSION = "V4.3"


# ============================================================
# 目录
# ============================================================

BASE_DIR = Path(__file__).parent

SHOPS_FILE = BASE_DIR / "shops.json"

DATA_ROOT = BASE_DIR / "data"

COST_ROOT = BASE_DIR / "cost_config"

LOG_ROOT = BASE_DIR / "logs"


DATA_ROOT.mkdir(exist_ok=True)

COST_ROOT.mkdir(exist_ok=True)

LOG_ROOT.mkdir(exist_ok=True)


# ============================================================
# 基础参数
# ============================================================

PAGE_TIMEOUT = 90_000

SYCM_API_KEYWORD = "/cc/item/live/view/top.json"

PLAYROAD_FINDPAGE_KEYWORD = "findPage.json"

PAGE_SIZE = 40


# ============================================================
# 通用
# ============================================================

def safe_filename(name):

    return re.sub(
        r'[\\/:*?"<>|]',
        "_",
        str(name)
    ).strip()


def normalize_id(value):

    if value is None:
        return None

    value = str(value).strip()

    if value.endswith(".0"):
        value = value[:-2]

    return value


def clean_numeric(series, default=0.0):

    s = pd.to_numeric(
        series,
        errors="coerce"
    )

    s = s.replace(
        [np.inf, -np.inf],
        np.nan
    )

    s = s.fillna(
        float(default)
    )

    return s.astype(
        "float64"
    )


def nested_value(
    obj,
    key,
    default=0
):

    try:

        value = obj.get(key)

        if isinstance(value, dict):

            return value.get(
                "value",
                default
            )

        if value is None:
            return default

        return value

    except Exception:

        return default


def change_query_param(
    url,
    **params
):

    parsed = urlparse(url)

    query = parse_qs(
        parsed.query
    )

    for key, value in params.items():

        query[key] = [
            str(value)
        ]

    return urlunparse(
        parsed._replace(
            query=urlencode(
                query,
                doseq=True
            )
        )
    )


# ============================================================
# shops.json
# ============================================================

def load_shops():

    if not SHOPS_FILE.exists():

        raise RuntimeError(
            f"找不到 shops.json：{SHOPS_FILE}"
        )

    with open(
        SHOPS_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        config = json.load(f)

    shops = []
    used_ports = set()

    for shop in config.get("shops", []):

        if not shop.get("enabled", True):
            continue

        name = str(
            shop.get("name", "")
        ).strip()

        if not name:
            continue

        try:

            port = int(
                shop.get("port")
            )

        except Exception:

            print(
                f"⚠️ {name} 端口配置错误"
            )
            continue

        if port in used_ports:

            print(
                f"⚠️ {name} 端口 {port} 重复"
            )
            continue

        used_ports.add(port)

        sycm_url = str(
            shop.get("sycm_url", "")
        ).strip()

        site_url = str(
            shop.get("site_url", "")
        ).strip()

        search_url = str(
            shop.get("search_url", "")
        ).strip()

        # 生意参谋是核心数据源，必须有。
        if not sycm_url:

            print(
                f"⚠️ {name} 缺少 sycm_url，跳过该店铺"
            )
            continue

        # 两类推广页面改为可选
        if not site_url:
            print(
                f"ℹ️ {name} 未配置全站推广页面，本次全站推广按 0 处理"
            )

        if not search_url:
            print(
                f"ℹ️ {name} 未配置关键词推广页面，本次关键词推广按 0 处理"
            )

        shops.append({

            "name":
                name,

            "safe_name":
                safe_filename(name),

            "port":
                port,

            "sycm_url":
                sycm_url,

            "site_url":
                site_url,

            "search_url":
                search_url,

        })

    return shops


# ============================================================
# 生意参谋
# ============================================================

def parse_sycm_response(data):

    rows = []

    try:

        records = (
            data["data"]
            ["data"]
            ["data"]
        )

    except Exception:

        return rows

    for record in records:

        item = record.get(
            "item",
            {}
        )

        item_id = (

            item.get(
                "itemId"
            )

            or

            nested_value(
                record,
                "itemId",
                None
            )

        )

        if not item_id:
            continue

        rows.append({

            "商品ID":
                normalize_id(
                    item_id
                ),

            "商品名称":
                item.get(
                    "title",
                    ""
                ),

            "商品货号":
                item.get(
                    "itemNO",
                    ""
                ),

            "支付件数":
                nested_value(
                    record,
                    "payItmCnt",
                    0
                ),

            "支付金额":
                nested_value(
                    record,
                    "payAmt",
                    0
                ),

            "支付买家数":
                nested_value(
                    record,
                    "payByrCnt",
                    0
                ),

            "商品访客":
                nested_value(
                    record,
                    "itmUv",
                    0
                ),

            "支付转化率":
                nested_value(
                    record,
                    "payRate",
                    0
                ),

            "加购件数":
                nested_value(
                    record,
                    "itemCartCnt",
                    0
                ),

        })

    return rows


def get_sycm_record_count(data):

    try:

        return int(
            data["data"]
            ["data"]
            ["recordCount"]
        )

    except Exception:

        return 0


def crawl_sycm(
    page,
    url,
    shop_name
):

    print(
        f"\n[{shop_name}] 生意参谋..."
    )

    captured = {
        "json": None,
        "url": None,
    }

    def handler(response):

        if SYCM_API_KEYWORD not in response.url:
            return

        if captured["json"] is not None:
            return

        try:

            data = response.json()

            rows = parse_sycm_response(
                data
            )

            if rows:

                captured["json"] = data

                captured["url"] = (
                    response.url
                )

        except Exception:
            pass

    page.on(
        "response",
        handler
    )

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

    except Exception:
        pass

    for _ in range(20):

        if captured["json"] is not None:
            break

        page.wait_for_timeout(
            1000
        )

    if captured["json"] is None:

        try:

            page.reload(
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

        except Exception:
            pass

        for _ in range(20):

            if captured["json"] is not None:
                break

            page.wait_for_timeout(
                1000
            )

    if captured["json"] is None:

        raise RuntimeError(
            "生意参谋接口未捕获，可能登录失效"
        )

    rows = parse_sycm_response(
        captured["json"]
    )

    record_count = (
        get_sycm_record_count(
            captured["json"]
        )
    )

    first_url = (
        captured["url"]
    )

    parsed = urlparse(
        first_url
    )

    query = parse_qs(
        parsed.query
    )

    try:

        page_size = int(
            query.get(
                "pageSize",
                ["10"]
            )[0]
        )

    except Exception:

        page_size = 10

    if record_count <= 0:

        record_count = len(
            rows
        )

    total_pages = max(
        1,
        math.ceil(
            record_count
            /
            page_size
        )
    )

    print(
        f"[{shop_name}] 生意参谋商品 {record_count} 个"
    )

    for page_no in range(
        2,
        total_pages + 1
    ):

        request_url = (
            change_query_param(
                first_url,
                page=page_no
            )
        )

        try:

            data = page.evaluate(
                """
                async (url) => {

                    const r =
                        await fetch(
                            url,
                            {
                                method: "GET",
                                credentials: "include",
                                cache: "no-store"
                            }
                        );

                    return await r.json();
                }
                """,
                request_url
            )

            current = (
                parse_sycm_response(
                    data
                )
            )

            rows.extend(
                current
            )

        except Exception as e:

            print(
                f"⚠️ [{shop_name}] 生意参谋第 {page_no} 页失败：{e}"
            )

    df = pd.DataFrame(
        rows
    )

    if df.empty:

        raise RuntimeError(
            "生意参谋数据为空"
        )

    df["商品ID"] = (
        df["商品ID"]
        .astype(str)
    )

    df = (
        df
        .drop_duplicates(
            "商品ID",
            keep="last"
        )
        .reset_index(
            drop=True
        )
    )

    print(
        f"✅ [{shop_name}] 经营商品：{len(df)}"
    )

    return df


# ============================================================
# Playroad 商品记录查找
# ============================================================

def find_material_records(obj):

    found = []

    if isinstance(obj, dict):

        if obj.get("materialId") is not None:
            found.append(obj)

        for value in obj.values():
            found.extend(
                find_material_records(value)
            )

    elif isinstance(obj, list):

        for item in obj:
            found.extend(
                find_material_records(item)
            )

    return found


# ============================================================
# reportInfoList 工具
# ============================================================

def get_report_list(record):

    report_list = record.get(
        "reportInfoList"
    )

    if isinstance(report_list, list):
        return report_list

    return []


def get_report_sum(
    record,
    field
):

    report_list = get_report_list(
        record
    )

    # 正常情况：指标放在 reportInfoList 内
    if report_list:

        total = 0.0

        for report in report_list:

            if not isinstance(report, dict):
                continue

            try:

                value = report.get(
                    field,
                    0
                )

                if value is None:
                    value = 0

                total += float(value)

            except Exception:
                pass

        return total

    # 兼容未来接口把指标直接放到商品记录上的情况
    try:

        direct_value = record.get(
            field,
            0
        )

        if direct_value is None:
            return 0.0

        return float(
            direct_value
        )

    except Exception:

        return 0.0


def get_report_weighted_average(
    record,
    field,
    weight_field="charge"
):

    report_list = get_report_list(
        record
    )

    weighted_total = 0.0
    weight_total = 0.0
    values = []

    for report in report_list:

        if not isinstance(report, dict):
            continue

        try:

            value = report.get(
                field
            )

            if value is None:
                continue

            value = float(
                value
            )

            weight = float(
                report.get(
                    weight_field,
                    0
                )
                or
                0
            )

            if weight > 0:
                weighted_total += (
                    value
                    *
                    weight
                )
                weight_total += weight

            values.append(
                value
            )

        except Exception:
            pass

    if weight_total > 0:
        return (
            weighted_total
            /
            weight_total
        )

    if values:
        return (
            sum(values)
            /
            len(values)
        )

    try:

        direct_value = record.get(
            field
        )

        if direct_value is None:
            return 0.0

        return float(
            direct_value
        )

    except Exception:

        return 0.0


def make_report_fingerprint(
    record
):

    """
    同一个 findPage Response 中有时会递归出现两份完全相同的商品对象。
    生成指纹去掉 JSON 结构重复，但不去掉真正不同的推广计划/单元。
    """

    material_id = normalize_id(
        record.get("materialId")
    )

    report_list = get_report_list(
        record
    )

    fingerprint_source = {
        "materialId": material_id,
        "materialName": record.get(
            "materialName",
            ""
        ),
        "campaignName": record.get(
            "campaignName",
            ""
        ),
        "dynamicTitleFlag": record.get(
            "dynamicTitleFlag"
        ),
        "reportInfoList": report_list,
    }

    try:

        return json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            default=str
        )

    except Exception:

        return (
            f"{material_id}|"
            f"{record.get('materialName', '')}|"
            f"{record.get('campaignName', '')}|"
            f"{len(report_list)}"
        )


# ============================================================
# Playroad Response 解析
# ============================================================

def _find_nested_material(record):
    """
    V4.3：
    万相 findPage 的报表字段和商品ID不一定在同一层。

    全站推广常见：
      父层 reportInfoList 有消耗
      商品ID在 adgroupList[0].material.materialId

    关键词推广常见：
      父层 reportInfoList 有消耗
      商品ID在 material.materialId
    """

    if not isinstance(record, dict):
        return None, ""

    direct_id = normalize_id(
        record.get("materialId")
    )

    if direct_id:
        return (
            direct_id,
            str(record.get("materialName", "") or "")
        )

    material = record.get("material")

    if isinstance(material, dict):
        mid = normalize_id(
            material.get("materialId")
        )

        if mid:
            return (
                mid,
                str(material.get("materialName", "") or "")
            )

    adgroups = record.get("adgroupList")

    if isinstance(adgroups, list):
        for adgroup in adgroups:
            if not isinstance(adgroup, dict):
                continue

            mid, name = _find_nested_material(
                adgroup
            )

            if mid:
                return mid, name

    last_adgroup = record.get("lastAdgroup")

    if isinstance(last_adgroup, dict):
        mid, name = _find_nested_material(
            last_adgroup
        )

        if mid:
            return mid, name

    for key, value in record.items():
        if key in {
            "reportInfoList",
            "reportInfoMap",
            "operationList",
        }:
            continue

        if isinstance(value, dict):
            mid, name = _find_nested_material(
                value
            )

            if mid:
                return mid, name

    return None, ""


def _find_report_owner_material(record):
    """
    判断 reportInfoList 真正归属的商品。
    有些万相台接口会把计划级/整页级 reportInfoList 放在父层，
    子层再挂多个商品。不能把父层消耗记到第一个商品上。
    这里只接受：
    - 当前记录自身或 record["material"] 明确有商品ID；
    - 只有一个 adgroup 子项的父层记录。
    """

    if not isinstance(record, dict):
        return None, ""

    direct_id = normalize_id(
        record.get("materialId")
    )

    if direct_id:
        return (
            direct_id,
            str(record.get("materialName", "") or "")
        )

    material = record.get("material")

    if isinstance(material, dict):
        mid = normalize_id(
            material.get("materialId")
        )

        if mid:
            return (
                mid,
                str(material.get("materialName", "") or "")
            )

    adgroups = record.get("adgroupList")

    if (
        isinstance(adgroups, list)
        and
        len(adgroups) == 1
        and
        isinstance(adgroups[0], dict)
    ):
        return _find_report_owner_material(
            adgroups[0]
        )

    return None, ""


def _find_playroad_report_records(obj):
    """
    优先找到真正承载 reportInfoList 的父级业务记录。
    旧逻辑只找到 nested material，所以 reportInfoList 常为 null，
    最终所有推广消耗被误算为 0。
    """

    found = []

    def walk(x):
        if isinstance(x, dict):
            report_list = x.get(
                "reportInfoList"
            )

            if isinstance(report_list, list):
                mid, name = _find_report_owner_material(
                    x
                )

                if mid:
                    found.append(
                        (x, mid, name)
                    )
                    return

            for value in x.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)

    return found


def parse_playroad_records(
    data,
    source_name
):

    report_records = (
        _find_playroad_report_records(
            data
        )
    )

    rows = []

    for record, material_id, nested_name in report_records:

        report_list = get_report_list(
            record
        )

        material_name = (
            nested_name
            or
            str(record.get("materialName", "") or "")
            or
            str(record.get("adgroupName", "") or "")
        )

        rows.append({

            "商品ID":
                material_id,

            f"{source_name}商品名称":
                material_name,

            f"{source_name}消耗":
                get_report_sum(
                    record,
                    "charge"
                ),

            f"{source_name}成交金额":
                get_report_sum(
                    record,
                    "alipayInshopAmt"
                ),

            f"{source_name}点击":
                get_report_sum(
                    record,
                    "click"
                ),

            f"{source_name}ROI":
                get_report_weighted_average(
                    record,
                    "roi"
                ),

            "_report_count":
                len(report_list),

            "_fingerprint":
                json.dumps(
                    {
                        "materialId": material_id,
                        "campaignId": record.get("campaignId"),
                        "adgroupId": record.get("adgroupId"),
                        "campaignName": record.get("campaignName", ""),
                        "reportInfoList": report_list,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str
                ),

        })

    return rows


def playroad_response_score(
    data,
    source_name
):

    """
    同一个页面通常会同时请求多个 findPage.json。
    V4.3 会优先选择真正父级 reportInfoList 最完整的 Response。
    """

    rows = parse_playroad_records(
        data,
        source_name
    )

    if not rows:
        return (
            0,
            0,
            0,
            0.0
        )

    temp = pd.DataFrame(
        rows
    )

    if "_fingerprint" in temp.columns:
        temp = temp.drop_duplicates(
            subset=["_fingerprint"],
            keep="first"
        )

    report_rows = int(
        (
            temp["_report_count"] > 0
        ).sum()
    )

    report_count = int(
        temp["_report_count"].sum()
    )

    unique_products = int(
        temp["商品ID"].nunique()
    )

    try:

        total_charge = float(
            clean_numeric(
                temp[
                    f"{source_name}消耗"
                ]
            ).sum()
        )

    except Exception:

        total_charge = 0.0

    return (
        report_rows,
        report_count,
        unique_products,
        total_charge
    )


def empty_playroad_df(
    source_name
):

    return pd.DataFrame(
        columns=[
            "商品ID",
            f"{source_name}商品名称",
            f"{source_name}消耗",
            f"{source_name}成交金额",
            f"{source_name}点击",
            f"{source_name}ROI",
        ]
    )


# ============================================================
# 修改 rptQuery 为今天实时
# ============================================================

def patch_today_realtime(obj):

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    if isinstance(
        obj,
        dict
    ):

        for key in list(
            obj.keys()
        ):

            if key == "startTime":

                obj[key] = today

            elif key == "endTime":

                obj[key] = today

            elif key == "isRt":

                obj[key] = True

            else:

                patch_today_realtime(
                    obj[key]
                )

    elif isinstance(
        obj,
        list
    ):

        for item in obj:

            patch_today_realtime(
                item
            )
# ============================================================
# 捕获 Playroad findPage 请求
# V4.2 核心修复：
# 1. 直接使用浏览器真实 Response
# 2. 多个 findPage 中优先选择 reportInfoList 最完整的
# 3. 没有该推广模块时不报错，返回空表
# ============================================================

def capture_findpage_request(
    page,
    url,
    expected_bizcode,
    shop_name,
    source_name
):

    if not url:

        print(
            f"ℹ️ [{shop_name}] 未配置 {source_name} 页面，本次按 0 处理"
        )

        return None

    candidates = []

    def handler(response):

        if (
            PLAYROAD_FINDPAGE_KEYWORD
            not in response.url
        ):
            return

        try:

            request = response.request

            post_data = (
                request.post_data_json
            )

            if not isinstance(
                post_data,
                dict
            ):
                return

            biz_code = (
                post_data.get(
                    "bizCode"
                )
                or
                post_data.get(
                    "mx_bizCode"
                )
            )

            if (
                biz_code
                !=
                expected_bizcode
            ):
                return

            data = response.json()

            score = (
                playroad_response_score(
                    data,
                    source_name
                )
            )

            if score[2] <= 0:
                return

            candidates.append({

                "url":
                    response.url,

                "payload":
                    copy.deepcopy(
                        post_data
                    ),

                "data":
                    data,

                "score":
                    score,

            })

        except Exception:
            pass

    page.on(
        "response",
        handler
    )

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # 等页面真实接口
    # --------------------------------------------------------

    page.wait_for_timeout(
        12000
    )

    # --------------------------------------------------------
    # 第一次没有任何候选时刷新一次
    # --------------------------------------------------------

    if not candidates:

        try:

            page.reload(
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT
            )

        except Exception:
            pass

        page.wait_for_timeout(
            12000
        )

    # --------------------------------------------------------
    # 没有抓到
    # 这里不再直接抛异常。
    # 因为可能该店铺当前没有这种推广。
    # --------------------------------------------------------

    if not candidates:

        print(
            f"ℹ️ [{shop_name}] 当前未检测到 {source_name} 数据，本次按 0 处理"
        )

        return None

    # --------------------------------------------------------
    # 选择最有价值的 Response
    #
    # score:
    # (
    #   有reportInfoList的商品记录数,
    #   reportInfo总条数,
    #   商品数,
    #   总charge
    # )
    #
    # 优先：
    # reportInfo覆盖更多
    # reportInfo条数更多
    # 商品更多
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x[
            "score"
        ],
        reverse=True
    )

    best = candidates[0]

    score = best[
        "score"
    ]

    print(
        f"✅ [{shop_name}] 捕获 {source_name} 接口"
    )

    print(
        f"[{shop_name}] {source_name} 原始商品：{score[2]}，"
        f"带报表记录：{score[0]}，"
        f"原始消耗：¥{score[3]:.2f}"
    )

    return best


# ============================================================
# Playroad 数据聚合
# ============================================================

def aggregate_playroad_rows(
    rows,
    source_name
):

    if not rows:

        return empty_playroad_df(
            source_name
        )

    raw_df = pd.DataFrame(
        rows
    )

    if raw_df.empty:

        return empty_playroad_df(
            source_name
        )

    # --------------------------------------------------------
    # 去掉递归 JSON 结构产生的完全重复记录
    # 但不同计划 / 单元不会被删
    # --------------------------------------------------------

    if "_fingerprint" in raw_df.columns:

        raw_df = (
            raw_df
            .drop_duplicates(
                subset=[
                    "_fingerprint"
                ],
                keep="first"
            )
            .reset_index(
                drop=True
            )
        )

    numeric_cols = [
        f"{source_name}消耗",
        f"{source_name}成交金额",
        f"{source_name}点击",
        f"{source_name}ROI",
    ]

    for col in numeric_cols:

        if col not in raw_df.columns:
            raw_df[col] = 0

        raw_df[col] = clean_numeric(
            raw_df[col]
        )

    weighted_roi_col = (
        f"_{source_name}ROI加权值"
    )
    raw_df[weighted_roi_col] = (
        raw_df[f"{source_name}ROI"]
        *
        raw_df[f"{source_name}消耗"]
    )

    # --------------------------------------------------------
    # 同一个 materialId
    # 多计划 / 多单元 / 多 reportInfo
    # 全部求和
    # --------------------------------------------------------

    agg = (
        raw_df
        .groupby(
            "商品ID",
            as_index=False
        )
        .agg({

            f"{source_name}消耗":
                "sum",

            f"{source_name}成交金额":
                "sum",

            f"{source_name}点击":
                "sum",

            weighted_roi_col:
                "sum",

            f"{source_name}商品名称":
                "first",

        })
    )

    roi_weight = clean_numeric(
        agg[f"{source_name}消耗"]
    )

    agg[f"{source_name}ROI"] = np.where(
        roi_weight > 0,
        clean_numeric(
            agg[weighted_roi_col]
        )
        /
        roi_weight,
        0.0
    )

    agg = agg.drop(
        columns=[
            weighted_roi_col
        ]
    )

    return agg


# ============================================================
# Playroad 分页
# V4.2:
# 第一页直接使用浏览器原始 Response
# 不再重放 offset=0
# ============================================================

def crawl_playroad(
    page,
    url,
    expected_bizcode,
    source_name,
    shop_name
):

    print(
        f"[{shop_name}] {source_name}..."
    )

    captured = (
        capture_findpage_request(
            page,
            url,
            expected_bizcode,
            shop_name,
            source_name
        )
    )

    # --------------------------------------------------------
    # 当前没有这种推广
    # --------------------------------------------------------

    if captured is None:

        return empty_playroad_df(
            source_name
        )

    request_url = (
        captured["url"]
    )

    base_payload = copy.deepcopy(
        captured["payload"]
    )

    # ========================================================
    # 第一页
    # V4.4：
    # 捕获到的浏览器 Response 只用于确认接口和请求结构。
    # 页面自动发出的第一页可能带着旧日期/旧筛选，所以这里必须
    # 重新 POST 一次已经 patch 成“今天实时”的 payload。
    # ========================================================

    patch_today_realtime(
        base_payload
    )

    try:
        first_payload = copy.deepcopy(
            base_payload
        )

        first_payload["offset"] = 0

        first_data = page.evaluate(
            """
            async ({url, payload}) => {

                const response =
                    await fetch(
                        url,
                        {
                            method: "POST",
                            credentials: "include",
                            headers: {
                                "Content-Type":
                                    "application/json;charset=UTF-8"
                            },
                            body:
                                JSON.stringify(
                                    payload
                                )
                        }
                    );

                if (!response.ok) {

                    throw new Error(
                        "HTTP "
                        +
                        response.status
                    );
                }

                return await response.json();
            }
            """,
            {
                "url":
                    request_url,

                "payload":
                    first_payload,
            }
        )

    except Exception as e:
        print(
            f"⚠️ [{shop_name}] {source_name} "
            f"今天实时第1页重放失败：{e}，本模块按 0 处理"
        )

        return empty_playroad_df(
            source_name
        )

    all_rows = (
        parse_playroad_records(
            first_data,
            source_name
        )
    )

    # --------------------------------------------------------
    # 去掉第一页 JSON 结构重复
    # --------------------------------------------------------

    if all_rows:

        first_df = pd.DataFrame(
            all_rows
        )

        if (
            "_fingerprint"
            in first_df.columns
        ):

            first_df = (
                first_df
                .drop_duplicates(
                    subset=[
                        "_fingerprint"
                    ],
                    keep="first"
                )
            )

        first_unique_rows = (
            first_df
            .to_dict(
                "records"
            )
        )

    else:

        first_unique_rows = []

    all_rows = first_unique_rows

    first_product_count = len(
        {
            row[
                "商品ID"
            ]
            for row in first_unique_rows
            if row.get(
                "商品ID"
            )
        }
    )

    first_charge = sum(
        float(
            row.get(
                f"{source_name}消耗",
                0
            )
            or
            0
        )
        for row in first_unique_rows
    )

    print(
        f"[{shop_name}] {source_name} 第1页："
        f"{first_product_count} 个商品，"
        f"消耗 ¥{first_charge:.2f}"
    )

    # ========================================================
    # 分页参数
    # ========================================================

    try:

        request_page_size = int(
            base_payload.get(
                "pageSize",
                PAGE_SIZE
            )
        )

    except Exception:

        request_page_size = (
            PAGE_SIZE
        )

    if request_page_size <= 0:

        request_page_size = (
            PAGE_SIZE
        )

    base_payload[
        "pageSize"
    ] = request_page_size

    # --------------------------------------------------------
    # 第一页浏览器通常是 offset=0
    # 第二页从 pageSize 开始
    # --------------------------------------------------------

    offset = request_page_size

    max_pages = 100

    previous_page_signature = None

    # --------------------------------------------------------
    # 记录第一页商品 / 数据签名
    # 防止接口忽略 offset，反复返回第一页
    # --------------------------------------------------------

    if first_unique_rows:

        previous_page_signature = (
            tuple(
                sorted(
                    [
                        (
                            str(
                                row.get(
                                    "商品ID",
                                    ""
                                )
                            ),
                            round(
                                float(
                                    row.get(
                                        f"{source_name}消耗",
                                        0
                                    )
                                    or
                                    0
                                ),
                                6
                            ),
                        )
                        for row in first_unique_rows
                    ]
                )
            )
        )

    # ========================================================
    # 第2页以后
    # ========================================================

    for page_index in range(
        2,
        max_pages + 1
    ):

        payload = copy.deepcopy(
            base_payload
        )

        payload[
            "offset"
        ] = offset

        payload[
            "pageSize"
        ] = request_page_size

        try:

            data = page.evaluate(
                """
                async ({url, payload}) => {

                    const response =
                        await fetch(
                            url,
                            {
                                method: "POST",
                                credentials: "include",
                                headers: {
                                    "Content-Type":
                                        "application/json;charset=UTF-8"
                                },
                                body:
                                    JSON.stringify(
                                        payload
                                    )
                            }
                        );

                    if (!response.ok) {

                        throw new Error(
                            "HTTP "
                            +
                            response.status
                        );
                    }

                    return await response.json();
                }
                """,
                {
                    "url":
                        request_url,

                    "payload":
                        payload,
                }
            )

        except Exception as e:

            print(
                f"⚠️ [{shop_name}] {source_name} "
                f"offset={offset} 请求失败：{e}"
            )

            break

        rows = (
            parse_playroad_records(
                data,
                source_name
            )
        )

        if not rows:
            break

        page_df = pd.DataFrame(
            rows
        )

        if (
            "_fingerprint"
            in page_df.columns
        ):

            page_df = (
                page_df
                .drop_duplicates(
                    subset=[
                        "_fingerprint"
                    ],
                    keep="first"
                )
                .reset_index(
                    drop=True
                )
            )

        page_rows = (
            page_df
            .to_dict(
                "records"
            )
        )

        if not page_rows:
            break

        # ----------------------------------------------------
        # 防止 offset 没生效
        # ----------------------------------------------------

        page_signature = (
            tuple(
                sorted(
                    [
                        (
                            str(
                                row.get(
                                    "商品ID",
                                    ""
                                )
                            ),
                            round(
                                float(
                                    row.get(
                                        f"{source_name}消耗",
                                        0
                                    )
                                    or
                                    0
                                ),
                                6
                            ),
                        )
                        for row in page_rows
                    ]
                )
            )
        )

        if (
            previous_page_signature
            is not None
            and
            page_signature
            ==
            previous_page_signature
        ):

            print(
                f"ℹ️ [{shop_name}] {source_name} "
                "后续页与上一页完全一致，停止分页"
            )

            break

        previous_page_signature = (
            page_signature
        )

        product_count = len(
            {
                row[
                    "商品ID"
                ]
                for row in page_rows
                if row.get(
                    "商品ID"
                )
            }
        )

        page_charge = sum(
            float(
                row.get(
                    f"{source_name}消耗",
                    0
                )
                or
                0
            )
            for row in page_rows
        )

        print(
            f"[{shop_name}] {source_name} 第{page_index}页 "
            f"offset={offset}："
            f"{product_count} 个商品，"
            f"消耗 ¥{page_charge:.2f}"
        )

        all_rows.extend(
            page_rows
        )

        # ----------------------------------------------------
        # 少于 pageSize，通常已经是最后一页
        #
        # 注意：
        # 一条商品记录可能递归被解析多次，
        # 所以这里用 materialId 唯一数量判断
        # ----------------------------------------------------

        if (
            product_count
            <
            request_page_size
        ):

            break

        offset += (
            request_page_size
        )

        time.sleep(
            0.3
        )

    # ========================================================
    # 聚合
    # ========================================================

    agg = (
        aggregate_playroad_rows(
            all_rows,
            source_name
        )
    )

    if agg.empty:

        print(
            f"ℹ️ [{shop_name}] {source_name} 当前没有推广商品，本次按 0 处理"
        )

        return empty_playroad_df(
            source_name
        )

    total_charge = float(
        clean_numeric(
            agg[
                f"{source_name}消耗"
            ]
        ).sum()
    )

    active_count = int(
        (
            clean_numeric(
                agg[
                    f"{source_name}消耗"
                ]
            )
            >
            0
        ).sum()
    )

    print(
        f"✅ [{shop_name}] {source_name}："
        f"{len(agg)} 个商品，"
        f"有消耗 {active_count} 个，"
        f"总消耗 ¥{total_charge:.2f}"
    )

    # --------------------------------------------------------
    # CMD 显示消耗 TOP5
    # --------------------------------------------------------

    if total_charge > 0:

        top = (
            agg
            .sort_values(
                f"{source_name}消耗",
                ascending=False
            )
            .head(5)
        )

        print(
            f"[{shop_name}] {source_name} 消耗TOP5："
        )

        for _, row in top.iterrows():

            print(
                "   "
                f"{row['商品ID']}  "
                f"¥{float(row[f'{source_name}消耗']):.2f}  "
                f"{str(row.get(f'{source_name}商品名称', ''))[:28]}"
            )

    return agg


# ============================================================
# 推广抓取安全包装
#
# 任何一个推广模块：
# - 没开
# - 暂时无数据
# - 页面没触发接口
#
# 都不能导致整个店铺失败
# ============================================================

def crawl_playroad_optional(
    page,
    url,
    expected_bizcode,
    source_name,
    shop_name
):

    try:

        return crawl_playroad(
            page,
            url,
            expected_bizcode,
            source_name,
            shop_name
        )

    except Exception as e:

        print(
            f"⚠️ [{shop_name}] {source_name} 本次未能抓取：{e}"
        )

        print(
            f"ℹ️ [{shop_name}] {source_name} 本次按 0 处理，继续后续流程"
        )

        return empty_playroad_df(
            source_name
        )


# ============================================================
# 成本配置
# ============================================================

def cost_file_for_shop(
    shop
):

    return (
        COST_ROOT
        /
        f"{shop['safe_name']}.csv"
    )


def load_cost_config(
    shop,
    business_df
):

    cost_file = (
        cost_file_for_shop(
            shop
        )
    )

    base = (
        business_df[
            [
                "商品ID",
                "商品名称",
            ]
        ]
        .copy()
        .rename(
            columns={
                "商品名称":
                    "商品名称参考"
            }
        )
    )

    if cost_file.exists():

        try:

            old = pd.read_csv(
                cost_file,
                dtype={
                    "商品ID":
                        str
                }
            )

        except Exception as e:

            print(
                f"⚠️ [{shop['name']}] 成本表读取失败：{e}"
            )

            old = pd.DataFrame()

    else:

        old = pd.DataFrame()

    if not old.empty:

        if (
            "商品ID"
            not in old.columns
        ):

            print(
                f"⚠️ [{shop['name']}] 成本配置缺少商品ID列，重新生成"
            )

            old = pd.DataFrame()

    if not old.empty:

        if (
            "商品名称参考"
            in old.columns
        ):

            old = old.drop(
                columns=[
                    "商品名称参考"
                ]
            )

        old[
            "商品ID"
        ] = (
            old[
                "商品ID"
            ]
            .astype(str)
            .str.strip()
        )

        old = (
            old
            .drop_duplicates(
                subset=[
                    "商品ID"
                ],
                keep="last"
            )
        )

        cost_df = base.merge(
            old,
            on="商品ID",
            how="left"
        )

    else:

        cost_df = base.copy()

    cost_cols = [
        "商品成本",
        "单件快递费",
        "平台扣点",
        "税点",
        "其他成本",
    ]

    for col in cost_cols:

        if col not in cost_df.columns:

            default_value = (
                0.05
                if col in [
                    "平台扣点",
                    "税点",
                ]
                else 0
            )

            cost_df[
                col
            ] = default_value

        cost_df[
            col
        ] = clean_numeric(
            cost_df[
                col
            ]
        )

    for rate_col in [
        "平台扣点",
        "税点",
    ]:

        cost_df.loc[
            cost_df[
                rate_col
            ]
            <=
            0,
            rate_col
        ] = 0.05

    # --------------------------------------------------------
    # 每次抓取都会自动把新商品加入成本表
    # 已填好的旧成本继续保留
    # --------------------------------------------------------

    cost_df[
        [
            "商品ID",
            "商品名称参考",
        ]
        +
        cost_cols
    ].to_csv(
        cost_file,
        index=False,
        encoding="utf-8-sig"
    )

    return cost_df[
        [
            "商品ID"
        ]
        +
        cost_cols
    ]
# ============================================================
# 合并 + 盈亏计算
# ============================================================

def merge_and_calculate(
    shop,
    business_df,
    site_df,
    search_df
):

    result = (
        business_df
        .merge(
            site_df,
            on="商品ID",
            how="left"
        )
        .merge(
            search_df,
            on="商品ID",
            how="left"
        )
    )

    # --------------------------------------------------------
    # 经营 + 推广数值字段
    # --------------------------------------------------------

    numeric_cols = [
        "支付件数",
        "支付金额",
        "支付买家数",
        "商品访客",
        "支付转化率",
        "加购件数",

        "全站推广消耗",
        "全站推广成交金额",
        "全站推广点击",
        "全站推广ROI",

        "关键词推广消耗",
        "关键词推广成交金额",
        "关键词推广点击",
        "关键词推广ROI",
    ]

    for col in numeric_cols:

        if col not in result.columns:
            result[col] = 0

        result[col] = clean_numeric(
            result[col]
        )

    # --------------------------------------------------------
    # 总推广消耗
    # --------------------------------------------------------

    result["总推广消耗"] = (
        result["全站推广消耗"]
        +
        result["关键词推广消耗"]
    )

    roi_weighted_total = (
        result["全站推广ROI"]
        *
        result["全站推广消耗"]
        +
        result["关键词推广ROI"]
        *
        result["关键词推广消耗"]
    )

    result["推广后台ROI"] = np.where(
        result["总推广消耗"] > 0,
        roi_weighted_total
        /
        result["总推广消耗"],
        0.0
    )

    # --------------------------------------------------------
    # 推广状态
    # --------------------------------------------------------

    result["是否有全站推广"] = np.where(
        result["全站推广消耗"] > 0,
        "是",
        "否"
    )

    result["是否有关键词推广"] = np.where(
        result["关键词推广消耗"] > 0,
        "是",
        "否"
    )

    # --------------------------------------------------------
    # 成本配置
    # --------------------------------------------------------

    cost_df = load_cost_config(
        shop,
        business_df
    )

    result = result.merge(
        cost_df,
        on="商品ID",
        how="left"
    )

    cost_cols = [
        "商品成本",
        "单件快递费",
        "平台扣点",
        "税点",
        "其他成本",
    ]

    for col in cost_cols:

        if col not in result.columns:
            result[col] = 0

        result[col] = clean_numeric(
            result[col]
        )

    # --------------------------------------------------------
    # 成本配置状态
    # --------------------------------------------------------

    result["成本配置状态"] = np.where(
        (
            result["商品成本"]
            +
            result["单件快递费"]
            +
            result["平台扣点"]
            +
            result["税点"]
            +
            result["其他成本"]
        )
        >
        0,
        "已配置",
        "未配置"
    )

    # --------------------------------------------------------
    # 成本计算
    # --------------------------------------------------------

    result["货品成本"] = (
        result["支付件数"]
        *
        result["商品成本"]
    )

    result["快递成本"] = (
        result["支付件数"]
        *
        result["单件快递费"]
    )

    result["平台费用"] = (
        result["支付金额"]
        *
        result["平台扣点"]
    )

    result["税费"] = (
        result["支付金额"]
        *
        result["税点"]
    )

    # --------------------------------------------------------
    # 销售毛利
    # --------------------------------------------------------

    result["销售毛利"] = (
        result["支付金额"]
        -
        result["货品成本"]
        -
        result["快递成本"]
        -
        result["平台费用"]
        -
        result["税费"]
    )

    # --------------------------------------------------------
    # 实时盈亏
    # --------------------------------------------------------

    result["实时盈亏"] = (
        result["销售毛利"]
        -
        result["总推广消耗"]
        -
        result["其他成本"]
    )

    # --------------------------------------------------------
    # 实际净投产
    # --------------------------------------------------------

    sales = (
        clean_numeric(
            result["支付金额"]
        )
        .to_numpy(
            dtype=float
        )
    )

    charge = (
        clean_numeric(
            result["总推广消耗"]
        )
        .to_numpy(
            dtype=float
        )
    )

    actual_roi = np.zeros(
        len(result),
        dtype="float64"
    )

    valid = (
        charge > 0
    )

    actual_roi[
        valid
    ] = (
        sales[
            valid
        ]
        /
        charge[
            valid
        ]
    )

    actual_roi = np.nan_to_num(
        actual_roi,
        nan=0.0,
        posinf=0.0,
        neginf=0.0
    )

    result["实际净投产"] = (
        actual_roi
    )

    # --------------------------------------------------------
    # 盈亏状态
    # --------------------------------------------------------

    result["盈亏状态"] = np.where(
        result["实时盈亏"] > 0,
        "盈利",
        np.where(
            result["实时盈亏"] < 0,
            "亏损",
            "持平"
        )
    )

    # --------------------------------------------------------
    # 店铺字段
    # --------------------------------------------------------

    result.insert(
        0,
        "店铺",
        shop["name"]
    )

    # --------------------------------------------------------
    # 金额 / 指标格式
    # --------------------------------------------------------

    money_cols = [
        "支付金额",

        "全站推广消耗",
        "全站推广成交金额",

        "关键词推广消耗",
        "关键词推广成交金额",

        "总推广消耗",

        "商品成本",
        "单件快递费",
        "其他成本",

        "货品成本",
        "快递成本",
        "平台费用",
        "税费",

        "销售毛利",
        "实时盈亏",

        "实际净投产",
        "推广后台ROI",
    ]

    for col in money_cols:

        if col in result.columns:

            result[col] = (
                clean_numeric(
                    result[col]
                )
                .round(2)
            )

    # --------------------------------------------------------
    # 主字段排序
    # --------------------------------------------------------

    preferred_cols = [
        "店铺",

        "商品ID",
        "商品名称",
        "商品货号",

        "支付件数",
        "支付金额",

        "全站推广消耗",
        "关键词推广消耗",
        "总推广消耗",

        "是否有全站推广",
        "是否有关键词推广",

        "商品成本",
        "单件快递费",
        "平台扣点",
        "税点",
        "其他成本",

        "成本配置状态",

        "货品成本",
        "快递成本",
        "平台费用",
        "税费",

        "销售毛利",
        "实时盈亏",
        "盈亏状态",
        "实际净投产",
        "推广后台ROI",

        "支付买家数",
        "商品访客",
        "支付转化率",
        "加购件数",

        "全站推广成交金额",
        "全站推广点击",
        "全站推广ROI",

        "关键词推广成交金额",
        "关键词推广点击",
        "关键词推广ROI",
    ]

    available_cols = [
        col
        for col in preferred_cols
        if col in result.columns
    ]

    other_cols = [
        col
        for col in result.columns
        if col not in available_cols
    ]

    result = result[
        available_cols
        +
        other_cols
    ]

    return result


# ============================================================
# 保存单店
# ============================================================

def save_shop(
    shop,
    df
):

    shop_dir = (
        DATA_ROOT
        /
        shop["safe_name"]
    )

    shop_dir.mkdir(
        exist_ok=True
    )

    now = datetime.now()

    snapshot = now.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    file_time = now.strftime(
        "%Y%m%d_%H%M%S"
    )

    if (
        "抓取时间"
        in df.columns
    ):

        df["抓取时间"] = snapshot

    else:

        df.insert(
            1,
            "抓取时间",
            snapshot
        )

    history_file = (
        shop_dir
        /
        f"商品实时数据_{file_time}.csv"
    )

    latest_file = (
        shop_dir
        /
        "latest.csv"
    )

    df.to_csv(
        history_file,
        index=False,
        encoding="utf-8-sig"
    )

    df.to_csv(
        latest_file,
        index=False,
        encoding="utf-8-sig"
    )

    print(
        f"[{shop['name']}] 最新数据：{latest_file}"
    )

    return df


# ============================================================
# 错误日志
# ============================================================

def save_error(
    shop_name,
    text
):

    log_file = (
        LOG_ROOT
        /
        "errors.log"
    )

    with open(
        log_file,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            "\n"
            +
            "=" * 70
            +
            "\n"
        )

        f.write(
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        f.write(
            f" | {shop_name}\n"
        )

        f.write(
            text
            +
            "\n"
        )


# ============================================================
# 单店运行
# ============================================================

def run_shop(
    playwright,
    shop
):

    name = (
        shop["name"]
    )

    port = (
        shop["port"]
    )

    print()
    print(
        "=" * 70
    )

    print(
        f"开始抓取：{name}"
    )

    print(
        f"CDP：127.0.0.1:{port}"
    )

    print(
        "=" * 70
    )

    try:

        browser = (
            playwright
            .chromium
            .connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        )

    except Exception as e:

        raise RuntimeError(
            f"无法连接 {name} 的 Chrome 端口 {port}：{e}"
        )

    contexts = (
        browser.contexts
    )

    if not contexts:

        raise RuntimeError(
            "没有 BrowserContext"
        )

    context = (
        contexts[0]
    )

    page = (
        context.new_page()
    )

    page.set_default_timeout(
        PAGE_TIMEOUT
    )

    try:

        # ====================================================
        # 1. 生意参谋
        # ====================================================

        business_df = (
            crawl_sycm(
                page,
                shop["sycm_url"],
                name
            )
        )

        # ====================================================
        # 2. 全站推广
        #
        # 当前没有也不会让店铺失败
        # ====================================================

        site_df = (
            crawl_playroad_optional(
                page,
                shop.get(
                    "site_url",
                    ""
                ),
                "onebpSite",
                "全站推广",
                name
            )
        )

        # ====================================================
        # 3. 关键词推广
        #
        # 咖时光目前没开：
        # 自动返回0
        #
        # 以后开了：
        # 页面出现 onebpSearch 后自动抓
        # ====================================================

        search_df = (
            crawl_playroad_optional(
                page,
                shop.get(
                    "search_url",
                    ""
                ),
                "onebpSearch",
                "关键词推广",
                name
            )
        )

        # ====================================================
        # 4. 合并计算
        # ====================================================

        result = (
            merge_and_calculate(
                shop,
                business_df,
                site_df,
                search_df
            )
        )

        # ====================================================
        # 5. 保存
        # ====================================================

        result = save_shop(
            shop,
            result
        )

        # ====================================================
        # 单店汇总
        # ====================================================

        total_sales = float(
            clean_numeric(
                result[
                    "支付金额"
                ]
            ).sum()
        )

        total_site = float(
            clean_numeric(
                result[
                    "全站推广消耗"
                ]
            ).sum()
        )

        total_search = float(
            clean_numeric(
                result[
                    "关键词推广消耗"
                ]
            ).sum()
        )

        total_charge = float(
            clean_numeric(
                result[
                    "总推广消耗"
                ]
            ).sum()
        )

        total_profit = float(
            clean_numeric(
                result[
                    "实时盈亏"
                ]
            ).sum()
        )

        configured_count = int(
            (
                result[
                    "成本配置状态"
                ]
                ==
                "已配置"
            )
            .sum()
        )

        print()
        print(
            f"✅ {name} 完成"
        )

        print(
            f"经营商品：{len(result)}"
        )

        print(
            f"已配置成本：{configured_count}"
        )

        print(
            f"支付金额：¥{total_sales:.2f}"
        )

        print(
            f"全站推广：¥{total_site:.2f}"
        )

        print(
            f"关键词推广：¥{total_search:.2f}"
        )

        print(
            f"总推广消耗：¥{total_charge:.2f}"
        )

        print(
            f"实时盈亏：¥{total_profit:.2f}"
        )

        return result

    finally:

        try:

            page.close()

        except Exception:
            pass


# ============================================================
# 保存所有店铺总表
# ============================================================

def save_all_shops(
    frames
):

    if not frames:

        return None

    df = pd.concat(
        frames,
        ignore_index=True
    )

    now = datetime.now()

    latest = (
        DATA_ROOT
        /
        "all_shops_latest.csv"
    )

    history = (
        DATA_ROOT
        /
        (
            "all_shops_"
            +
            now.strftime(
                "%Y%m%d_%H%M%S"
            )
            +
            ".csv"
        )
    )

    df.to_csv(
        latest,
        index=False,
        encoding="utf-8-sig"
    )

    df.to_csv(
        history,
        index=False,
        encoding="utf-8-sig"
    )

    return df


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print(
        "=" * 70
    )

    print(
        f"千牛多店铺实时盈亏抓取 {VERSION}"
    )

    print(
        "经营 + 全站推广 + 关键词推广"
    )

    print(
        "无人工确认模式"
    )

    print(
        "=" * 70
    )

    shops = load_shops()

    print(
        f"\n启用店铺：{len(shops)}"
    )

    if not shops:

        print(
            "❌ shops.json 中没有可运行店铺"
        )

        return

    frames = []

    success = 0

    failed = 0

    with sync_playwright() as p:

        for shop in shops:

            try:

                df = run_shop(
                    p,
                    shop
                )

                frames.append(
                    df
                )

                success += 1

            except Exception as e:

                failed += 1

                print()
                print(
                    f"❌ {shop['name']} 抓取失败"
                )

                print(
                    str(e)
                )

                save_error(
                    shop["name"],
                    traceback.format_exc()
                )

                # 一家失败不能影响下一家
                continue

    combined = (
        save_all_shops(
            frames
        )
    )

    print()
    print(
        "=" * 70
    )

    print(
        f"{VERSION} 全部执行完成"
    )

    print(
        "=" * 70
    )

    print(
        f"成功店铺：{success}"
    )

    print(
        f"失败店铺：{failed}"
    )

    if combined is not None:

        total_sales = float(
            clean_numeric(
                combined[
                    "支付金额"
                ]
            ).sum()
        )

        total_site = float(
            clean_numeric(
                combined[
                    "全站推广消耗"
                ]
            ).sum()
        )

        total_search = float(
            clean_numeric(
                combined[
                    "关键词推广消耗"
                ]
            ).sum()
        )

        total_charge = float(
            clean_numeric(
                combined[
                    "总推广消耗"
                ]
            ).sum()
        )

        total_profit = float(
            clean_numeric(
                combined[
                    "实时盈亏"
                ]
            ).sum()
        )

        print(
            f"总商品记录：{len(combined)}"
        )

        print(
            f"全部支付金额：¥{total_sales:.2f}"
        )

        print(
            f"全站推广消耗：¥{total_site:.2f}"
        )

        print(
            f"关键词推广消耗：¥{total_search:.2f}"
        )

        print(
            f"全部推广消耗：¥{total_charge:.2f}"
        )

        print(
            f"全部实时盈亏：¥{total_profit:.2f}"
        )

        print()
        print(
            "总表："
        )

        print(
            DATA_ROOT
            /
            "all_shops_latest.csv"
        )

    if failed:

        print()
        print(
            "失败日志："
        )

        print(
            LOG_ROOT
            /
            "errors.log"
        )


if __name__ == "__main__":

    main()

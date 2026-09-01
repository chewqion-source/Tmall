# -*- coding: utf-8 -*-
"""
千牛多店铺实时盈亏 V4.9
一键整合版：多店铺 SKU 真实成本 + 稳定版实时盈亏 + 多店汇总

运行顺序：
1. 调用当前目录 order_sku_crawler.py
   - 三家店铺订单SKU抓取
   - 商品ID解析
   - SKU单件货价匹配
   - 订单级快递费计算
   - 生成每家 product_cost_summary_YYYYMMDD.csv

2. 调用当前目录 qianniu_profit_crawler.py
   - 保留你已经验证正确的生意参谋 / 推广抓取逻辑
   - 生成每家 data/<店铺>/latest.csv

3. 本程序把 SKU 真实成本合并回每家 latest.csv
   并重新计算：
   - 货品成本
   - 快递成本
   - 平台费用
   - 税费
   - 销售毛利
   - 实时盈亏
   - 利润率
   - 盈亏状态
   - 实际净投产

4. 重建 data/all_shops_latest.csv

说明：
- 退款已接入：调用 refund_crawler_v3_6.py，按当天“退款成功”金额扣减实时盈亏。
- 如果某商品 SKU 成本仍存在未匹配行，不会用不完整成本覆盖原成本。
"""

from pathlib import Path
from datetime import datetime
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request

import numpy as np
import pandas as pd

from fee_config_utils import fee_rates_for_store


VERSION = "V5.5"

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
SHOPS_FILE = BASE_DIR / "shops.json"
FEE_CONFIG_FILE = CONFIG_DIR / "fee_config.xlsx"

GUOHUO_SHOP_NAME = "国货严选"
GUOHUO_MARKETING_EXEMPT_PRODUCT_IDS = {
    "952900248402",
    "949587977970",
    "954859088828",
    "992853929359",
    "1058126529708",
    "991021966779",
    "977855300916",
}
SKU_SCRIPT = BASE_DIR / "order_sku_crawler_v2_5_4.py"
PROFIT_SCRIPT = BASE_DIR / "qianniu_profit_crawler.py"
REFUND_SCRIPT = BASE_DIR / "refund_crawler_v3_6_2.py"
GUOHUO_SCRIPT = BASE_DIR / "guohuo_yanxuan_crawler.py"
DOUYIN_SHOP_NAME = "盲盒抖店"
DOUYIN_SCRIPT = BASE_DIR / "douyin_profit_crawler.py"
XIAOHONGSHU_SHOP_NAME = "盲盒千帆"
XIAOHONGSHU_SCRIPT = BASE_DIR / "xiaohongshu_profit_crawler.py"


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
        return ""

    value = str(value).strip()

    if value.endswith(".0"):
        value = value[:-2]

    return value


def clean_numeric(value, default=0.0):
    if isinstance(value, pd.Series):
        s = pd.to_numeric(
            value,
            errors="coerce"
        )

        s = s.replace(
            [np.inf, -np.inf],
            np.nan
        )

        return (
            s
            .fillna(float(default))
            .astype("float64")
        )

    try:
        x = float(value)

        if np.isfinite(x):
            return x

    except Exception:
        pass

    return float(default)


def load_enabled_shops():
    if not SHOPS_FILE.exists():
        raise RuntimeError(
            f"找不到 shops.json：{SHOPS_FILE}"
        )

    with open(
        SHOPS_FILE,
        "r",
        encoding="utf-8-sig"
    ) as f:
        config = json.load(f)

    only_shop = str(os.environ.get("TMALL_ONLY_SHOP", "")).strip()
    shops = []

    for item in config.get("shops", []):

        if not item.get(
            "enabled",
            True
        ):
            continue

        name = str(
            item.get(
                "name",
                ""
            )
        ).strip()

        if not name:
            continue

        if only_shop and name != only_shop:
            continue

        shops.append({
            "name":
                name,

            "safe_name":
                safe_filename(
                    name
                ),

            "platform":
                str(item.get("platform", "")).strip().lower(),

            "port":
                item.get("port"),

            "profile":
                item.get("profile"),

            "sycm_url":
                item.get("sycm_url"),

            "site_url":
                item.get("site_url"),

            "search_url":
                item.get("search_url"),

            "smart_site_url":
                item.get("smart_site_url"),

            "order_url":
                item.get("order_url"),
        })

    return shops


def is_guohuo_shop(name):
    return str(name or "").strip() == GUOHUO_SHOP_NAME


def is_douyin_shop(shop):
    return (
        str(shop.get("platform", "")).strip().lower() == "douyin"
        or
        str(shop.get("name", "")).strip() == DOUYIN_SHOP_NAME
    )


def is_xiaohongshu_shop(shop):
    return (
        str(shop.get("platform", "")).strip().lower() == "xiaohongshu"
        or
        str(shop.get("name", "")).strip() == XIAOHONGSHU_SHOP_NAME
    )


def load_qianniu_refund_shops():
    return [
        shop
        for shop in load_enabled_shops()
        if not is_guohuo_shop(shop.get("name"))
        and
        not is_douyin_shop(shop)
        and
        not is_xiaohongshu_shop(shop)
    ]


def shop_name(shop):
    return str(shop.get("name", "")).strip()


def shop_port(shop):
    try:
        return int(shop.get("port"))
    except Exception:
        return 0


def platform_rate_for_shop(name):
    return fee_rates_for_store(FEE_CONFIG_FILE, str(name or "").strip())["platform_rate"]


def tax_rate_for_shop(name):
    return fee_rates_for_store(FEE_CONFIG_FILE, str(name or "").strip())["tax_rate"]


def marketing_rate_for_shop(name):
    return fee_rates_for_store(FEE_CONFIG_FILE, str(name or "").strip())["marketing_rate"]


def with_only_shop(name, func):
    previous = os.environ.get("TMALL_ONLY_SHOP")
    os.environ["TMALL_ONLY_SHOP"] = name
    try:
        return func()
    finally:
        if previous is None:
            os.environ.pop("TMALL_ONLY_SHOP", None)
        else:
            os.environ["TMALL_ONLY_SHOP"] = previous


def chrome_pids_for_port(port):
    if not port:
        return []
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'chrome.exe' "
            f"-and $_.CommandLine -match 'remote-debugging-port={port}(\\s|$)' }} | "
            "ForEach-Object { $_.ProcessId }"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return []
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def close_shop_browser(shop, reason=""):
    port = shop_port(shop)
    pids = chrome_pids_for_port(port)
    if not pids:
        return
    for pid in pids:
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass
    label = f"（{reason}）" if reason else ""
    print(f"✓ 已关闭 {shop_name(shop)} Chrome 端口 {port}{label}")


def close_managed_browsers(shops):
    for shop in shops:
        close_shop_browser(shop, "清理旧会话")


def wait_for_cdp(port, timeout_seconds=35):
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def launch_shop_browser(shop):
    from shop_manager import launch_shop_chrome

    port = shop_port(shop)
    close_shop_browser(shop, "启动前清理")
    ok = launch_shop_chrome(shop)
    if not ok:
        raise RuntimeError(f"{shop_name(shop)} Chrome 启动失败")
    if not wait_for_cdp(port):
        raise RuntimeError(f"{shop_name(shop)} Chrome 端口 {port} 启动后不可连接")
    time.sleep(8)
    print(f"✓ {shop_name(shop)} Chrome 端口 {port} 已就绪")


def import_module_from_file(
    module_name,
    file_path
):
    if not file_path.exists():
        raise RuntimeError(
            f"找不到文件：{file_path}"
        )

    spec = (
        importlib.util
        .spec_from_file_location(
            module_name,
            file_path
        )
    )

    if (
        spec is None
        or
        spec.loader is None
    ):
        raise RuntimeError(
            f"无法载入：{file_path}"
        )

    module = (
        importlib.util
        .module_from_spec(
            spec
        )
    )

    sys.modules[
        module_name
    ] = module

    spec.loader.exec_module(
        module
    )

    return module


# ============================================================
# 阶段1：运行多店铺 SKU 成本抓取器
# ============================================================

def run_sku_crawler():
    global SKU_SUCCESS_SHOPS

    print()
    print("=" * 76)
    print("阶段 1 / 4：抓取启用店铺 SKU 真实成本")
    print("=" * 76)

    module = import_module_from_file(
        "_qianniu_sku_runtime",
        SKU_SCRIPT
    )

    if not hasattr(module, "main"):
        raise RuntimeError(
            f"{SKU_SCRIPT.name} 中找不到 main()"
        )

    main_func = module.main

    if asyncio.iscoroutinefunction(main_func):
        result = asyncio.run(
            main_func()
        )
    else:
        result = main_func()

    result = result or {}

    SKU_SUCCESS_SHOPS.update(
        result.get(
            "success_shops",
            []
        )
    )

    print()
    print(
        "本轮 SKU 成功店铺："
        +
        (
            "、".join(sorted(SKU_SUCCESS_SHOPS))
            if SKU_SUCCESS_SHOPS
            else "0家"
        )
    )

    print("✓ SKU 成本阶段执行完成")


# ============================================================
# 阶段2：运行当前稳定版实时盈亏抓取器
# ============================================================

def run_profit_crawler():
    print()
    print("=" * 76)
    print("阶段 2 / 4：运行当前稳定版实时盈亏抓取")
    print("=" * 76)

    module = import_module_from_file(
        "_qianniu_profit_runtime",
        PROFIT_SCRIPT
    )

    if not hasattr(
        module,
        "main"
    ):
        raise RuntimeError(
            "qianniu_profit_crawler.py 中找不到 main()"
        )

    module.main()

    print()
    print("✓ 实时盈亏抓取阶段执行完成")



# ============================================================
# 阶段3：运行稳定版退款抓取器 V3.6
# ============================================================

REFUND_RUN_STARTED_AT = None
SKU_SUCCESS_SHOPS = set()
REFUND_RESULT_MAP = {}

def run_refund_crawler():
    global REFUND_RUN_STARTED_AT
    global REFUND_RESULT_MAP

    print()
    print("=" * 76)
    print("阶段 3 / 4：抓取普通千牛店当天【退款成功】金额")
    print("=" * 76)

    if not REFUND_SCRIPT.exists():
        raise RuntimeError(
            f"找不到退款抓取器：{REFUND_SCRIPT}"
        )

    REFUND_RUN_STARTED_AT = datetime.now().timestamp()

    module = import_module_from_file(
        "_qianniu_refund_runtime",
        REFUND_SCRIPT
    )

    if not hasattr(module, "main"):
        raise RuntimeError(
            f"{REFUND_SCRIPT.name} 中找不到 main()"
        )

    main_func = module.main

    try:
        if asyncio.iscoroutinefunction(main_func):
            output = asyncio.run(
                main_func()
            )
        else:
            output = main_func()
    except Exception as exc:
        print()
        print(f"⚠ 退款抓取器末尾异常，尝试使用已落盘退款文件继续：{exc}")
        print(traceback.format_exc())
        output = {}

    output = output or {}
    results = output.get("results", [])

    current_results = {
        str(item.get("shop", "")).strip():
        {
            "amount": float(item.get("amount", 0) or 0),
            "rows": int(item.get("rows", 0) or 0),
        }
        for item in results
        if str(item.get("shop", "")).strip()
    }

    day = datetime.now().strftime("%Y%m%d")
    for shop in load_qianniu_refund_shops():
        name = shop_name(shop)
        if not name or name in current_results:
            continue
        summary_file = DATA_ROOT / safe_filename(name) / f"refund_summary_{day}.csv"
        if not summary_file.exists():
            continue
        if REFUND_RUN_STARTED_AT and summary_file.stat().st_mtime < REFUND_RUN_STARTED_AT:
            continue
        try:
            summary_df = pd.read_csv(summary_file, encoding="utf-8-sig")
            amount_col = "退款金额" if "退款金额" in summary_df.columns else None
            amount = float(clean_numeric(summary_df[amount_col]).sum()) if amount_col else 0.0
            current_results[name] = {
                "amount": amount,
                "rows": int(len(summary_df)),
            }
            print(f"   ↳ 已从退款文件恢复 {name}：{len(summary_df)} 单 / ¥{amount:.2f}")
        except Exception as file_exc:
            print(f"   ⚠ 读取退款文件失败 {name}：{file_exc}")

    REFUND_RESULT_MAP.update(current_results)

    print()
    print("本轮退款结果确认：")

    for shop in load_qianniu_refund_shops():
        name = shop["name"]
        info = REFUND_RESULT_MAP.get(name)

        if info is None:
            print(
                f"   × {name}：本轮未成功，不读取历史退款"
            )
        else:
            print(
                f"   ✓ {name}：{info['rows']} 单 / ¥{info['amount']:.2f}"
            )

    print()
    print("✓ 退款抓取阶段执行完成")


def run_guohuo_yanxuan_crawler():
    global SKU_SUCCESS_SHOPS

    print()
    print("=" * 76)
    print("阶段 3.5 / 4：抓取国货严选实时数据 + 投流托管 + SKU")
    print("=" * 76)

    if not GUOHUO_SCRIPT.exists():
        raise RuntimeError(
            f"找不到国货严选抓取器：{GUOHUO_SCRIPT}"
        )

    module = import_module_from_file(
        "_guohuo_yanxuan_runtime",
        GUOHUO_SCRIPT
    )

    if not hasattr(module, "main"):
        raise RuntimeError(
            f"{GUOHUO_SCRIPT.name} 中找不到 main()"
        )

    output = module.main() or {}

    if not output.get("success"):
        raise RuntimeError(
            "国货严选抓取未成功"
        )

    SKU_SUCCESS_SHOPS.add(
        GUOHUO_SHOP_NAME
    )

    print("✓ 国货严选抓取阶段执行完成")


def run_douyin_crawler():
    print()
    print("=" * 76)
    print("阶段 3.6 / 4：抓取盲盒抖店订单 + 当日退款成功 + 推广消耗")
    print("=" * 76)

    if not DOUYIN_SCRIPT.exists():
        raise RuntimeError(
            f"找不到抖店抓取器：{DOUYIN_SCRIPT}"
        )

    douyin_shops = [
        shop
        for shop in load_enabled_shops()
        if is_douyin_shop(shop)
    ]

    if not douyin_shops:
        print("ℹ️ 未启用抖店，跳过")
        return

    shop = douyin_shops[0]
    port = int(shop.get("port") or 9226)

    module = import_module_from_file(
        "_douyin_profit_runtime",
        DOUYIN_SCRIPT
    )

    if not hasattr(module, "run"):
        raise RuntimeError(
            f"{DOUYIN_SCRIPT.name} 中找不到 run()"
        )

    result = module.run(port=port)

    if result is None or result.empty:
        raise RuntimeError("盲盒抖店抓取未成功")

    SKU_SUCCESS_SHOPS.add(DOUYIN_SHOP_NAME)
    print("✓ 盲盒抖店抓取阶段执行完成")


def run_xiaohongshu_crawler():
    print()
    print("=" * 76)
    print("阶段 3.7 / 4：抓取盲盒千帆订单 + 当日退款成功 + 千帆推广消耗")
    print("=" * 76)

    if not XIAOHONGSHU_SCRIPT.exists():
        raise RuntimeError(
            f"找不到小红书抓取器：{XIAOHONGSHU_SCRIPT}"
        )

    xhs_shops = [
        shop
        for shop in load_enabled_shops()
        if is_xiaohongshu_shop(shop)
    ]

    if not xhs_shops:
        print("ℹ️ 未启用小红书店铺，跳过")
        return

    success = 0
    for shop in xhs_shops:
        name = shop_name(shop)
        port = int(shop.get("port") or 9227)
        command = [
            sys.executable,
            str(XIAOHONGSHU_SCRIPT),
            "--port",
            str(port),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                timeout=480,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{name} 小红书抓取超过 8 分钟，已强制跳过") from exc
        if result.returncode != 0:
            raise RuntimeError(f"{name} 抓取未成功，退出码 {result.returncode}")

        latest_file = DATA_ROOT / safe_filename(name) / "latest.csv"
        if not latest_file.exists():
            fallback_file = DATA_ROOT / safe_filename(XIAOHONGSHU_SHOP_NAME) / "latest.csv"
            if fallback_file.exists() and fallback_file != latest_file:
                latest_file.parent.mkdir(parents=True, exist_ok=True)
                latest_file.write_bytes(fallback_file.read_bytes())
            else:
                raise RuntimeError(f"{name} 抓取未生成 latest.csv")
        SKU_SUCCESS_SHOPS.add(name)
        success += 1

    print(f"✓ 小红书店铺抓取阶段执行完成：{success} 家")


def integrate_external_platform_shop(shop, default_shop_name, platform_label):
    shop_display_name = shop_name(shop) or default_shop_name
    shop_dir = DATA_ROOT / safe_filename(shop_display_name)
    latest_file = shop_dir / "latest.csv"
    summary_file = shop_dir / "latest_summary.json"

    if not latest_file.exists():
        print(f"⚠ [{shop_display_name}] 没有 latest.csv，跳过")
        return None

    df = pd.read_csv(
        latest_file,
        dtype={"商品ID": str},
        encoding="utf-8-sig"
    )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store_ad_cost = 0.0
    ad_balance = None
    overall_profit = None
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            generated_at = str(summary.get("generated_at") or generated_at)
            store_ad_cost = float(summary.get("store_ad_cost", 0) or 0)
            if summary.get("ad_balance") is not None:
                ad_balance = float(summary.get("ad_balance") or 0)
            overall_profit = float(summary.get("overall_profit", 0) or 0)
        except Exception:
            pass

    df["店铺"] = shop_display_name
    df["抓取时间"] = generated_at
    df["商品货号"] = df.get("商家编码", "")
    df["支付件数"] = clean_numeric(df.get("SKU成交件数", 0))
    df["店铺级推广消耗"] = store_ad_cost
    df["店铺整体盈亏"] = overall_profit if overall_profit is not None else clean_numeric(df.get("实时盈亏", 0)).sum() - store_ad_cost
    df["账户推广余额"] = ad_balance

    if "成本匹配状态" in df.columns:
        df["SKU成本状态"] = np.where(
            df["成本匹配状态"].astype(str).eq("已匹配"),
            "完整",
            "存在未匹配SKU"
        )
    else:
        df["SKU成本状态"] = "无SKU订单成本"

    df["SKU成本来源"] = f"{platform_label}订单SKU成本"
    df["SKU成本未匹配行数"] = np.where(
        df["SKU成本状态"].eq("完整"),
        0,
        1
    )
    df["销售毛利"] = (
        clean_numeric(df.get("支付金额", 0))
        -
        clean_numeric(df.get("货品成本", 0))
        -
        clean_numeric(df.get("快递成本", 0))
        -
        clean_numeric(df.get("平台费用", 0))
        -
        clean_numeric(df.get("税费", 0))
    )
    df["盈亏状态"] = np.where(
        clean_numeric(df.get("实时盈亏", 0)) > 0,
        "盈利",
        np.where(clean_numeric(df.get("实时盈亏", 0)) < 0, "亏损", "持平")
    )

    print()
    print(f"✅ {shop_display_name} 成本整合完成")
    print(f"   支付金额：¥{clean_numeric(df.get('支付金额', 0)).sum():.2f}")
    print(f"   推广消耗：¥{clean_numeric(df.get('总推广消耗', 0)).sum() + store_ad_cost:.2f}")
    store_profit = (
        float(df["店铺整体盈亏"].iloc[0])
        if not df.empty and "店铺整体盈亏" in df.columns
        else float(overall_profit or 0)
    )
    print(f"   实时盈亏：¥{store_profit:.2f}")
    if ad_balance is not None:
        print(f"   账户推广余额：¥{ad_balance:.2f}")
    print(f"   最新数据：{latest_file}")
    return df


def integrate_douyin_shop(shop):
    return integrate_external_platform_shop(
        shop,
        DOUYIN_SHOP_NAME,
        "抖店",
    )


def integrate_xiaohongshu_shop(shop):
    return integrate_external_platform_shop(
        shop,
        XIAOHONGSHU_SHOP_NAME,
        "小红书",
    )


def load_shop_refund_total(shop):
    """
    V5.1：
    优先使用退款抓取器本轮直接返回的结果。
    这样坐拥_宁静等店铺不会因为 Windows 文件时间精度/
    写入顺序问题被误判为旧退款文件。
    """
    name = shop["name"]

    if is_guohuo_shop(name):
        return (
            0.0,
            0,
            None,
            "国货严选跳过千牛退款抓取"
        )

    if name in REFUND_RESULT_MAP:
        info = REFUND_RESULT_MAP[name]

        return (
            float(info["amount"]),
            int(info["rows"]),
            None,
            "正常"
        )

    day = datetime.now().strftime(
        "%Y%m%d"
    )
    summary_file = (
        DATA_ROOT
        /
        safe_filename(name)
        /
        f"refund_summary_{day}.csv"
    )
    if summary_file.exists():
        try:
            summary_df = pd.read_csv(
                summary_file,
                encoding="utf-8-sig"
            )
            amount_col = (
                "退款金额"
                if "退款金额" in summary_df.columns
                else
                None
            )
            amount = (
                float(clean_numeric(summary_df[amount_col]).sum())
                if amount_col
                else
                0.0
            )
            rows = int(len(summary_df))
            REFUND_RESULT_MAP[name] = {
                "amount": amount,
                "rows": rows,
            }
            return (
                amount,
                rows,
                summary_file,
                "正常"
            )
        except Exception:
            return (
                0.0,
                0,
                summary_file,
                "退款汇总读取失败"
            )

    # 当天文件也不可用时，明确返回0，防止读取历史退款造成重复误扣。
    return (
        0.0,
        0,
        None,
        "本轮退款抓取失败"
    )


def load_shop_refund_by_product(shop):
    """
    按退款明细里的商品ID汇总当天退款。
    退款抓取器明细里保留了原始记录，其中 itemInfo.auctionId 是商品ID。
    """

    total, rows, _file, status = load_shop_refund_total(
        shop
    )

    day = datetime.now().strftime(
        "%Y%m%d"
    )

    path = (
        DATA_ROOT
        /
        shop["safe_name"]
        /
        f"refund_detail_{day}.csv"
    )

    empty = pd.DataFrame(
        columns=[
            "商品ID",
            "退款金额",
        ]
    )

    if (
        status != "正常"
        or
        total <= 0
        or
        not path.exists()
    ):
        return empty, total, rows, status, 0.0

    try:
        detail = pd.read_csv(
            path,
            dtype=str,
            encoding="utf-8-sig"
        )
    except Exception:
        return empty, total, rows, "退款明细读取失败", total

    if detail.empty or "退款金额" not in detail.columns:
        return empty, total, rows, status, total

    def extract_product_id(row):
        raw = row.get(
            "原始记录",
            ""
        )

        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(
                    raw
                )

                item_info = (
                    payload
                    .get("disputeBodyVO", {})
                    .get("itemInfo", {})
                )

                product_id = normalize_id(
                    item_info.get("auctionId")
                )

                if product_id:
                    return product_id

            except Exception:
                pass

        return normalize_id(
            row.get("商品ID")
        )

    detail["商品ID"] = detail.apply(
        extract_product_id,
        axis=1
    )

    detail["退款金额"] = clean_numeric(
        detail["退款金额"]
    )

    matched = detail[
        detail["商品ID"].astype(str).str.len() > 0
    ].copy()

    if matched.empty:
        return empty, total, rows, status, total

    refund_df = (
        matched
        .groupby(
            "商品ID",
            as_index=False
        )
        .agg({
            "退款金额":
                "sum",
        })
    )

    assigned_total = float(
        refund_df["退款金额"].sum()
    )

    unassigned_total = max(
        0.0,
        float(total) - assigned_total
    )

    if unassigned_total < 0.01:
        unassigned_total = 0.0

    return (
        refund_df,
        total,
        rows,
        status,
        unassigned_total
    )


# ============================================================
# 读取某店 SKU 商品成本汇总
# ============================================================

def load_product_cost_summary(
    shop
):
    day = datetime.now().strftime(
        "%Y%m%d"
    )

    path = (
        DATA_ROOT
        /
        shop["safe_name"]
        /
        f"product_cost_summary_{day}.csv"
    )

    if not path.exists():
        return None, path

    df = pd.read_csv(
        path,
        dtype={
            "商品ID":
                str
        },
        encoding="utf-8-sig"
    )

    required = [
        "商品ID",
        "货品成本",
        "分摊快递费",
        "未匹配成本行数",
    ]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"{path.name} 缺少字段："
            +
            "、".join(
                missing
            )
        )

    df[
        "商品ID"
    ] = (
        df[
            "商品ID"
        ]
        .map(
            normalize_id
        )
    )

    for col in [
        "订单数",
        "成交件数",
        "货品成本",
        "分摊快递费",
        "货品+快递总成本",
        "未匹配成本行数",
    ]:
        if col not in df.columns:
            df[col] = 0

        df[col] = clean_numeric(
            df[col]
        )

    df = (
        df
        .groupby(
            "商品ID",
            as_index=False
        )
        .agg({
            "订单数":
                "sum",

            "成交件数":
                "sum",

            "货品成本":
                "sum",

            "分摊快递费":
                "sum",

            "货品+快递总成本":
                "sum",

            "未匹配成本行数":
                "sum",
        })
    )

    df = df.rename(
        columns={
            "订单数":
                "SKU订单数",

            "成交件数":
                "SKU成交件数",

            "货品成本":
                "SKU货品成本",

            "分摊快递费":
                "SKU快递成本",

            "货品+快递总成本":
                "SKU货品快递总成本",

            "未匹配成本行数":
                "SKU成本未匹配行数",
        }
    )

    return df, path


# ============================================================
# 合并单店真实成本并重算盈亏
# ============================================================

def integrate_shop(
    shop
):
    shop_dir = (
        DATA_ROOT
        /
        shop["safe_name"]
    )

    latest_file = (
        shop_dir
        /
        "latest.csv"
    )

    if not latest_file.exists():

        print(
            f"⚠ [{shop['name']}] "
            "没有 latest.csv，跳过"
        )

        return None

    cost_file = (
        shop_dir
        /
        f"product_cost_summary_{datetime.now().strftime('%Y%m%d')}.csv"
    )

    use_sku_summary = (
        shop["name"] in SKU_SUCCESS_SHOPS
        or
        (
            cost_file.exists()
            and
            cost_file.stat().st_size > 0
        )
    )

    # 优先使用本轮 SKU 汇总；如果是补救整合/补发上传，允许使用当天已生成的汇总。
    # 仍然不读取跨日期历史文件，避免旧成本误扣到今天。
    if use_sku_summary:
        cost_df, cost_file = (
            load_product_cost_summary(
                shop
            )
        )
    else:
        cost_df = None

    df = pd.read_csv(
        latest_file,
        dtype={
            "商品ID":
                str
        },
        encoding="utf-8-sig"
    )

    if "商品ID" not in df.columns:

        raise RuntimeError(
            f"{latest_file} 缺少商品ID列"
        )

    df[
        "商品ID"
    ] = (
        df[
            "商品ID"
        ]
        .map(
            normalize_id
        )
    )

    # 允许重复运行 V4.9
    remove_cols = [
        "SKU订单数",
        "SKU成交件数",
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "SKU成本未匹配行数",
        "SKU成本状态",
        "SKU成本来源",
        "利润率",
        "退款金额",
        "退款口径",
        "退款数据状态",
    ]

    existing_remove = [
        col
        for col in remove_cols
        if col in df.columns
    ]

    if existing_remove:
        df = df.drop(
            columns=existing_remove
        )

    if cost_df is not None:
        df = df.merge(
            cost_df,
            on="商品ID",
            how="left"
        )
    else:
        print(
            f"⚠ [{shop['name']}] 本轮 SKU 抓取失败，"
            "本次不使用历史 SKU 汇总；保留实时主表原成本，但仍继续扣退款。"
        )

    for col in [
        "SKU订单数",
        "SKU成交件数",
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "SKU成本未匹配行数",
    ]:
        if col not in df.columns:
            df[col] = 0

        df[col] = clean_numeric(
            df[col]
        )

    # 有订单成本且完全匹配，才能覆盖原成本
    exact_mask = (
        (
            df[
                "SKU订单数"
            ]
            >
            0
        )
        &
        (
            df[
                "SKU成本未匹配行数"
            ]
            ==
            0
        )
    )

    partial_mask = (
        (
            df[
                "SKU订单数"
            ]
            >
            0
        )
        &
        (
            df[
                "SKU成本未匹配行数"
            ]
            >
            0
        )
    )

    df[
        "SKU成本状态"
    ] = np.where(
        exact_mask,
        "完整",
        np.where(
            partial_mask,
            "存在未匹配SKU",
            "无SKU订单成本"
        )
    )

    any_sku_mask = (
        df[
            "SKU订单数"
        ]
        >
        0
    )

    df[
        "SKU成本来源"
    ] = np.where(
        exact_mask,
        "订单SKU真实成本",
        np.where(
            partial_mask,
            "订单SKU部分成本",
            "保留原成本"
        )
    )

    # 补齐后续计算字段
    for col in [
        "货品成本",
        "快递成本",
        "平台扣点",
        "税点",
        "平台费用",
        "税费",
        "其他成本",
        "预估营销托管费用",
        "支付金额",
        "总推广消耗",
    ]:

        if col not in df.columns:
            df[col] = 0

        df[col] = clean_numeric(
            df[col]
        )

    store_platform_rate = platform_rate_for_shop(shop.get("name"))
    store_tax_rate = tax_rate_for_shop(shop.get("name"))
    store_marketing_rate = marketing_rate_for_shop(shop.get("name"))
    df[
        "平台扣点"
    ] = store_platform_rate
    df[
        "税点"
    ] = store_tax_rate

    # 使用订单 SKU 已识别到的单件货成本和快递成本。
    # 即使存在未匹配 SKU，也先计入已知成本，同时保留未匹配行数提示。
    df.loc[
        any_sku_mask,
        "货品成本"
    ] = (
        df.loc[
            any_sku_mask,
            "SKU货品成本"
        ]
    )

    df.loc[
        any_sku_mask,
        "快递成本"
    ] = (
        df.loc[
            any_sku_mask,
            "SKU快递成本"
        ]
    )

    # --------------------------------------------------------
    # 当天退款成功金额
    # --------------------------------------------------------
    # 退款明细带有原始 itemInfo.auctionId，优先按商品ID归属。
    # 这样单品盈亏不会再被整店退款挤到第一条商品上。
    refund_by_product, refund_total, refund_rows, refund_status, refund_unassigned = (
        load_shop_refund_by_product(shop)
    )

    df["退款金额"] = 0.0
    if is_guohuo_shop(shop["name"]):
        df["退款口径"] = "国货严选使用淘工厂专用实时口径；不进入千牛退款抓取"
    else:
        df["退款口径"] = "当天申请时间 + 售后状态=退款成功；按退款明细商品ID归属"
    df["退款数据状态"] = refund_status

    if not refund_by_product.empty:
        df = df.merge(
            refund_by_product.rename(
                columns={
                    "退款金额":
                        "_商品退款金额"
                }
            ),
            on="商品ID",
            how="left"
        )

        df["_商品退款金额"] = clean_numeric(
            df["_商品退款金额"]
        )

        df["退款金额"] = df["_商品退款金额"]

        df = df.drop(
            columns=[
                "_商品退款金额"
            ]
        )

    print(
        f"   当天退款成功：{refund_rows} 单 / ¥{refund_total:.2f}"
    )
    if refund_unassigned > 0:
        print(
            f"   ⚠ 未能按商品ID归属退款：¥{refund_unassigned:.2f}"
        )
    if refund_status != "正常" and refund_status not in {"本轮退款为0", "国货严选跳过千牛退款抓取"}:
        print(
            f"   ⚠ 退款数据状态：{refund_status}；本店本轮不扣历史退款"
        )

    # 保留主程序平台扣点 / 税点配置
    df[
        "平台费用"
    ] = (
        df[
            "支付金额"
        ]
        *
        df[
            "平台扣点"
        ]
    )

    df[
        "税费"
    ] = (
        df[
            "支付金额"
        ]
        *
        df[
            "税点"
        ]
    )

    df[
        "预估营销托管费用"
    ] = 0.0
    if store_marketing_rate > 0:
        exempt_product_ids = (
            GUOHUO_MARKETING_EXEMPT_PRODUCT_IDS
            if shop.get("name") == GUOHUO_SHOP_NAME
            else set()
        )
        exempt_mask = df[
            "商品ID"
        ].astype(str).str.strip().isin(
            exempt_product_ids
        )
        df[
            "预估营销托管费用"
        ] = np.where(
            exempt_mask,
            0.0,
            df[
                "支付金额"
            ]
            *
            store_marketing_rate,
        )

    # 重算毛利
    df[
        "销售毛利"
    ] = (
        df[
            "支付金额"
        ]
        -
        df[
            "货品成本"
        ]
        -
        df[
            "快递成本"
        ]
        -
        df[
            "平台费用"
        ]
        -
        df[
            "税费"
        ]
    )

    # 重算盈亏
    df[
        "实时盈亏"
    ] = (
        df[
            "销售毛利"
        ]
        -
        df[
            "总推广消耗"
        ]
        -
        df[
            "其他成本"
        ]
        -
        df[
            "预估营销托管费用"
        ]
        -
        df[
            "退款金额"
        ]
    )

    # 盈亏状态
    df[
        "盈亏状态"
    ] = np.where(
        df[
            "实时盈亏"
        ]
        >
        0,
        "盈利",
        np.where(
            df[
                "实时盈亏"
            ]
            <
            0,
            "亏损",
            "持平"
        )
    )

    # 利润率
    sales = (
        df[
            "支付金额"
        ]
        .to_numpy(
            dtype=float
        )
    )

    profit = (
        df[
            "实时盈亏"
        ]
        .to_numpy(
            dtype=float
        )
    )

    margin = np.zeros(
        len(df),
        dtype=float
    )

    valid_sales = (
        sales
        >
        0
    )

    margin[
        valid_sales
    ] = (
        profit[
            valid_sales
        ]
        /
        sales[
            valid_sales
        ]
    )

    df[
        "利润率"
    ] = np.nan_to_num(
        margin,
        nan=0.0,
        posinf=0.0,
        neginf=0.0
    )

    # 实际净投产 = 支付金额 / 总推广消耗
    charge = (
        df[
            "总推广消耗"
        ]
        .to_numpy(
            dtype=float
        )
    )

    roi = np.zeros(
        len(df),
        dtype=float
    )

    valid_charge = (
        charge
        >
        0
    )

    roi[
        valid_charge
    ] = (
        sales[
            valid_charge
        ]
        /
        charge[
            valid_charge
        ]
    )

    df[
        "实际净投产"
    ] = np.nan_to_num(
        roi,
        nan=0.0,
        posinf=0.0,
        neginf=0.0
    )

    money_cols = [
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "货品成本",
        "快递成本",
        "平台费用",
        "税费",
        "预估营销托管费用",
        "退款金额",
        "销售毛利",
        "实时盈亏",
    ]

    for col in money_cols:
        if col in df.columns:

            df[col] = (
                clean_numeric(
                    df[col]
                )
                .round(2)
            )

    df[
        "利润率"
    ] = (
        clean_numeric(
            df[
                "利润率"
            ]
        )
        .round(4)
    )

    if (
        "实际净投产"
        in df.columns
    ):
        df[
            "实际净投产"
        ] = (
            clean_numeric(
                df[
                    "实际净投产"
                ]
            )
            .round(2)
        )

    if (
        "推广后台ROI"
        in df.columns
    ):
        df[
            "推广后台ROI"
        ] = (
            clean_numeric(
                df[
                    "推广后台ROI"
                ]
            )
            .round(2)
        )

    # 主字段排序
    preferred = [
        "店铺",
        "抓取时间",

        "商品ID",
        "商品名称",
        "商品货号",

        "支付件数",
        "支付金额",

        "普通全站推广消耗",
        "智能托管消耗",
        "全站推广消耗",
        "关键词推广消耗",
        "总推广消耗",

        "SKU订单数",
        "SKU成交件数",
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "SKU成本未匹配行数",
        "SKU成本状态",
        "SKU成本来源",

        "货品成本",
        "快递成本",

        "平台扣点",
        "平台费用",

        "税点",
        "税费",

        "其他成本",
        "预估营销托管费用",
        "退款金额",
        "退款口径",
        "退款数据状态",

        "销售毛利",
        "实时盈亏",
        "利润率",
        "盈亏状态",
        "实际净投产",
        "推广后台ROI",
        "全站推广ROI",
        "关键词推广ROI",
    ]

    first_cols = [
        col
        for col in preferred
        if col in df.columns
    ]

    other_cols = [
        col
        for col in df.columns
        if col not in first_cols
    ]

    df = df[
        first_cols
        +
        other_cols
    ]

    now = datetime.now()

    history_file = (
        shop_dir
        /
        (
            "商品实时数据_"
            +
            now.strftime(
                "%Y%m%d_%H%M%S"
            )
            +
            "_SKU真实成本.csv"
        )
    )

    # 覆盖 latest，让后续仪表盘直接读取新成本
    df.to_csv(
        latest_file,
        index=False,
        encoding="utf-8-sig"
    )

    df.to_csv(
        history_file,
        index=False,
        encoding="utf-8-sig"
    )

    exact_count = int(
        exact_mask.sum()
    )

    partial_count = int(
        partial_mask.sum()
    )

    merch = float(
        df.loc[
            exact_mask,
            "SKU货品成本"
        ].sum()
    )

    freight = float(
        df.loc[
            exact_mask,
            "SKU快递成本"
        ].sum()
    )

    sales_total = float(
        df[
            "支付金额"
        ].sum()
    )

    profit_total = float(
        df[
            "实时盈亏"
        ].sum()
    )

    print()
    print(
        f"✅ {shop['name']} 成本整合完成"
    )

    print(
        f"   SKU真实成本商品：{exact_count}"
    )

    print(
        f"   SKU未完整匹配商品：{partial_count}"
    )

    print(
        f"   SKU货品成本：¥{merch:.2f}"
    )

    print(
        f"   SKU快递成本：¥{freight:.2f}"
    )

    print(
        f"   支付金额：¥{sales_total:.2f}"
    )

    print(
        f"   实时盈亏：¥{profit_total:.2f}"
    )

    print(
        f"   最新数据：{latest_file}"
    )

    return df


# ============================================================
# 重建多店总表
# ============================================================

def _snapshot_number(row, column, default=0.0):
    try:
        return float(
            clean_numeric(
                pd.Series(
                    [
                        row.get(
                            column,
                            default
                        )
                    ]
                )
            ).iloc[0]
        )
    except Exception:
        return float(
            default
        )


def _snapshot_optional_number(row, column):
    if column not in row.index:
        return None

    raw_value = row.get(
        column
    )

    if pd.isna(
        raw_value
    ):
        return None

    try:
        value = clean_numeric(
            pd.Series(
                [
                    raw_value
                ]
            )
        ).iloc[0]
    except Exception:
        return None

    if pd.isna(value):
        return None

    return float(
        value
    )


def _snapshot_first_number(row, columns, default=0.0):
    for column in columns:
        if column not in row.index:
            continue

        value = _snapshot_optional_number(
            row,
            column
        )

        if value is not None:
            return value

    return float(
        default
    )


def _snapshot_ratio(numerator, denominator):
    try:
        denominator = float(denominator)
        if abs(denominator) < 1e-9:
            return None
        return float(numerator) / denominator
    except Exception:
        return None


def write_realtime_snapshot(df):
    snapshot_dir = (
        DATA_ROOT
        /
        "realtime_snapshot"
    )
    snapshot_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    latest = (
        snapshot_dir
        /
        "latest.json"
    )
    previous_payload = {}
    if latest.exists():
        try:
            previous_payload = json.loads(
                latest.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            previous_payload = {}

    generated_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    records = []
    store_adjustments = {}

    for _, row in df.iterrows():
        captured_raw = row.get(
            "抓取时间",
            generated_at
        )

        if pd.isna(
            captured_raw
        ) or not str(
            captured_raw
        ).strip():
            captured_raw = generated_at

        captured_at = str(
            captured_raw
        )
        date_text = captured_at[:10]

        pay_amount = _snapshot_number(row, "支付金额")
        normal_site_ad_cost = _snapshot_number(row, "普通全站推广消耗")
        smart_ad_cost = _snapshot_number(row, "智能托管消耗")
        site_ad_cost = _snapshot_number(row, "全站推广消耗")
        keyword_ad_cost = _snapshot_number(row, "关键词推广消耗")
        component_ad_cost = (
            site_ad_cost
            +
            keyword_ad_cost
        )
        ad_cost = _snapshot_number(row, "总推广消耗")
        if component_ad_cost > 0 and abs(component_ad_cost - ad_cost) > 0.01:
            ad_cost = component_ad_cost
        profit = _snapshot_number(row, "实时盈亏")
        break_even_ad_cost = ad_cost + profit
        current_roi = _snapshot_ratio(profit, ad_cost)
        break_even_roi = _snapshot_ratio(pay_amount, break_even_ad_cost) if break_even_ad_cost > 0 else None
        ad_balance = _snapshot_optional_number(row, "账户推广余额")

        records.append(
            {
                "store": str(row.get("店铺", "")),
                "captured_at": captured_at,
                "product_id": normalize_id(row.get("商品ID", "")),
                "product_name": str(row.get("商品名称", "")),
                "sales_qty": _snapshot_first_number(
                    row,
                    [
                        "支付件数",
                        "SKU成交件数",
                    ]
                ),
                "pay_amount": pay_amount,
                "normal_site_ad_cost": normal_site_ad_cost,
                "smart_ad_cost": smart_ad_cost,
                "site_ad_cost": site_ad_cost,
                "keyword_ad_cost": keyword_ad_cost,
                "ad_cost": ad_cost,
                "promotion_roi": _snapshot_optional_number(row, "推广后台ROI"),
                "site_promotion_roi": _snapshot_optional_number(row, "全站推广ROI"),
                "keyword_promotion_roi": _snapshot_optional_number(row, "关键词推广ROI"),
                "current_roi": current_roi,
                "break_even_roi": break_even_roi,
                "ad_balance": ad_balance,
                "ad_balance_source": "promotion_balance_api" if ad_balance is not None else "",
                "order_count": _snapshot_number(row, "SKU订单数"),
                "sku_count": _snapshot_number(row, "SKU成交件数"),
                "merch_cost": _snapshot_number(row, "货品成本"),
                "freight_cost": _snapshot_number(row, "快递成本"),
                "platform_fee": _snapshot_number(row, "平台费用"),
                "tax_fee": _snapshot_number(row, "税费"),
                "estimated_marketing_cost": _snapshot_number(row, "预估营销托管费用"),
                "refund_amount": _snapshot_number(row, "退款金额"),
                "gross_profit": _snapshot_number(row, "销售毛利"),
                "profit": profit,
                "unmatched_sku_rows": _snapshot_number(row, "SKU成本未匹配行数"),
                "date": date_text,
            }
        )

        store_name = str(row.get("店铺", "")).strip()
        store_level_ad_cost = _snapshot_number(row, "店铺级推广消耗")
        if store_name and store_level_ad_cost > 0:
            current = store_adjustments.get(
                store_name,
                {
                    "store": store_name,
                    "store_level_ad_cost": 0.0,
                }
            )
            current["store_level_ad_cost"] = max(
                float(current.get("store_level_ad_cost", 0.0)),
                store_level_ad_cost
            )
            store_adjustments[store_name] = current

    seen_stores = {
        str(record.get("store", "")).strip()
        for record in records
        if str(record.get("store", "")).strip()
    }
    for shop in load_enabled_shops():
        name = shop_name(shop)
        summary_file = DATA_ROOT / safe_filename(name) / "latest_summary.json"
        summary = {}
        if summary_file.exists():
            try:
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
            except Exception:
                summary = {}

        store_ad_cost = float(summary.get("store_ad_cost", 0) or 0)
        if name and store_ad_cost > 0:
            current = store_adjustments.get(
                name,
                {
                    "store": name,
                    "store_level_ad_cost": 0.0,
                }
            )
            current["store_level_ad_cost"] = max(
                float(current.get("store_level_ad_cost", 0.0)),
                store_ad_cost,
            )
            store_adjustments[name] = current

        if not name or name in seen_stores:
            continue
        captured_at = str(summary.get("generated_at") or generated_at)
        date_text = str(summary.get("order_day") or captured_at[:10])
        overall_profit = float(summary.get("overall_profit", 0) or 0)
        records.append(
            {
                "store": name,
                "captured_at": captured_at,
                "product_id": "",
                "product_name": "店铺暂无商品成交",
                "sales_qty": 0.0,
                "pay_amount": float(summary.get("pay_amount", 0) or 0),
                "normal_site_ad_cost": 0.0,
                "smart_ad_cost": 0.0,
                "site_ad_cost": 0.0,
                "keyword_ad_cost": 0.0,
                "ad_cost": float(summary.get("product_ad_cost", 0) or 0),
                "promotion_roi": None,
                "site_promotion_roi": None,
                "keyword_promotion_roi": None,
                "current_roi": None,
                "break_even_roi": None,
                "ad_balance": None,
                "ad_balance_source": "",
                "order_count": 0.0,
                "sku_count": 0.0,
                "merch_cost": 0.0,
                "freight_cost": 0.0,
                "platform_fee": 0.0,
                "tax_fee": 0.0,
                "estimated_marketing_cost": 0.0,
                "refund_amount": float(summary.get("refund_amount", 0) or 0),
                "gross_profit": 0.0,
                "profit": overall_profit + store_ad_cost,
                "unmatched_sku_rows": 0.0,
                "date": date_text,
            }
        )
        if store_ad_cost > 0:
            store_adjustments[name] = {
                "store": name,
                "store_level_ad_cost": store_ad_cost,
            }

    payload = {
        "generated_at": generated_at,
        "previous_generated_at": previous_payload.get("generated_at", ""),
        "previous_records": previous_payload.get("records", []),
        "previous_store_adjustments": previous_payload.get("store_adjustments", []),
        "source": Path(__file__).name,
        "store_adjustments": list(store_adjustments.values()),
        "records": records,
    }

    previous_file = (
        snapshot_dir
        /
        "previous.json"
    )
    if previous_payload:
        previous_file.write_text(
            json.dumps(
                previous_payload,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    latest.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    return latest


def rebuild_all_shops(
    frames
):
    valid = [
        x
        for x in frames
        if x is not None
        and
        not x.empty
    ]

    if not valid:
        return None

    df = pd.concat(
        valid,
        ignore_index=True,
        sort=False
    )

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
            datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )
            +
            "_SKU真实成本.csv"
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

    snapshot = write_realtime_snapshot(
        df
    )

    print(
        f"网站实时快照：{snapshot}"
    )

    return df


# ============================================================
# 阶段3：多店成本整合
# ============================================================

def integrate_all_shops():
    print()
    print("=" * 76)
    print("阶段 4 / 4：整合 SKU 真实成本 + 当天退款")
    print("=" * 76)

    shops = load_enabled_shops()

    if GUOHUO_SHOP_NAME in SKU_SUCCESS_SHOPS:
        exists = any(
            shop["name"] == GUOHUO_SHOP_NAME
            for shop in shops
        )

        if not exists:
            shops.append({
                "name": GUOHUO_SHOP_NAME,
                "safe_name": safe_filename(GUOHUO_SHOP_NAME),
            })

    frames = []

    success = 0
    failed = 0

    for shop in shops:

        try:
            if is_douyin_shop(shop):
                df = integrate_douyin_shop(
                    shop
                )
            elif is_xiaohongshu_shop(shop):
                df = integrate_xiaohongshu_shop(
                    shop
                )
            else:
                df = integrate_shop(
                    shop
                )

            if df is not None:
                frames.append(
                    df
                )

            success += 1

        except Exception as e:

            failed += 1

            print()
            print(
                f"❌ [{shop['name']}] 成本整合失败：{e}"
            )

            print(
                traceback.format_exc()
            )

            continue

    combined = rebuild_all_shops(
        frames
    )

    print()
    print("=" * 76)
    print("多店 SKU 真实成本整合结果")
    print("=" * 76)
    print(
        f"整合成功：{success} 家"
    )
    print(
        f"整合失败：{failed} 家"
    )

    if combined is not None:

        total_sales = float(
            clean_numeric(
                combined.get(
                    "支付金额",
                    pd.Series(
                        [0] * len(
                            combined
                        )
                    )
                )
            ).sum()
        )

        total_profit = float(
            clean_numeric(
                combined.get(
                    "实时盈亏",
                    pd.Series(
                        [0] * len(
                            combined
                        )
                    )
                )
            ).sum()
        )

        exact = 0

        if (
            "SKU成本状态"
            in combined.columns
        ):
            exact = int(
                (
                    combined[
                        "SKU成本状态"
                    ]
                    ==
                    "完整"
                ).sum()
            )

        unmatched = 0

        if (
            "SKU成本未匹配行数"
            in combined.columns
        ):
            unmatched = int(
                clean_numeric(
                    combined[
                        "SKU成本未匹配行数"
                    ]
                ).sum()
            )

        print(
            f"总商品记录：{len(combined)}"
        )

        print(
            f"SKU真实成本商品：{exact}"
        )

        print(
            f"SKU成本未匹配行：{unmatched}"
        )
        store_count = combined["店铺"].dropna().astype(str).str.strip().nunique() if "店铺" in combined.columns else 0
        store_scope = f"{store_count}店" if store_count else "多店"

        print(
            f"{store_scope}支付金额：¥{total_sales:.2f}"
        )

        print(
            f"{store_scope}实时盈亏：¥{total_profit:.2f}"
        )

        print()
        print(
            f"{store_scope}总表："
        )

        print(
            DATA_ROOT
            /
            "all_shops_latest.csv"
        )

    return combined


# ============================================================
# MAIN
# ============================================================

def run_single_shop_pipeline(shop):
    name = shop_name(shop)
    print()
    print("=" * 76)
    print(f"单店串行抓取：{name}")
    print("=" * 76)

    launch_shop_browser(shop)

    success = False
    try:
        def _run():
            if is_douyin_shop(shop):
                run_douyin_crawler()
            elif is_xiaohongshu_shop(shop):
                run_xiaohongshu_crawler()
            elif is_guohuo_shop(name):
                run_guohuo_yanxuan_crawler()
            else:
                run_sku_crawler()
                run_profit_crawler()
                run_refund_crawler()

        with_only_shop(name, _run)
        success = True
        print(f"✓ {name} 单店抓取完成")
        return True
    finally:
        if success:
            close_shop_browser(shop, "本店抓取完成")
        else:
            print(f"⚠ {name} 抓取失败，浏览器暂时保留用于登录/排查")


def run_sequential_shop_pipelines():
    global SKU_SUCCESS_SHOPS
    global REFUND_RESULT_MAP
    global REFUND_RUN_STARTED_AT

    SKU_SUCCESS_SHOPS = set()
    REFUND_RESULT_MAP = {}
    REFUND_RUN_STARTED_AT = None

    shops = load_enabled_shops()
    if not shops:
        raise RuntimeError("没有启用店铺")

    close_managed_browsers(shops)

    success = 0
    failed = 0
    for shop in shops:
        try:
            if run_single_shop_pipeline(shop):
                success += 1
        except Exception as exc:
            failed += 1
            print()
            print(f"✗ {shop_name(shop)} 单店抓取失败：{exc}")
            print(traceback.format_exc())

    print()
    print("=" * 76)
    print(f"单店串行抓取完成：成功 {success} 家，失败 {failed} 家")
    print("=" * 76)


def run_hybrid_shop_pipelines():
    print()
    print("=" * 76)
    print("混合抓取模式：千牛/国货使用已登录浏览器，抖店抓完自动关闭")
    print("=" * 76)

    # 千牛三店：保持浏览器同时打开，避免串行重启导致登录态变化。
    run_sku_crawler()
    run_profit_crawler()
    run_refund_crawler()

    # 国货严选：使用已打开的专用端口，不主动关闭。
    run_guohuo_yanxuan_crawler()

    # 抖店：独立启动，抓取完成后关闭，节省资源。
    douyin_shops = [
        shop
        for shop in load_enabled_shops()
        if is_douyin_shop(shop)
    ]
    if douyin_shops:
        try:
            run_single_shop_pipeline(douyin_shops[0])
        except Exception as exc:
            print()
            print(f"✗ {shop_name(douyin_shops[0])} 抓取失败，跳过本轮抖店数据：{exc}")
            print(traceback.format_exc())
    else:
        print("ℹ️ 未启用抖店，跳过")

    # 小红书千帆：使用已登录端口，抓取实时订单/退款/推广。
    xhs_shops = [
        shop
        for shop in load_enabled_shops()
        if is_xiaohongshu_shop(shop)
    ]
    if xhs_shops:
        try:
            run_xiaohongshu_crawler()
        except Exception as exc:
            print()
            print(f"✗ 小红书店铺抓取失败，跳过本轮小红书数据：{exc}")
            print(traceback.format_exc())
    else:
        print("ℹ️ 未启用小红书店铺，跳过")


def sync_outputs_to_server():
    if os.environ.get("TMALL_SKIP_AUTO_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}:
        print("ℹ️ 已按环境变量跳过自动同步服务器")
        return

    sync_steps = [
        (
            "SKU成本维护表",
            [
                sys.executable,
                str(BASE_DIR / "sync_sku_cost.py"),
                "push",
            ],
        ),
        (
            "网站实时快照",
            [
                sys.executable,
                str(BASE_DIR / "upload_realtime_snapshot.py"),
            ],
        ),
    ]

    print()
    print("=" * 76)
    print("自动同步服务器")
    print("=" * 76)

    for label, command in sync_steps:
        print(f"同步{label}...")
        try:
            completed = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=240,
            )
        except Exception as exc:
            print(f"⚠ {label}同步失败：{exc}")
            continue

        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if output:
            print(output)
        if error:
            print(error)
        if completed.returncode != 0:
            print(f"⚠ {label}同步失败，退出码：{completed.returncode}")
        else:
            print(f"✓ {label}同步完成")


def main():
    print()
    print("=" * 76)
    print(f"千牛多店铺实时盈亏 {VERSION}")
    print("SKU本轮真实成本 + 当天退款成功 + 多店实时盈亏")
    print("=" * 76)

    sequential = os.environ.get("TMALL_SEQUENTIAL_BROWSERS", "0").strip().lower()
    if sequential in {"1", "true", "yes", "on"}:
        run_sequential_shop_pipelines()
    else:
        run_hybrid_shop_pipelines()

    # 4. SKU成本 + 退款覆盖 / 重算
    integrate_all_shops()
    sync_outputs_to_server()

    print()
    print("=" * 76)
    print(f"{VERSION} 一键流程全部完成")
    print("=" * 76)

    print()
    print(
        "当前实时盈亏已扣：SKU货品成本、订单快递费、平台费用、税费、推广费、当天退款成功金额；配置了营销托管比例的店铺会按支付金额预估扣减；抖店/小红书的店铺被投推广按店铺级扣减，推商品推广按商品/SKU扣减。"
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\n用户停止程序"
        )
        sys.exit(130)

    except Exception as e:

        print()
        print("=" * 76)
        print("程序异常")
        print("=" * 76)

        print(
            f"{type(e).__name__}: {e}"
        )

        print()
        print(
            traceback.format_exc()
        )
        sys.exit(1)

    if (
        sys.stdin.isatty()
        and
        not os.environ.get("TMALL_NO_PAUSE")
    ):
        input(
            "\n按 Enter 退出..."
        )

# -*- coding: utf-8 -*-
"""
refund_probe.py
退款字段探测器 V1.0

用途：
- 不改动现有 V4.9 / SKU 抓取器
- 自动扫描三家店最新的 order_raw_YYYYMMDD.json
- 递归查找所有可能与退款有关的字段：
  refund / refundFee / refundAmt / refundAmount / refundMoney / 退款 等
- 输出：
  1. CMD 候选字段统计
  2. data/refund_probe_result.csv
  3. data/refund_probe_samples.json

下一步根据探测结果，把“退款金额”正式接入实时盈亏。
"""

from pathlib import Path
from datetime import datetime
import csv
import json
import re

BASE_DIR = Path(__file__).resolve().parent
LOG_ROOT = BASE_DIR / "logs"
DATA_ROOT = BASE_DIR / "data"
SHOPS_FILE = BASE_DIR / "shops.json"

DATA_ROOT.mkdir(exist_ok=True)

OUT_CSV = DATA_ROOT / "refund_probe_result.csv"
OUT_JSON = DATA_ROOT / "refund_probe_samples.json"

KEYWORDS = [
    "refund",
    "refundfee",
    "refundamt",
    "refundamount",
    "refundmoney",
    "refundprice",
    "refundstatus",
    "refundphase",
    "refundstate",
    "afterSale",
    "aftersale",
    "return",
    "退款",
    "退货",
    "售后",
]

MONEY_WORDS = [
    "fee",
    "amt",
    "amount",
    "money",
    "price",
    "pay",
    "refund",
    "退款金额",
]


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip()


def load_shops():
    with open(SHOPS_FILE, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    shops = []
    for x in cfg.get("shops", []):
        if not x.get("enabled", True):
            continue

        name = str(x.get("name", "")).strip()
        if not name:
            continue

        shops.append({
            "name": name,
            "safe_name": safe_filename(name),
        })

    return shops


def newest_order_raw(shop_safe_name):
    folder = LOG_ROOT / shop_safe_name

    if not folder.exists():
        return None

    files = sorted(
        folder.glob("order_raw_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    return files[0] if files else None


def looks_refund_key(key):
    k = str(key).lower()

    for word in KEYWORDS:
        if str(word).lower() in k:
            return True

    return False


def looks_money_key(key):
    k = str(key).lower()
    return any(str(x).lower() in k for x in MONEY_WORDS)


def short_value(value):
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    else:
        text = str(value)

    text = " ".join(text.replace("\n", " ").replace("\r", " ").split())

    if len(text) > 300:
        text = text[:300] + "..."

    return text


def walk(obj, path="", order_hint=""):
    found = []

    if isinstance(obj, dict):

        current_order = order_hint

        # 尝试在当前对象识别订单号
        for candidate in [
            "id",
            "orderId",
            "tradeId",
            "bizOrderId",
            "mainOrderId",
        ]:
            value = obj.get(candidate)

            if value is not None:
                text = str(value).strip()

                # 淘宝订单号一般较长，减少把普通 id 当订单号
                if text.isdigit() and len(text) >= 12:
                    current_order = text
                    break

        for key, value in obj.items():

            new_path = f"{path}.{key}" if path else str(key)

            if looks_refund_key(key):

                found.append({
                    "path": new_path,
                    "key": str(key),
                    "value": short_value(value),
                    "value_type": type(value).__name__,
                    "order_hint": current_order,
                    "money_like": looks_money_key(key),
                })

            found.extend(
                walk(
                    value,
                    new_path,
                    current_order,
                )
            )

    elif isinstance(obj, list):

        for index, value in enumerate(obj):

            found.extend(
                walk(
                    value,
                    f"{path}[{index}]",
                    order_hint,
                )
            )

    return found


def main():
    print("=" * 76)
    print("千牛退款字段探测器 V1.0")
    print("只扫描现有订单原始JSON，不修改任何正式数据")
    print("=" * 76)

    shops = load_shops()

    all_rows = []
    sample_json = {}

    for shop in shops:

        raw_file = newest_order_raw(
            shop["safe_name"]
        )

        print()
        print(f"[{shop['name']}]")

        if raw_file is None:

            print("  × 没找到 order_raw_*.json")
            continue

        print(f"  原始订单：{raw_file}")

        try:
            with open(
                raw_file,
                "r",
                encoding="utf-8"
            ) as f:
                data = json.load(f)

        except Exception as e:

            print(f"  × JSON读取失败：{e}")
            continue

        found = walk(data)

        # path/key/value 去重
        seen = set()
        unique = []

        for row in found:

            key = (
                row["path"],
                row["key"],
                row["value"],
            )

            if key in seen:
                continue

            seen.add(key)
            unique.append(row)

        print(f"  发现退款相关字段：{len(unique)} 个")

        # 字段统计
        counter = {}

        for row in unique:

            counter[row["key"]] = (
                counter.get(row["key"], 0) + 1
            )

            all_rows.append({
                "店铺": shop["name"],
                "原始文件": str(raw_file),
                "订单号提示": row["order_hint"],
                "字段路径": row["path"],
                "字段名": row["key"],
                "字段值": row["value"],
                "字段类型": row["value_type"],
                "疑似金额字段": "是" if row["money_like"] else "否",
            })

        for key, count in sorted(
            counter.items(),
            key=lambda x: (-x[1], x[0])
        )[:30]:

            print(
                f"    {key}: {count}"
            )

        # 每家保留最多100个样例
        sample_json[
            shop["name"]
        ] = unique[:100]

    fields = [
        "店铺",
        "原始文件",
        "订单号提示",
        "字段路径",
        "字段名",
        "字段值",
        "字段类型",
        "疑似金额字段",
    ]

    with open(
        OUT_CSV,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(all_rows)

    with open(
        OUT_JSON,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            sample_json,
            f,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 76)
    print("探测完成")
    print("=" * 76)
    print(f"候选字段总数：{len(all_rows)}")
    print(f"CSV：{OUT_CSV}")
    print(f"样例JSON：{OUT_JSON}")
    print()
    print("下一步：把 CMD 最后结果截图，或把 refund_probe_result.csv 发给我。")
    print("我会据此直接把退款金额接进 V4.9。")
    print("=" * 76)


if __name__ == "__main__":
    main()
    input("\n按 Enter 退出...")

# -*- coding: utf-8 -*-
"""
千牛多店铺实时盈亏 V4.8 - SKU真实成本整合启动器

设计目的：
1. 不改你当前已经稳定的 qianniu_profit_crawler.py 抓取核心。
2. 先运行当前主程序。
3. 主程序抓取完成后，自动读取：
      data/<店铺>/product_cost_summary_YYYYMMDD.csv
   把 SKU 实际货品成本 + 订单级快递成本合并到：
      data/<店铺>/latest.csv
4. 重新计算：
      销售毛利
      实时盈亏
      盈亏状态
      利润率
      实际净投产
5. 最后重建：
      data/all_shops_latest.csv

重要：
- SKU 成本汇总来自 order_sku_crawler.py V1.7。
- 若某店当天没有 product_cost_summary 文件，则该店保留原主程序成本逻辑，不会报错。
- 只有“SKU成本未匹配行数 = 0”的商品才替换成真实 SKU 成本；
  有未匹配 SKU 的商品不会用不完整成本覆盖旧成本，避免利润虚高。
"""

from pathlib import Path
from datetime import datetime
import importlib
import json
import re
import sys
import traceback

import numpy as np
import pandas as pd


VERSION = "V4.8-SKU-COST"

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / "data"
SHOPS_FILE = BASE_DIR / "shops.json"

MAIN_MODULE_NAME = "qianniu_profit_crawler"


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


def num(series_or_value, default=0.0):
    if isinstance(series_or_value, pd.Series):
        s = pd.to_numeric(
            series_or_value,
            errors="coerce"
        )

        return (
            s
            .replace(
                [np.inf, -np.inf],
                np.nan
            )
            .fillna(default)
            .astype("float64")
        )

    try:
        value = float(series_or_value)

        if np.isfinite(value):
            return value

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

    shops = []

    for shop in config.get(
        "shops",
        []
    ):
        if not shop.get(
            "enabled",
            True
        ):
            continue

        name = str(
            shop.get(
                "name",
                ""
            )
        ).strip()

        if not name:
            continue

        shops.append({
            "name":
                name,

            "safe_name":
                safe_filename(
                    name
                ),
        })

    return shops


# ============================================================
# 读取 SKU 商品成本汇总
# ============================================================

def load_product_cost_summary(
    shop
):
    date_text = datetime.now().strftime(
        "%Y%m%d"
    )

    path = (
        DATA_ROOT
        /
        shop["safe_name"]
        /
        f"product_cost_summary_{date_text}.csv"
    )

    if not path.exists():
        return None, path

    try:
        df = pd.read_csv(
            path,
            dtype={
                "商品ID":
                    str
            },
            encoding="utf-8-sig"
        )

    except Exception:
        df = pd.read_csv(
            path,
            dtype={
                "商品ID":
                    str
            }
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

        df[col] = num(
            df[col]
        )

    # 理论上已经一商品一行，再保险聚合一次。
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
# 单店 SKU 成本整合
# ============================================================

def integrate_shop_cost(
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
            f"⚠️ [{shop['name']}] "
            f"找不到 latest.csv，跳过成本整合"
        )

        return None

    cost_df, cost_file = (
        load_product_cost_summary(
            shop
        )
    )

    if cost_df is None:
        print(
            f"ℹ️ [{shop['name']}] "
            "今天没有 SKU 商品成本汇总，"
            "保留原成本逻辑"
        )

        print(
            f"   期待文件：{cost_file}"
        )

        return pd.read_csv(
            latest_file,
            dtype={
                "商品ID":
                    str
            },
            encoding="utf-8-sig"
        )

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
            f"{latest_file} 缺少商品ID"
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

    # 如果之前已经跑过 V4.8，先删旧的 SKU 成本辅助列，
    # 避免 _x / _y。
    sku_cols = [
        "SKU订单数",
        "SKU成交件数",
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "SKU成本未匹配行数",
        "SKU成本状态",
        "SKU成本来源",
    ]

    old_sku_cols = [
        x
        for x in sku_cols
        if x in df.columns
    ]

    if old_sku_cols:
        df = df.drop(
            columns=old_sku_cols
        )

    df = df.merge(
        cost_df,
        on="商品ID",
        how="left"
    )

    for col in [
        "SKU订单数",
        "SKU成交件数",
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "SKU成本未匹配行数",
    ]:
        df[col] = num(
            df[col]
        )

    # --------------------------------------------------------
    # exact_mask：
    # 有 SKU 成本记录，并且该商品没有未匹配成本行
    # --------------------------------------------------------

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

    df[
        "SKU成本来源"
    ] = np.where(
        exact_mask,
        "订单SKU真实成本",
        "保留主程序原成本"
    )

    # --------------------------------------------------------
    # 旧成本列不存在时补 0
    # --------------------------------------------------------

    for col in [
        "货品成本",
        "快递成本",
        "平台费用",
        "税费",
        "其他成本",
        "平台扣点",
        "税点",
        "支付金额",
        "总推广消耗",
    ]:
        if col not in df.columns:
            df[col] = 0

        df[col] = num(
            df[col]
        )

    # --------------------------------------------------------
    # 替换为 SKU 实际成本
    # --------------------------------------------------------

    df.loc[
        exact_mask,
        "货品成本"
    ] = (
        df.loc[
            exact_mask,
            "SKU货品成本"
        ]
    )

    df.loc[
        exact_mask,
        "快递成本"
    ] = (
        df.loc[
            exact_mask,
            "SKU快递成本"
        ]
    )

    # --------------------------------------------------------
    # 平台费用 / 税费
    # 保持主程序当前费率逻辑
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 毛利、盈亏
    # --------------------------------------------------------

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
    )

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

    # --------------------------------------------------------
    # 利润率
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 实际净投产
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 金额格式
    # --------------------------------------------------------

    money_cols = [
        "SKU货品成本",
        "SKU快递成本",
        "SKU货品快递总成本",
        "货品成本",
        "快递成本",
        "平台费用",
        "税费",
        "销售毛利",
        "实时盈亏",
    ]

    for col in money_cols:
        if col in df.columns:
            df[col] = (
                num(
                    df[col]
                )
                .round(2)
            )

    if (
        "实际净投产"
        in df.columns
    ):
        df[
            "实际净投产"
        ] = (
            num(
                df[
                    "实际净投产"
                ]
            )
            .round(2)
        )

    df[
        "利润率"
    ] = (
        num(
            df[
                "利润率"
            ]
        )
        .round(4)
    )

    # --------------------------------------------------------
    # 调整关键字段顺序
    # --------------------------------------------------------

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
        "平台费用",
        "税费",
        "其他成本",

        "销售毛利",
        "实时盈亏",
        "利润率",
        "盈亏状态",
        "实际净投产",
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

    # --------------------------------------------------------
    # 保存覆盖 latest + SKU成本版历史快照
    # --------------------------------------------------------

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
            "_SKU成本版.csv"
        )
    )

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

    sku_merchandise = float(
        df.loc[
            exact_mask,
            "SKU货品成本"
        ].sum()
    )

    sku_shipping = float(
        df.loc[
            exact_mask,
            "SKU快递成本"
        ].sum()
    )

    total_profit = float(
        df[
            "实时盈亏"
        ].sum()
    )

    print()
    print(
        f"✅ [{shop['name']}] SKU真实成本已整合"
    )

    print(
        f"   SKU成本完整商品：{exact_count}"
    )

    print(
        f"   存在未匹配SKU商品：{partial_count}"
    )

    print(
        f"   SKU货品成本：¥{sku_merchandise:.2f}"
    )

    print(
        f"   SKU快递成本：¥{sku_shipping:.2f}"
    )

    print(
        f"   重算实时盈亏：¥{total_profit:.2f}"
    )

    print(
        f"   最新数据：{latest_file}"
    )

    return df


# ============================================================
# 重建全店总表
# ============================================================

def rebuild_all_shops(
    shop_frames
):
    frames = [
        df
        for df in shop_frames
        if df is not None
        and
        not df.empty
    ]

    if not frames:
        return None

    combined = pd.concat(
        frames,
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
            "_SKU成本版.csv"
        )
    )

    combined.to_csv(
        latest,
        index=False,
        encoding="utf-8-sig"
    )

    combined.to_csv(
        history,
        index=False,
        encoding="utf-8-sig"
    )

    return combined


# ============================================================
# 运行原主程序
# ============================================================

def run_original_main():
    print()
    print(
        "=" * 72
    )

    print(
        f"千牛实时盈亏 {VERSION}"
    )

    print(
        "第一阶段：运行当前稳定版抓取主程序"
    )

    print(
        "=" * 72
    )

    # 确保从当前项目目录导入。
    if str(
        BASE_DIR
    ) not in sys.path:

        sys.path.insert(
            0,
            str(
                BASE_DIR
            )
        )

    module = importlib.import_module(
        MAIN_MODULE_NAME
    )

    if not hasattr(
        module,
        "main"
    ):
        raise RuntimeError(
            "qianniu_profit_crawler.py 中找不到 main()"
        )

    module.main()


# ============================================================
# 主入口
# ============================================================

def main():
    # --------------------------------------------------------
    # 1. 跑原抓取器
    # --------------------------------------------------------

    run_original_main()

    # --------------------------------------------------------
    # 2. 接 SKU 真实成本
    # --------------------------------------------------------

    print()
    print(
        "=" * 72
    )

    print(
        "第二阶段：整合 SKU 真实货品成本 + 订单快递成本"
    )

    print(
        "=" * 72
    )

    shops = load_enabled_shops()

    frames = []

    for shop in shops:

        try:
            df = integrate_shop_cost(
                shop
            )

            if df is not None:
                frames.append(
                    df
                )

        except Exception as e:

            print()
            print(
                f"❌ [{shop['name']}] SKU成本整合失败：{e}"
            )

            print(
                traceback.format_exc()
            )

            # 成本整合失败不影响其他店
            continue

    combined = rebuild_all_shops(
        frames
    )

    print()
    print(
        "=" * 72
    )

    print(
        f"{VERSION} 完成"
    )

    print(
        "=" * 72
    )

    if combined is not None:

        total_sales = (
            float(
                num(
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
        )

        total_profit = (
            float(
                num(
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
        )

        exact_count = 0

        if (
            "SKU成本状态"
            in combined.columns
        ):
            exact_count = int(
                (
                    combined[
                        "SKU成本状态"
                    ]
                    ==
                    "完整"
                ).sum()
            )

        print(
            f"总商品记录：{len(combined)}"
        )

        print(
            f"SKU真实成本商品：{exact_count}"
        )

        print(
            f"全部支付金额：¥{total_sales:.2f}"
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

    print()
    print(
        "说明：退款尚未接入，本版盈亏为“未扣退款”的实时盈亏。"
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print(
            "\n用户停止程序"
        )

    except Exception as e:
        print()
        print(
            "=" * 72
        )
        print(
            "V4.8 主程序异常"
        )
        print(
            "=" * 72
        )
        print(
            f"{type(e).__name__}: {e}"
        )
        print()
        print(
            traceback.format_exc()
        )

    input(
        "\n按 Enter 退出..."
    )

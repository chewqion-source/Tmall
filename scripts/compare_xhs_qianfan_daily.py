from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "manual_vs_system_compare"
OUT_DIR.mkdir(parents=True, exist_ok=True)


ALIASES = {
    "pay": ["支付金额", "支付", "实付金额", "订单实付"],
    "refund": ["成功退款金额", "退款金额", "成功退款", "售后退款"],
    "ad": ["推广花费", "推广花", "推广消耗", "总推广消耗"],
    "profit": ["单品结余", "盈亏", "结余", "利润"],
    "qty": ["数量", "件数", "支付件数", "销量"],
    "orders": ["订单数", "订单", "支付订单数"],
    "cost": ["货总价", "货品成本", "货品总价"],
    "ship": ["发货费", "快递费", "运费"],
    "tax": ["税额", "税点"],
    "fee": ["扣点", "平台扣点"],
}


def norm(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().replace("\n", "").replace(" ", "")


def to_num(values: pd.Series) -> pd.Series:
    return pd.to_numeric(
        values.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("¥", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.strip(),
        errors="coerce",
    ).fillna(0.0)


def locate_file(root: Path, pattern: str, suffixes: tuple[str, ...]) -> Path:
    matches = [
        p
        for p in root.iterdir()
        if pattern in p.name and p.suffix.lower() in suffixes and p.is_file()
    ]
    if not matches:
        raise FileNotFoundError(f"{root} 下没有找到 {pattern}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def sheet_date(name: str) -> str | None:
    match = re.match(r"^(\d{1,2})[.\-月](\d{1,2})", str(name).strip())
    if not match:
        return None
    return f"2026-{int(match.group(1)):02d}-{int(match.group(2)):02d}"


def find_header(raw: pd.DataFrame) -> int | None:
    alias_words = {x for values in ALIASES.values() for x in values}
    alias_words.update({"商品ID", "货号", "商家编码", "SKU规格", "规格"})
    best_row = None
    best_score = -1
    for row_index in range(min(len(raw), 30)):
        row = [norm(value) for value in raw.iloc[row_index].tolist()]
        score = sum(1 for value in row if value in alias_words)
        if score > best_score:
            best_row = row_index
            best_score = score
    return best_row if best_score >= 3 else None


def pick_column(df: pd.DataFrame, kind: str) -> str | None:
    normalized = {norm(column): column for column in df.columns}
    for alias in ALIASES[kind]:
        if alias in normalized:
            return normalized[alias]
    return None


def summarize(path: Path) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    excel = pd.ExcelFile(path)
    rows: list[dict[str, object]] = []
    bad: list[tuple[str, str]] = []
    for sheet in excel.sheet_names:
        day = sheet_date(sheet)
        if not day:
            continue
        raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
        header_row = find_header(raw)
        if header_row is None:
            bad.append((sheet, "no_header"))
            continue
        columns = [norm(value) or f"col_{index}" for index, value in enumerate(raw.iloc[header_row].tolist())]
        df = raw.iloc[header_row + 1 :].copy()
        df.columns = columns
        df = df.dropna(how="all")
        row: dict[str, object] = {"date": day, "sheet": sheet, "rows": len(df)}
        for kind in ALIASES:
            column = pick_column(df, kind)
            row[kind] = round(float(to_num(df[column]).sum()), 2) if column else 0.0
            row[f"{kind}_col"] = column or ""
        rows.append(row)
    if not rows:
        return pd.DataFrame(), bad
    return pd.DataFrame(rows).sort_values("date"), bad


def main() -> None:
    manual = locate_file(Path.home() / "Desktop", "盲盒千帆", (".xls", ".xlsx"))
    system = locate_file(ROOT / "data", "盲盒千帆日报表", (".xlsx",))

    manual_daily, bad_manual = summarize(manual)
    system_daily, bad_system = summarize(system)
    if manual_daily.empty or system_daily.empty:
        raise RuntimeError(f"表格解析为空：manual={len(manual_daily)} system={len(system_daily)}")

    merged = manual_daily.merge(system_daily, on="date", how="outer", suffixes=("_manual", "_system")).fillna(0)
    for kind in ALIASES:
        merged[f"{kind}_diff"] = (merged[f"{kind}_system"] - merged[f"{kind}_manual"]).round(2)
    merged["abs_profit_diff"] = merged["profit_diff"].abs()
    merged["abs_pay_diff"] = merged["pay_diff"].abs()
    merged["abs_refund_diff"] = merged["refund_diff"].abs()
    merged["abs_ad_diff"] = merged["ad_diff"].abs()
    merged["needs_recrawl"] = (
        (merged["abs_profit_diff"] > 50)
        | (merged["abs_pay_diff"] > 20)
        | (merged["abs_refund_diff"] > 20)
        | (merged["abs_ad_diff"] > 20)
    )

    daily_path = OUT_DIR / "xhs_qianfan_daily_compare_current.csv"
    summary_path = OUT_DIR / "xhs_qianfan_compare_summary_current.csv"
    merged.to_csv(daily_path, index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            {
                "manual_pay": manual_daily["pay"].sum(),
                "system_pay": system_daily["pay"].sum(),
                "pay_diff": merged["pay_diff"].sum(),
                "manual_refund": manual_daily["refund"].sum(),
                "system_refund": system_daily["refund"].sum(),
                "refund_diff": merged["refund_diff"].sum(),
                "manual_ad": manual_daily["ad"].sum(),
                "system_ad": system_daily["ad"].sum(),
                "ad_diff": merged["ad_diff"].sum(),
                "manual_profit": manual_daily["profit"].sum(),
                "system_profit": system_daily["profit"].sum(),
                "profit_diff": merged["profit_diff"].sum(),
                "bad_manual": len(bad_manual),
                "bad_system": len(bad_system),
                "recrawl_days": int(merged["needs_recrawl"].sum()),
            }
        ]
    )
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    cols = [
        "date",
        "pay_manual",
        "pay_system",
        "pay_diff",
        "refund_manual",
        "refund_system",
        "refund_diff",
        "ad_manual",
        "ad_system",
        "ad_diff",
        "profit_manual",
        "profit_system",
        "profit_diff",
        "cost_diff",
        "qty_diff",
        "orders_diff",
        "needs_recrawl",
    ]
    top = merged.sort_values(
        ["needs_recrawl", "abs_profit_diff", "abs_pay_diff", "abs_ad_diff"],
        ascending=[False, False, False, False],
    )[cols].head(25)
    print(summary.to_string(index=False))
    print("\nTOP_DIFF")
    print(top.to_string(index=False))
    print(f"\nDAILY_CSV={daily_path}")
    print(f"SUMMARY_CSV={summary_path}")


if __name__ == "__main__":
    main()

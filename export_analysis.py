from pathlib import Path

import pandas as pd

from data_loader import (
    build_summary,
    complete_daily_series,
    find_store_workbooks,
    load_store_daily,
    validate_known_sample,
)


project_dir = Path(__file__).resolve().parent
sources = find_store_workbooks()
daily = load_store_daily(sources)
sample = validate_known_sample(daily[daily["store"] == "易丽洁"])

complete_frames = []
summary_frames = []
for store, store_daily in daily.groupby("store", sort=False):
    complete = complete_daily_series(store_daily.copy())
    complete["store"] = store
    complete = complete[["store", *[column for column in complete.columns if column != "store"]]]
    summary = build_summary(store_daily, complete)
    summary.insert(0, "store", store)
    complete_frames.append(complete)
    summary_frames.append(summary)

complete_all = pd.concat(complete_frames, ignore_index=True)
summary_all = pd.concat(summary_frames, ignore_index=True)

output_dir = project_dir / "data"
output_dir.mkdir(exist_ok=True)
daily.to_csv(output_dir / "四店商品按日聚合.csv", index=False, encoding="utf-8-sig")
complete_all.to_csv(output_dir / "四店商品跨日变化.csv", index=False, encoding="utf-8-sig")
summary_all.to_csv(output_dir / "四店商品汇总.csv", index=False, encoding="utf-8-sig")

print("sources")
for store, path in sources.items():
    print(f"  {store}={path}")
print(f"sample={sample}")
print("store_totals")
print(
    daily.groupby("store")
    .agg(
        date_sheets=("date", "nunique"),
        products=("product_id", "nunique"),
        rows=("product_id", "size"),
        sales=("sales_qty", "sum"),
        orders=("order_count", "sum"),
        profit=("profit", "sum"),
    )
    .to_string()
)

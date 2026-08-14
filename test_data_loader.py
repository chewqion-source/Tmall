from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from data_loader import (
    LEGACY_SUMMARY_PRODUCT_ID,
    MIN_REPORT_DATE,
    complete_daily_series,
    find_store_workbooks,
    load_product_daily,
    load_product_daily_cached,
    load_store_daily,
    validate_known_sample,
)


def test_converted_workbook_and_merged_sample():
    workbook = Path(__file__).resolve().parent / "data" / "2026年天猫日报表-易丽洁8月.xlsx"
    daily = load_product_daily(workbook)
    assert daily["sheet"].nunique() == 13
    assert validate_known_sample(daily) == {
        "sales_qty": 20.0,
        "order_count": 9.0,
        "profit": 22.015,
    }

    complete = complete_daily_series(daily)
    assert complete["date"].nunique() == 13
    assert not complete.duplicated(["product_id", "date"]).any()


def test_four_desktop_stores_stay_separate():
    sources = find_store_workbooks()
    daily = load_store_daily(sources)
    assert set(daily["store"]) == {"易丽洁", "坐拥宁静", "国货严选", "咖时光"}
    assert daily.groupby("store")["source_file"].nunique().eq(1).all()
    assert not daily.duplicated(["store", "date", "product_id"]).any()


def test_kashiguang_filters_dates_before_july():
    workbook = find_store_workbooks()["咖时光"]
    daily = load_product_daily_cached(workbook)
    assert daily["date"].min() == MIN_REPORT_DATE
    assert LEGACY_SUMMARY_PRODUCT_ID not in set(daily["product_id"])
    assert ((daily["order_count"] % 1).abs() < 1e-9).all()


def test_disk_cache_reuses_unchanged_workbook():
    workbook = Path(__file__).resolve().parent / "data" / "2026年天猫日报表-易丽洁8月.xlsx"
    with TemporaryDirectory() as cache_dir:
        first = load_product_daily_cached(workbook, cache_dir)
        second = load_product_daily_cached(workbook, cache_dir)
        assert first.attrs["cache_hit"] is False
        assert second.attrs["cache_hit"] is True
        pd.testing.assert_frame_equal(first, second)
        assert len(list(Path(cache_dir).glob("*.pkl"))) == 1
        assert len(list(Path(cache_dir).glob("*.json"))) == 1

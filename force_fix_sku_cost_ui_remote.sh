set -e
APP=/opt/tmall-dashboard/current/app.py
cd /opt/tmall-dashboard/current
cp "$APP" "$APP.bak.$(date +%Y%m%d_%H%M%S)"
python3 - <<'PY'
from pathlib import Path

p = Path("/opt/tmall-dashboard/current/app.py")
text = p.read_text(encoding="utf-8")

STORE = "\u5e97\u94fa"
FREIGHT = "\u5feb\u9012\u8d39"
UNIT_PRICE = "\u5355\u4ef6\u8d27\u4ef7"
GUOHUO = "\u56fd\u8d27\u4e25\u9009"
BATCH_BUTTON = "\u6279\u91cf\u586b\u5199\u5f53\u524d\u7b5b\u9009"

data_dir_line = 'DATA_DIR = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))\n'
if data_dir_line not in text:
    text = text.replace("SKU_COST_PATH = Path(\n", data_dir_line + "SKU_COST_PATH = Path(\n", 1)
text = text.replace(
    '        Path(__file__).resolve().parent / "data" / "sku_cost.xlsx",\n',
    '        DATA_DIR / "sku_cost.xlsx",\n',
    1,
)
first = text.find(data_dir_line)
if first >= 0:
    second = text.find(data_dir_line, first + len(data_dir_line))
    if second >= 0:
        text = text[:second] + text[second + len(data_dir_line):]

old_store_line = f'    stores = sorted(store for store in data["{STORE}"].dropna().unique() if str(store).strip())\n'
new_store_block = f'''    default_store_options = ["\u6613\u4e3d\u6d01", "\u5496\u65f6\u5149", "\u5750\u62e5_\u5b81\u9759", "\u5750\u62e5\u5b81\u9759", "{GUOHUO}"]
    stores = sorted(
        {{
            *default_store_options,
            *[str(store).strip() for store in data["{STORE}"].dropna().unique() if str(store).strip()],
        }}
    )
'''
if "default_store_options = [" not in text:
    if old_store_line not in text:
        raise SystemExit("store options line not found; stop to avoid unsafe edit")
    text = text.replace(old_store_line, new_store_block, 1)

text = text.replace(
    f'"{STORE}": st.column_config.SelectboxColumn("{STORE}", options=["\u6613\u4e3d\u6d01", "\u5496\u65f6\u5149", "\u5750\u62e5_\u5b81\u9759", "\u5750\u62e5\u5b81\u9759"]),',
    f'"{STORE}": st.column_config.SelectboxColumn("{STORE}", options=stores),',
)

batch_block = f'''    batch_cols = st.columns([1, 1, 1, 1.2])
    with batch_cols[0]:
        batch_column = st.selectbox("\u6279\u91cf\u5b57\u6bb5", ["{FREIGHT}", "{UNIT_PRICE}"])
    with batch_cols[1]:
        batch_value = st.number_input("\u6279\u91cf\u91d1\u989d", min_value=0.0, value=3.7, step=0.1, format="%.2f")
    with batch_cols[2]:
        batch_scope = st.selectbox("\u586b\u5199\u8303\u56f4", ["\u53ea\u586b\u7a7a\u503c", "\u8986\u76d6\u5f53\u524d\u7b5b\u9009"])
    with batch_cols[3]:
        st.write("")
        st.write("")
        if st.button("{BATCH_BUTTON}", type="secondary", width="stretch"):
            target_ids = view["_row_id"].dropna().astype(int).tolist()
            if not target_ids:
                st.warning("\u5f53\u524d\u7b5b\u9009\u6ca1\u6709\u53ef\u586b\u5199\u7684 SKU\u3002")
            else:
                save_data = data.copy()
                target_mask = save_data.index.isin(target_ids)
                if batch_scope == "\u53ea\u586b\u7a7a\u503c":
                    target_mask = target_mask & save_data[batch_column].isna()
                changed = int(target_mask.sum())
                if changed == 0:
                    st.info("\u5f53\u524d\u7b5b\u9009\u91cc\u6ca1\u6709\u9700\u8981\u586b\u5199\u7684\u7a7a\u503c\u3002")
                else:
                    save_data.loc[target_mask, batch_column] = round(float(batch_value), 2)
                    backup_path = save_sku_cost_frame(save_data)
                    st.success(
                        f"\u5df2\u6279\u91cf\u586b\u5199 {{changed}} \u884c {{batch_column}}\u3002"
                        + (f" \u65e7\u6587\u4ef6\u5907\u4efd\uff1a{{backup_path.name}}" if backup_path else "")
                    )
                    st.rerun()

'''
if BATCH_BUTTON not in text:
    marker = "    edited = st.data_editor(\n"
    if marker not in text:
        raise SystemExit("sku editor marker not found; stop to avoid unsafe edit")
    text = text.replace(marker, batch_block + marker, 1)

p.write_text(text, encoding="utf-8")
PY
python3 -m py_compile "$APP"
sudo systemctl restart tmall-dashboard.service
sleep 3
systemctl is-active tmall-dashboard.service
python3 - <<'PY'
from pathlib import Path
text = Path("/opt/tmall-dashboard/current/app.py").read_text(encoding="utf-8")
print("shared_cost_path=", 'DATA_DIR / "sku_cost.xlsx"' in text)
print("batch_fill=", "\u6279\u91cf\u586b\u5199\u5f53\u524d\u7b5b\u9009" in text)
print("guohuo_store=", "\u56fd\u8d27\u4e25\u9009" in text)
PY

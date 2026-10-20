set -e
cd /opt/tmall-dashboard/current
cp app.py "app.py.bak.$(date +%Y%m%d_%H%M%S)"
python3 - <<'PY'
from pathlib import Path
p = Path('/opt/tmall-dashboard/current/app.py')
text = p.read_text(encoding='utf-8')

data_dir_line = 'DATA_DIR = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))\n'
if data_dir_line not in text:
    text = text.replace('SKU_COST_PATH = Path(\n', data_dir_line + 'SKU_COST_PATH = Path(\n', 1)
text = text.replace(
    '        Path(__file__).resolve().parent / "data" / "sku_cost.xlsx",\n',
    '        DATA_DIR / "sku_cost.xlsx",\n',
    1,
)
first = text.find(data_dir_line)
if first != -1:
    second = text.find(data_dir_line, first + len(data_dir_line))
    if second != -1:
        text = text[:second] + text[second + len(data_dir_line):]

old_stores = '    stores = sorted(store for store in data["\u5e97\u94fa"].dropna().unique() if str(store).strip())\n'
new_stores = '''    default_store_options = ["\u6613\u4e3d\u6d01", "\u5496\u65f6\u5149", "\u5750\u62e5_\u5b81\u9759", "\u5750\u62e5\u5b81\u9759", "\u56fd\u8d27\u4e25\u9009"]
    stores = sorted(
        {
            *default_store_options,
            *[str(store).strip() for store in data["\u5e97\u94fa"].dropna().unique() if str(store).strip()],
        }
    )
'''
if old_stores in text and 'default_store_options' not in text:
    text = text.replace(old_stores, new_stores, 1)

text = text.replace(
    '"\u5e97\u94fa": st.column_config.SelectboxColumn("\u5e97\u94fa", options=["\u6613\u4e3d\u6d01", "\u5496\u65f6\u5149", "\u5750\u62e5_\u5b81\u9759", "\u5750\u62e5\u5b81\u9759"]),',
    '"\u5e97\u94fa": st.column_config.SelectboxColumn("\u5e97\u94fa", options=stores),',
)

caption = '    st.caption("\u53ef\u76f4\u63a5\u4fee\u6539\u5355\u4ef6\u8d27\u4ef7\u3001\u5feb\u9012\u8d39\uff0c\u4e5f\u53ef\u4ee5\u5728\u6700\u540e\u65b0\u589e\u884c\u3002\u4fdd\u5b58\u540e\u4f1a\u5199\u56de\u7ebf\u4e0a sku_cost.xlsx\u3002")\n'
batch_block = '''
    batch_cols = st.columns([1, 1, 1, 1.2])
    with batch_cols[0]:
        batch_column = st.selectbox("\u6279\u91cf\u5b57\u6bb5", ["\u5feb\u9012\u8d39", "\u5355\u4ef6\u8d27\u4ef7"])
    with batch_cols[1]:
        batch_value = st.number_input("\u6279\u91cf\u91d1\u989d", min_value=0.0, value=3.7, step=0.1, format="%.2f")
    with batch_cols[2]:
        batch_scope = st.selectbox("\u586b\u5199\u8303\u56f4", ["\u53ea\u586b\u7a7a\u503c", "\u8986\u76d6\u5f53\u524d\u7b5b\u9009"])
    with batch_cols[3]:
        st.write("")
        st.write("")
        if st.button("\u6279\u91cf\u586b\u5199\u5f53\u524d\u7b5b\u9009", type="secondary", width="stretch"):
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
                        f"\u5df2\u6279\u91cf\u586b\u5199 {changed} \u884c {batch_column}\u3002"
                        + (f" \u65e7\u6587\u4ef6\u5907\u4efd\uff1a{backup_path.name}" if backup_path else "")
                    )
                    st.rerun()
'''
if '\u6279\u91cf\u586b\u5199\u5f53\u524d\u7b5b\u9009' not in text and '????????' not in text:
    text = text.replace(caption, caption + batch_block + '\n', 1)

p.write_text(text, encoding='utf-8')
PY
python3 -m py_compile app.py
sudo systemctl restart tmall-dashboard.service
sleep 2
systemctl is-active tmall-dashboard.service
python3 - <<'PY'
from pathlib import Path
text = Path('/opt/tmall-dashboard/current/app.py').read_text(encoding='utf-8')
print('shared_cost_path=', 'DATA_DIR / "sku_cost.xlsx"' in text)
print('batch_fill=', '????????' in text)
print('guohuo_store=', '????' in text)
PY

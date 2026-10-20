import pandas as pd
from pathlib import Path
map_path=Path('D:/qianniu_profit_crawler/outputs/category_mapping_server.xlsx')
if not map_path.exists():
    map_path=Path('D:/qianniu_profit_crawler/data/category_mapping.xlsx')
print('map_path', map_path, map_path.exists())
if map_path.exists():
    m=pd.read_excel(map_path,dtype=str).fillna('')
    print('rows',len(m))
    for col in ['一级品类','二级品类','商家编码','商品ID','店铺']:
        if col in m.columns:
            hit=m[m[col].astype(str).str.contains('405', na=False)]
            print('hit',col,len(hit))
            if len(hit): print(hit[['店铺','商品ID','商家编码','商品简称','一级品类','二级品类','是否启用']].head(30).to_string(index=False))
    print('category counts')
    print(m.groupby('一级品类').size().to_string())

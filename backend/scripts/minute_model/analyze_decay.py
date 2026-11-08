"""信号衰减分析：按模型重跑全截面预测，输出月度 IC、4h/5h 对照、Top20 收益。

用法（容器内，cwd=/，PYTHONPATH=/app）：
  docker exec -i -w / -e PYTHONPATH=/app quantmind python - < backend/scripts/minute_model/analyze_decay.py
输出：data/minute_model/model/decay_monthly.csv、decay_daily.csv
"""
import sys, json, pickle
sys.path.insert(0,'/app')
import duckdb, numpy as np, pandas as pd
from pathlib import Path
PANEL=Path('data/minute_model/panel'); OUT=Path('data/minute_model/model')
m=pickle.load(open(OUT/'lgb_4h_cat_live5.pkl','rb')); model, feats = m['model'], m['features']
con=duckdb.connect(); con.execute("SET memory_limit='8GB'")
days=sorted(p.name.split('=')[1] for p in PANEL.glob('date=*')
            if '20250701'<=p.name.split('=')[1]<='20261029' and (p/'data.parquet').exists())
out=[]
for i,d in enumerate(days,1):
    df=con.sql(f"SELECT * FROM read_parquet('{PANEL}/date={d}/data.parquet') WHERE cutoff='14:50'").df()
    df=df[df['label_ok']&df['tradable']&df['ret_4h'].notna()&df['ret_5h'].notna()]
    if len(df)<200: continue
    df['cutoff_idx']=4
    for c in feats:
        if c not in df.columns: df[c]=np.nan
    df['pred']=model.predict(df[feats])
    df['date']=str(d)
    out.append(df[['date','pred','ret_4h','ret_5h','ret_today_sofar']])
p=pd.concat(out,ignore_index=True); con.register('p',p)
ic=con.sql("""
 WITH r AS (
   SELECT date, pred, ret_4h, ret_5h,
     rank() OVER (PARTITION BY date ORDER BY pred) rp,
     rank() OVER (PARTITION BY date ORDER BY ret_4h) r4,
     rank() OVER (PARTITION BY date ORDER BY ret_5h) r5
   FROM p)
 SELECT date, corr(rp,r4) ic4, corr(rp,r5) ic5 FROM r GROUP BY 1
""").df()
top=con.sql("""
 WITH r AS (SELECT *, row_number() OVER (PARTITION BY date ORDER BY pred DESC) rn FROM p)
 SELECT date, avg(CASE WHEN rn<=20 THEN ret_4h END)*100 top20, avg(CASE WHEN rn<=20 THEN ret_5h END)*100 top20_5h FROM r GROUP BY 1
""").df()
d=ic.merge(top,on='date'); d['ym']=d['date'].str[:6]
mon=d.groupby('ym').agg(ic4=('ic4','mean'),ic5=('ic5','mean'),top20=('top20','mean'),top20_5h=('top20_5h','mean'),n=('date','size')).reset_index()
print(mon.round(4).to_string(index=False))
mon.to_csv('/data/minute_model/model/decay_monthly.csv',index=False)
d.to_csv('/data/minute_model/model/decay_daily.csv',index=False)
# 最近 20 日滚动 IC 趋势
d['ic4_20']=d['ic4'].rolling(20).mean()
print('\n最近5日滚动20日IC:', d['ic4_20'].tail(5).round(4).tolist())
print('IC4 全期均值 %.4f, IC5 全期均值 %.4f'%(d['ic4'].mean(),d['ic5'].mean()))

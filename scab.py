import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta, date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import japanize_matplotlib # 日本語文字化け対策
import matplotlib.patches as mpatches
import matplotlib.dates as mdates

# --- 地点プリセット ---
LOCATIONS = {
    "西之表市（種子島）": (30.73, 131.00),
    "長島町":            (32.18, 130.12),
    "鹿屋市（大隅）":    (31.38, 130.85),
    "南さつま市":        (31.41, 130.32),
    "伊仙町（徳之島）":  (27.68, 128.93),
    "知名町（沖永良部）":(27.38, 128.59),
    "カスタム入力":      None,
}

# --- リスク閾値プリセット ---
THRESHOLD_HIGH_DEFAULT = 30
THRESHOLD_MED_DEFAULT  = 80

st.set_page_config(page_title="そうか病 感染リスク判定・期間分析", layout="wide")
st.title("🌱 そうか病 感染リスク判定システム")
st.markdown("""
マルチ栽培を前提とし、植え付け日からの積算温度で塊茎の初期肥大期を推定します。  
**積算温度が設定GDD閾値**の期間を「感染リスク期」とし、その間の降水量でリスクを判定します。
""")

# ========== サイドバー ==========
st.sidebar.header("🗺️ 分析モードと地点")

analysis_mode = st.sidebar.radio(
    "分析モードを選択",
    ["単一日の判定", "植え付け期間分析"]
)

loc_name = st.sidebar.selectbox("地点を選択", list(LOCATIONS.keys()))

if LOCATIONS[loc_name] is None:
    lat = st.sidebar.number_input("緯度", value=30.73, format="%.4f")
    lon = st.sidebar.number_input("経度", value=131.00, format="%.4f")
else:
    lat, lon = LOCATIONS[loc_name]
    st.sidebar.caption(f"緯度: {lat}  経度: {lon}")

st.sidebar.divider()
st.sidebar.header("🌱 栽培パラメータ")

if analysis_mode == "単一日の判定":
    planting_date = st.sidebar.date_input(
        "植え付け日",
        date(2025, 9, 30) # 単一日判定のデフォルトも更新
    )
    analysis_end_date = None
else:
    # --- ご指定の日付を初期値に設定 ---
    default_start = date(2025, 9, 30)
    default_end = date(2026, 1, 1)
    
    planting_period = st.sidebar.date_input(
        "植え付け分析期間（開始日〜終了日）",
        (default_start, default_end),
        help="分析したい植え付け日の範囲を選択してください。"
    )
    
    if isinstance(planting_period, tuple) and len(planting_period) == 2:
        planting_date, analysis_end_date = planting_period
    else:
        planting_date = planting_period[0] if isinstance(planting_period, list) else planting_period
        analysis_end_date = planting_date
        st.sidebar.warning("期間（終了日）を選択してください。")

base_temp = st.sidebar.number_input(
    "ベース温度 (℃)",
    min_value=0.0, max_value=15.0, value=0.0, step=0.5
)

st.sidebar.divider()
st.sidebar.header("⚙️ GDD閾値")

gdd_start = st.sidebar.number_input("開始 GDD", value=300, step=10)
gdd_end = st.sidebar.number_input("終了 GDD", value=600, step=10)

st.sidebar.divider()
st.sidebar.header("🌧️ リスク判定閾値 (mm)")
threshold_high = st.sidebar.number_input("高リスク上限", value=THRESHOLD_HIGH_DEFAULT)
threshold_med = st.sidebar.number_input("中リスク上限", value=THRESHOLD_MED_DEFAULT)

# ========== データ取得・計算ロジック (省略せず統合) ==========

DAILY_PARAMS = "temperature_2m_mean,precipitation_sum"

@st.cache_data(ttl=259200)
def _fetch_archive(lat, lon, start, end):
    url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}&timezone=Asia%2FTokyo&start_date={start}&end_date={end}"
    r = requests.get(url, timeout=15)
    return r.json()

@st.cache_data(ttl=21600)
def _fetch_forecast(lat, lon, start, end):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}&timezone=Asia%2FTokyo&start_date={start}&end_date={end}"
    r = requests.get(url, timeout=15)
    return r.json()

def fetch_weather_data(lat, lon, start_date, end_analysis_date=None):
    fetch_end_date = (end_analysis_date if end_analysis_date else start_date) + timedelta(days=150)
    today = date.today()
    cutoff_arc = today - timedelta(days=5)
    
    frames = []
    # 過去
    if start_date <= cutoff_arc:
        arc_end = min(cutoff_arc, fetch_end_date)
        data = _fetch_archive(lat, lon, start_date.strftime('%Y-%m-%d'), arc_end.strftime('%Y-%m-%d'))
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))
    # 予報
    fcast_start = max(start_date, cutoff_arc + timedelta(days=1))
    fcast_end = min(fetch_end_date, today + timedelta(days=15))
    if fcast_start <= fcast_end:
        data = _fetch_forecast(lat, lon, fcast_start.strftime('%Y-%m-%d'), fcast_end.strftime('%Y-%m-%d'))
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))

    if not frames: raise ValueError("気象データが見つかりません。")
    df = pd.concat(frames).drop_duplicates('time').sort_values('time')
    df['time'] = pd.to_datetime(df['time'])
    return df

def calculate_scab_risk(p_date, weather_df, b_temp, g_start, g_end, t_high, t_med):
    df_after = weather_df[weather_df['time'] >= pd.Timestamp(p_date)].copy()
    if df_after.empty: return None
    df_after['gdd_daily'] = (df_after['temperature_2m_mean'].fillna(0) - b_temp).clip(lower=0)
    df_after['gdd_cum'] = df_after['gdd_daily'].cumsum()
    
    start_w = df_after[df_after['gdd_cum'] >= g_start]
    if start_w.empty: return {'status': 'GDD未到達', 'planting_date': p_date}

    start_date_w = start_w.iloc[0]['time']
    end_w = df_after[df_after['gdd_cum'] >= g_end]
    reached_end = not end_w.empty
    end_date_w = end_w.iloc[0]['time'] if reached_end else df_after['time'].iloc[-1]
    
    risk_df = df_after[(df_after['time'] >= start_date_w) & (df_after['time'] <= end_date_w)]
    total_precip = risk_df['precipitation_sum'].sum()

    if total_precip < t_high: risk_l, risk_c, risk_v = "高 (High)", "#FF4B4B", 2
    elif total_precip < t_med: risk_l, risk_c, risk_v = "中 (Medium)", "#FFA500", 1
    else: risk_l, risk_c, risk_v = "低 (Low)", "#0068C9", 0

    return {
        'status': '判定完了', 'planting_date': p_date, 'start_date_w': start_date_w, 'end_date_w': end_date_w,
        'reached_end': reached_end, 'total_precip': total_precip, 'risk_level': risk_l, 'risk_color': risk_c,
        'risk_value': risk_v, 'risk_df': risk_df, 'plot_df': df_after[df_after['time'] <= end_date_w]
    }

# ========== 実行処理 ==========
if st.sidebar.button("▶ リスク分析を実行", type="primary"):
    try:
        weather_df = fetch_weather_data(lat, lon, planting_date, analysis_end_date)
        
        if analysis_mode == "単一日の判定":
            res = calculate_scab_risk(planting_date, weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med)
            if res['status'] == '判定完了':
                st.subheader(f"📊 判定結果: {res['risk_level']}")
                st.metric("リスク期 降水量", f"{res['total_precip']:.1f} mm")
                
                fig, ax = plt.subplots(figsize=(10, 4))
                ax.bar(res['plot_df']['time'], res['plot_df']['precipitation_sum'], color="skyblue")
                ax.set_title(f"降水量の推移 (植え付け: {planting_date})")
                st.pyplot(fig)
            else:
                st.warning("指定日のデータが不足しているか、リスク期に達していません。")

        else:
            # 期間分析
            results = []
            date_range = pd.date_range(planting_date, analysis_end_date)
            prog = st.progress(0)
            for i, d in enumerate(date_range):
                r = calculate_scab_risk(d.date(), weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med)
                if r: results.append(r)
                prog.progress((i+1)/len(date_range))
            
            res_df = pd.DataFrame(results)
            valid_df = res_df[res_df['status'] == '判定完了'].copy()
            
            if not valid_df.empty:
                st.subheader("📈 植え付け日別のリスク推移")
                fig, ax = plt.subplots(figsize=(12, 5))
                # 日付を数値に変換してプロット
                ax.scatter(valid_df['planting_date'], valid_df['total_precip'], c=valid_df['risk_color'], s=100)
                ax.plot(valid_df['planting_date'], valid_df['total_precip'], color="gray", alpha=0.3)
                ax.axhline(threshold_high, color="#FF4B4B", linestyle="--", label="高リスク境界")
                ax.set_ylabel("リスク期の積算降水量(mm)")
                ax.set_xlabel("植え付け日")
                plt.xticks(rotation=45)
                st.pyplot(fig)
                
                with st.expander("📋 詳細データ"):
                    # ここで .dt エラーを回避するため pd.to_datetime を適用
                    show_df = valid_df[['planting_date', 'total_precip', 'risk_level']].copy()
                    show_df['planting_date'] = pd.to_datetime(show_df['planting_date']).dt.strftime('%Y/%m/%d')
                    st.write(show_df)
            else:
                st.error("分析期間内に判定可能なデータがありませんでした。")
                
    except Exception as e:
        st.error(f"エラーが発生しました: {e}")

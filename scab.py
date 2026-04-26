import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta, date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import japanize_matplotlib # 文字化け対策
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
        date(2025, 9, 30) # 初期値
    )
    analysis_end_date = None
else:
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

# ========== データ取得関数 ==========
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
    if start_date <= cutoff_arc:
        arc_end = min(cutoff_arc, fetch_end_date)
        data = _fetch_archive(lat, lon, start_date.strftime('%Y-%m-%d'), arc_end.strftime('%Y-%m-%d'))
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))
    
    fcast_start = max(start_date, cutoff_arc + timedelta(days=1))
    fcast_end = min(fetch_end_date, today + timedelta(days=15))
    if fcast_start <= fcast_end:
        data = _fetch_forecast(lat, lon, fcast_start.strftime('%Y-%m-%d'), fcast_end.strftime('%Y-%m-%d'))
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))

    if not frames: raise ValueError("気象データが見つかりません。")
    df = pd.concat(frames).drop_duplicates('time').sort_values('time')
    df['time'] = pd.to_datetime(df['time'])
    return df

# ========== リスク計算関数 ==========
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

# ========== 期間分析用のグラフ関数（復元） ==========
def plot_period_analysis(results_df, t_high, t_med):
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#1a1d24")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444")

    if results_df.empty:
        ax.text(0.5, 0.5, "データがありません", color="white", fontsize=15, ha='center', va='center')
        return fig

    df_plot = results_df[results_df['status'] == '判定完了'].copy()
    if df_plot.empty:
        ax.text(0.5, 0.5, "判定を完了した日がありません", color="white", fontsize=15, ha='center', va='center')
        return fig

    p_dates_num = matplotlib.dates.date2num(df_plot['planting_date'])

    # 散布図と折れ線
    ax.scatter(p_dates_num, df_plot['total_precip'], c=df_plot['risk_color'], s=50, edgecolors='white', linewidths=0.5, zorder=3)
    ax.plot(p_dates_num, df_plot['total_precip'], color="white", alpha=0.3, linestyle="-", linewidth=1.5, zorder=2)

    # 閾値の点線（復元）
    ax.axhline(t_high, color="#FF4B4B", linestyle=":", linewidth=1.5, alpha=0.8)
    ax.axhline(t_med,  color="#FFA500",  linestyle=":", linewidth=1.5, alpha=0.8)
    ax.text(p_dates_num[0], t_high + 2, f"高リスク境界 {t_high}mm", color="#FF4B4B", fontsize=9)
    ax.text(p_dates_num[0], t_med + 2,  f"中リスク境界 {t_med}mm",  color="#FFA500",  fontsize=9)

    ax.set_ylabel("リスク期内の積算降水量 (mm)", color="white")
    ax.set_xlabel("植え付け日", color="white")
    ax.yaxis.label.set_color("white")
    ax.xaxis.label.set_color("white")

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y/%m/%d'))
    fig.autofmt_xdate()

    # 凡例
    high_patch = mpatches.Patch(color="#FF4B4B", label="高リスク (High)")
    med_patch  = mpatches.Patch(color="#FFA500", label="中リスク (Medium)")
    low_patch  = mpatches.Patch(color="#0068C9", label="低リスク (Low)")
    ax.legend(handles=[high_patch, med_patch, low_patch], loc="best", facecolor="#1a1d24", labelcolor="white")

    plt.tight_layout()
    return fig

# ========== 実行処理 ==========
if st.sidebar.button("▶ リスク分析を実行", type="primary"):
    with st.spinner("気象データを取得・解析中..."):
        try:
            weather_df = fetch_weather_data(lat, lon, planting_date, analysis_end_date)
        except Exception as e:
            st.error(f"気象データ取得エラー: {e}")
            st.stop()

    if analysis_mode == "単一日の判定":
        res = calculate_scab_risk(planting_date, weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med)
        if res is None or res['status'] != '判定完了':
            st.warning("指定日のデータが不足しているか、リスク期に達していません。")
            st.stop()
            
        st.subheader(f"📊 判定結果（植え付け日: {planting_date.strftime('%Y/%m/%d')}）")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("リスク期 開始", res['start_date_w'].strftime('%Y/%m/%d'), f"{gdd_start} GDD")
        col2.metric("リスク期 終了", res['end_date_w'].strftime('%Y/%m/%d'), f"{gdd_end} GDD" if res['reached_end'] else "進行中")
        col3.metric("期間中の積算降水量", f"{res['total_precip']:.1f} mm")
        col4.metric("ベース温度", f"{base_temp} ℃")

        st.markdown(f"""
        <div style="background-color:{res['risk_color']}18; border-left:5px solid {res['risk_color']}; padding:15px; border-radius:5px; margin-top:10px;">
            <h3 style="color:{res['risk_color']}; margin:0;">リスクレベル: {res['risk_level']}</h3>
            <p style="margin-top:8px; font-size:15px;">期間中の積算降水量: {res['total_precip']:.1f} mm</p>
        </div>
        """, unsafe_allow_html=True)

        # グラフ描画（リッチバージョン復元）
        st.subheader("📈 気象データの推移（リスク期を強調表示）")
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        fig.patch.set_facecolor("#0e1117")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1a1d24")
            ax.tick_params(colors="white")
            for spine in ax.spines.values():
                spine.set_color("#444")

        risk_start_num = matplotlib.dates.date2num(res['start_date_w'])
        risk_end_num   = matplotlib.dates.date2num(res['end_date_w'])

        def highlight_risk(ax, color_code):
            ax.axvspan(risk_start_num, risk_end_num, color=color_code, alpha=0.15, label="感染リスク期")
            ax.axvline(risk_start_num, color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.axvline(risk_end_num,   color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)

        # 上段: 積算GDD
        ax1.plot(res['plot_df']['time'], res['plot_df']['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
        ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7)
        ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7)
        highlight_risk(ax1, res['risk_color'])
        ax1.set_ylabel(f"積算温度 (℃·day)", color="white")
        ax1.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")
        ax1.yaxis.label.set_color("white")
        
        # 下段: 日降水量
        colors_bar = [res['risk_color'] if (res['start_date_w'] <= t <= res['end_date_w']) else "#4a90d9"
                      for t in res['plot_df']['time']]
        ax2.bar(res['plot_df']['time'], res['plot_df']['precipitation_sum'].fillna(0), color=colors_bar, width=0.8, alpha=0.85)
        highlight_risk(ax2, res['risk_color'])
        
        # 積算降水量ライン
        if not res['risk_df'].empty:
            cum_precip_df = res['risk_df'].copy()
            cum_precip_df['cum_precip'] = cum_precip_df['precipitation_sum'].fillna(0).cumsum()
            ax2b = ax2.twinx()
            ax2b.plot(cum_precip_df['time'], cum_precip_df['cum_precip'], color="white", linewidth=1.5, linestyle="-", alpha=0.8, label="リスク期積算降水量")
            ax2b.set_ylabel("リスク期積算降水量 (mm)", color="white")
            ax2b.yaxis.label.set_color("white")
            ax2b.tick_params(colors="white")
            for spine in ax2b.spines.values():
                spine.set_color("#444")
            ax2b.axhline(threshold_high, color="#FF4B4B", linestyle=":", linewidth=1.2, alpha=0.7)
            ax2b.axhline(threshold_med,  color="#FFA500",  linestyle=":", linewidth=1.2, alpha=0.7)

        ax2.set_ylabel("日降水量 (mm)", color="white")
        ax2.set_xlabel("日付", color="white")
        fig.autofmt_xdate()
        plt.tight_layout()
        st.pyplot(fig)

    else:
        # 期間分析の実行
        results_list = []
        current_p_date = planting_date
        date_list = []
        while current_p_date <= analysis_end_date:
            date_list.append(current_p_date)
            current_p_date += timedelta(days=1)
        
        bar = st.progress(0)
        for i, p_date in enumerate(date_list):
            res = calculate_scab_risk(p_date, weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med)
            if res: results_list.append(res)
            if i % 5 == 0: bar.progress((i + 1) / len(date_list), text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()
        
        results_df = pd.DataFrame(results_list)
        if results_df.empty:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()
            
        st.subheader("📈 植え付け日による感染リスクの変化")
        fig_period = plot_period_analysis(results_df, threshold_high, threshold_med)
        st.pyplot(fig_period)
        
        with st.expander("📋 分析結果の詳細データテーブル"):
            show_df = results_df[results_df['status'] == '判定完了'][['planting_date', 'total_precip', 'risk_level', 'start_date_w', 'end_date_w']].copy()
            show_df['planting_date'] = pd.to_datetime(show_df['planting_date']).dt.strftime('%Y/%m/%d')
            show_df['start_date_w'] = pd.to_datetime(show_df['start_date_w']).dt.strftime('%Y/%m/%d')
            show_df['end_date_w'] = pd.to_datetime(show_df['end_date_w']).dt.strftime('%Y/%m/%d')
            show_df.columns = ['植え付け日', 'リスク期 降水量(mm)', 'リスクレベル', 'リスク期 開始日', 'リスク期 終了日']
            st.dataframe(show_df, use_container_width=True)

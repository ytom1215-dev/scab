import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
import os

# --- 日本語フォント設定 ---
def get_jp_font():
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return fm.FontProperties(fname=path)
    return fm.FontProperties()

JP_FONT = get_jp_font()

# --- 地点プリセット（ご指定の産地に更新） ---
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

st.set_page_config(page_title="そうか病 感染リスク判定", layout="wide")
st.title("🌱 そうか病 感染リスク判定システム")
st.markdown("""
マルチ栽培を前提とし、植え付け日からの積算温度（ベース温度可変）で塊茎の初期肥大期を推定します。  
**積算温度が設定GDD閾値（デフォルト: 300〜600度日）**の期間を「感染リスク期」とし、その間の降水量でリスクを判定します。
""")

# ========== サイドバー ==========
st.sidebar.header("📍 観測地点")

loc_name = st.sidebar.selectbox("地点を選択", list(LOCATIONS.keys()))

if LOCATIONS[loc_name] is None:
    lat = st.sidebar.number_input("緯度", value=30.73, format="%.4f")
    lon = st.sidebar.number_input("経度", value=131.00, format="%.4f")
else:
    lat, lon = LOCATIONS[loc_name]
    st.sidebar.caption(f"緯度: {lat}  経度: {lon}")

st.sidebar.divider()
st.sidebar.header("🌱 栽培パラメータ")

planting_date = st.sidebar.date_input(
    "植え付け日",
    datetime.today() - timedelta(days=40)
)

base_temp = st.sidebar.number_input(
    "ベース温度 (℃)",
    min_value=0.0, max_value=15.0, value=0.0, step=0.5,
    help="GDD計算の基準温度。種子島マルチ栽培では0℃が標準。"
)

st.sidebar.divider()
st.sidebar.header("⚙️ GDD閾値（感染リスク期）")

gdd_start = st.sidebar.number_input(
    "リスク期 開始 GDD (度日)",
    min_value=50, max_value=500, value=300, step=10,
    help="塊茎肥大開始の目安。通常300度日。"
)
gdd_end = st.sidebar.number_input(
    "リスク期 終了 GDD (度日)",
    min_value=100, max_value=1000, value=600, step=10,
    help="塊茎肥大ピーク終了の目安。通常600度日。"
)

if gdd_end <= gdd_start:
    st.sidebar.error("終了GDDは開始GDDより大きくしてください。")
    st.stop()

st.sidebar.divider()
st.sidebar.header("🌧️ リスク判定閾値 (mm)")

threshold_high = st.sidebar.number_input(
    "高リスク上限 (mm未満)",
    min_value=1, max_value=200, value=THRESHOLD_HIGH_DEFAULT, step=5,
    help="この降水量未満なら高リスク"
)
threshold_med = st.sidebar.number_input(
    "中リスク上限 (mm未満)",
    min_value=10, max_value=500, value=THRESHOLD_MED_DEFAULT, step=5,
    help="この降水量未満なら中リスク"
)

if threshold_med <= threshold_high:
    st.sidebar.error("中リスク閾値は高リスク閾値より大きくしてください。")
    st.stop()

# ========== データ取得 (API節約の要) ==========
DAILY_PARAMS = "temperature_2m_mean,precipitation_sum"

# 過去データは確定値のため、長期間（3日間 = 259200秒）キャッシュしてAPIコールを極限まで減らす
@st.cache_data(ttl=259200)
def _fetch_archive(lat, lon, start, end):
    """過去データ: Open-Meteo Historical Weather API (ERA5)"""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&daily={DAILY_PARAMS}"
        f"&timezone=Asia%2FTokyo"
        f"&start_date={start.strftime('%Y-%m-%d')}"
        f"&end_date={end.strftime('%Y-%m-%d')}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

# 予報データは変わる可能性があるため、6時間（21600秒）ごとに再取得
@st.cache_data(ttl=21600)
def _fetch_forecast(lat, lon, start, end):
    """予報データ: Open-Meteo Forecast API（最大16日先）"""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily={DAILY_PARAMS}"
        f"&timezone=Asia%2FTokyo"
        f"&start_date={start.strftime('%Y-%m-%d')}"
        f"&end_date={end.strftime('%Y-%m-%d')}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_weather_data(lat, lon, start_date):
    """データを結合するラッパー関数（この関数自体はキャッシュせず、内部API呼び出しでキャッシュを効かせる）"""
    end_date   = start_date + timedelta(days=120)
    today      = datetime.today().date()
    cutoff_arc = today - timedelta(days=5)
    cutoff_arc = min(cutoff_arc, end_date)

    frames = []
    # --- 過去分 (archive) ---
    if start_date <= cutoff_arc:
        arc_end = min(cutoff_arc, end_date)
        data = _fetch_archive(lat, lon, start_date, arc_end)
        frames.append(pd.DataFrame(data['daily']))

    # --- 未来分 (forecast) ---
    fcast_start = cutoff_arc + timedelta(days=1)
    fcast_end   = min(end_date, today + timedelta(days=15))
    if fcast_start <= fcast_end:
        data = _fetch_forecast(lat, lon, fcast_start, fcast_end)
        frames.append(pd.DataFrame(data['daily']))

    if not frames:
        raise ValueError("取得できる期間がありません。植え付け日を確認してください。")

    df = pd.concat(frames, ignore_index=True)
    df['time'] = pd.to_datetime(df['time'])
    df = df.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    return df

# ========== 実行ボタン ==========
if st.sidebar.button("▶ リスク判定を実行", type="primary"):
    with st.spinner("気象データを取得・解析中..."):
        try:
            df = fetch_weather_data(lat, lon, planting_date)
        except Exception as e:
            st.error(f"気象データ取得エラー: {e}")
            st.stop()

    # --- GDD計算 ---
    df['gdd_daily'] = (df['temperature_2m_mean'].fillna(0) - base_temp).clip(lower=0)
    df['gdd_cum']   = df['gdd_daily'].cumsum()

    # --- リスク期の特定 ---
    start_w = df[df['gdd_cum'] >= gdd_start]
    end_w   = df[df['gdd_cum'] >= gdd_end]

    if start_w.empty:
        st.warning(f"まだ積算温度が {gdd_start} 度日に達していません。")
        st.stop()

    start_date_w = start_w.iloc[0]['time']
    reached_end  = not end_w.empty
    end_date_w   = end_w.iloc[0]['time'] if reached_end else df['time'].iloc[-1]

    if not reached_end:
        st.info(f"※ 積算温度 {gdd_end} 度日に向けて進行中（最新データ: {end_date_w.strftime('%Y/%m/%d')}）")

    # --- 期間内降水量 ---
    risk_df = df[(df['time'] >= start_date_w) & (df['time'] <= end_date_w)]
    total_precip = risk_df['precipitation_sum'].sum()

    # --- リスク判定 ---
    if total_precip < threshold_high:
        risk_level = "高 (High)"
        risk_color = "#FF4B4B"
        msg = f"土壌が極度に乾燥（{total_precip:.1f} mm < {threshold_high} mm）。そうか病の感染リスクが非常に高い状態です。"
    elif total_precip < threshold_med:
        risk_level = "中 (Medium)"
        risk_color = "#FFA500"
        msg = f"やや乾燥気味（{total_precip:.1f} mm < {threshold_med} mm）。今後の天候次第でリスクが高まる可能性があります。"
    else:
        risk_level = "低 (Low)"
        risk_color = "#0068C9"
        msg = f"十分な降水量（{total_precip:.1f} mm）。拮抗菌が優占しやすい状態です。"

    # ========== 結果表示 ==========
    st.subheader("📊 判定結果")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("リスク期 開始", start_date_w.strftime('%Y/%m/%d'), f"{gdd_start} GDD")
    col2.metric("リスク期 終了", end_date_w.strftime('%Y/%m/%d'), f"{gdd_end} GDD" if reached_end else "進行中")
    col3.metric("期間中の積算降水量", f"{total_precip:.1f} mm")
    col4.metric("ベース温度", f"{base_temp} ℃")

    st.markdown(f"""
    <div style="background-color:{risk_color}18; border-left:5px solid {risk_color}; padding:15px; border-radius:5px; margin-top:10px;">
        <h3 style="color:{risk_color}; margin:0;">リスクレベル: {risk_level}</h3>
        <p style="margin-top:8px; font-size:15px;">{msg}</p>
    </div>
    """, unsafe_allow_html=True)

    # ========== グラフ ==========
    st.subheader("📈 気象データの推移（リスク期を強調表示）")

    plot_df = df[(df['time'] >= pd.Timestamp(planting_date)) & (df['time'] <= end_date_w)].copy()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    fig.patch.set_facecolor("#0e1117")
    for ax in (ax1, ax2):
        ax.set_facecolor("#1a1d24")
        ax.tick_params(colors="white")
        for spine in ax.spines.values():
            spine.set_color("#444")

    risk_start_num = matplotlib.dates.date2num(start_date_w)
    risk_end_num   = matplotlib.dates.date2num(end_date_w)

    def highlight_risk(ax):
        ax.axvspan(risk_start_num, risk_end_num,
                   color=risk_color, alpha=0.15, label="感染リスク期")
        ax.axvline(risk_start_num, color=risk_color, linestyle="--", linewidth=1.2, alpha=0.8)
        ax.axvline(risk_end_num,   color=risk_color, linestyle="--", linewidth=1.2, alpha=0.8)

    # --- 上段: 積算GDD ---
    ax1.plot(plot_df['time'], plot_df['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
    ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7)
    ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7)
    highlight_risk(ax1)
    ax1.set_ylabel(f"積算温度 (℃·day, ベース{base_temp}℃)", color="white", fontproperties=JP_FONT)
    ax1.legend(loc="upper left", prop=JP_FONT, facecolor="#1a1d24", labelcolor="white")
    ax1.yaxis.label.set_color("white")

    # GDDラベル
    ax1.text(plot_df['time'].iloc[0], gdd_start + 5, f"{gdd_start} GDD",
             color="#ffcc00", fontsize=8, fontproperties=JP_FONT)
    ax1.text(plot_df['time'].iloc[0], gdd_end + 5, f"{gdd_end} GDD",
             color="#ff8800", fontsize=8, fontproperties=JP_FONT)

    # --- 下段: 日降水量 ---
    colors_bar = [risk_color if (start_date_w <= t <= end_date_w) else "#4a90d9"
                  for t in plot_df['time']]
    ax2.bar(plot_df['time'], plot_df['precipitation_sum'].fillna(0),
            color=colors_bar, width=0.8, alpha=0.85)
    highlight_risk(ax2)

    # 積算降水量（リスク期）を重ね表示
    risk_plot = plot_df[(plot_df['time'] >= start_date_w) & (plot_df['time'] <= end_date_w)].copy()
    if not risk_plot.empty:
        risk_plot['cum_precip'] = risk_plot['precipitation_sum'].fillna(0).cumsum()
        ax2b = ax2.twinx()
        ax2b.plot(risk_plot['time'], risk_plot['cum_precip'],
                  color="white", linewidth=1.5, linestyle="-", alpha=0.6, label="リスク期積算降水量")
        ax2b.set_ylabel("リスク期積算降水量 (mm)", color="white", fontproperties=JP_FONT)
        ax2b.yaxis.label.set_color("white")
        ax2b.tick_params(colors="white")
        for spine in ax2b.spines.values():
            spine.set_color("#444")
        # 閾値ライン
        ax2b.axhline(threshold_high, color="#FF4B4B", linestyle=":", linewidth=1.2, alpha=0.7)
        ax2b.axhline(threshold_med,  color="#FFA500",  linestyle=":", linewidth=1.2, alpha=0.7)
        ax2b.text(risk_plot['time'].iloc[-1], threshold_high, f"高リスク境界 {threshold_high}mm",
                  color="#FF4B4B", fontsize=8, ha="right", fontproperties=JP_FONT)
        ax2b.text(risk_plot['time'].iloc[-1], threshold_med, f"中リスク境界 {threshold_med}mm",
                  color="#FFA500", fontsize=8, ha="right", fontproperties=JP_FONT)

    ax2.set_ylabel("日降水量 (mm)", color="white", fontproperties=JP_FONT)
    ax2.set_xlabel("日付", color="white", fontproperties=JP_FONT)
    ax2.yaxis.label.set_color("white")
    ax2.xaxis.label.set_color("white")

    # 凡例パッチ
    risk_patch = mpatches.Patch(color=risk_color, alpha=0.4, label="感染リスク期")
    normal_patch = mpatches.Patch(color="#4a90d9", alpha=0.85, label="その他の期間")
    ax2.legend(handles=[risk_patch, normal_patch], loc="upper left",
               prop=JP_FONT, facecolor="#1a1d24", labelcolor="white")

    fig.autofmt_xdate()
    plt.tight_layout()
    st.pyplot(fig)

    # ========== データテーブル（リスク期） ==========
    with st.expander("📋 リスク期の日別データ"):
        show_df = risk_df[['time', 'temperature_2m_mean', 'precipitation_sum', 'gdd_daily', 'gdd_cum']].copy()
        show_df.columns = ['日付', '平均気温(℃)', '降水量(mm)', '日GDD', '積算GDD']
        show_df['日付'] = show_df['日付'].dt.strftime('%Y/%m/%d')
        show_df = show_df.reset_index(drop=True)
        st.dataframe(show_df.style.format({
            '平均気温(℃)': '{:.1f}',
            '降水量(mm)':  '{:.1f}',
            '日GDD':       '{:.1f}',
            '積算GDD':     '{:.1f}',
        }), use_container_width=True)
import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta, date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import japanize_matplotlib
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import matplotlib.dates as mdates
import io

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

THRESHOLD_HIGH_DEFAULT       = 30
THRESHOLD_MED_DEFAULT        = 80
ANTECEDENT_RELIEF_MM_DEFAULT = 20

st.set_page_config(page_title="そうか病 感染リスク判定・期間分析", layout="wide")
st.title("🌱 そうか病 感染リスク判定システム")
st.markdown("""
マルチ栽培を前提とし、植え付け日からの積算温度で塊茎の初期肥大期を推定します。  
**積算温度が設定GDD閾値**の期間を「感染リスク期」とし、その間の降水量でリスクを判定します。  
⚠️ **判定基準**: そうか病は乾燥条件で感染拡大するため、リスク期の**降水量が少ないほど高リスク**と判定します。  
💧 **先行降水補正**: 植え付け前の降水量が多い場合、初期土壌水分が高いとみなしリスクを1段階軽減します。  
❄️ **低温補正**: リスク期に地温（日平均気温）が低い日が続く場合、病原菌の活動が抑制されるためリスクを1段階軽減します。
""")

# ========== サイドバー ==========
st.sidebar.header("📡 データソース設定")
data_source = st.sidebar.radio(
    "気象データの取得元を選択",
    ["Open-Meteo (API自動取得)", "AMeDAS (テキスト貼り付け)"]
)

pasted_data = None
if data_source == "AMeDAS (テキスト貼り付け)":
    st.sidebar.info("💡 **データの注意**: ExcelやCSVからデータをコピーし、下の枠に貼り付けてください。1行目にヘッダーがあり、列名に「日」「気温」「降水」が含まれるデータを推奨します。")
    pasted_data = st.sidebar.text_area("AMeDASの気象データ (コピペ用)", height=200)

st.sidebar.divider()

st.sidebar.header("🗺️ 分析モードと地点")

analysis_mode = st.sidebar.radio(
    "分析モードを選択",
    ["単一日の判定", "植え付け期間分析", "複数年比較分析"]
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
    planting_date     = st.sidebar.date_input("植え付け日", date(2025, 9, 30))
    analysis_end_date = None
elif analysis_mode == "植え付け期間分析":
    planting_period = st.sidebar.date_input(
        "植え付け分析期間（開始日〜終了日）",
        (date(2025, 9, 30), date(2026, 1, 1)),
        help="分析したい植え付け日の範囲を選択してください。"
    )
    if isinstance(planting_period, tuple) and len(planting_period) == 2:
        planting_date, analysis_end_date = planting_period
    else:
        planting_date     = planting_period[0] if isinstance(planting_period, list) else planting_period
        analysis_end_date = planting_date
        st.sidebar.warning("期間（終了日）を選択してください。")
else:
    # 複数年比較分析
    compare_years = st.sidebar.multiselect(
        "比較する年を選択", 
        list(range(2020, 2030)), 
        default=[2023, 2024, 2025]
    )
    planting_period = st.sidebar.date_input(
        "分析期間（月日）",
        (date(2025, 9, 1), date(2025, 12, 31)),
        help="選択された期間の「月日」のみを使用し、選択した全ての年で比較します。"
    )
    if isinstance(planting_period, tuple) and len(planting_period) == 2:
        start_md_date, end_md_date = planting_period
    else:
        start_md_date = planting_period[0] if isinstance(planting_period, list) else planting_period
        end_md_date = start_md_date
        st.sidebar.warning("期間（終了日）を選択してください。")


base_temp = st.sidebar.number_input(
    "ベース温度 (℃)", min_value=0.0, max_value=15.0, value=0.0, step=0.5
)

st.sidebar.divider()
st.sidebar.header("⚙️ GDD閾値")
gdd_start = st.sidebar.number_input("開始 GDD", value=300, step=10)
gdd_end   = st.sidebar.number_input("終了 GDD", value=600, step=10)

st.sidebar.divider()
st.sidebar.header("🌧️ リスク判定閾値（降水量）")
st.sidebar.caption(
    "⬇️ 降水量が**少ない**ほど感染リスクが高くなります。"
)
threshold_high = st.sidebar.number_input(
    "高リスク上限 (mm) ← これ未満で高リスク",
    value=THRESHOLD_HIGH_DEFAULT
)
threshold_med = st.sidebar.number_input(
    "中リスク上限 (mm) ← これ未満で中リスク",
    value=THRESHOLD_MED_DEFAULT
)
if threshold_high >= threshold_med:
    st.sidebar.error("⚠️ 高リスク上限は中リスク上限より小さい値を設定してください。")

st.sidebar.divider()

# ===== 先行降水量設定 =====
st.sidebar.header("💧 先行降水量補正")
use_antecedent = st.sidebar.checkbox("先行降水量補正を使用する", value=True)
if use_antecedent:
    antecedent_days = st.sidebar.number_input("集計期間（日）", min_value=1, max_value=30, value=7, step=1)
    antecedent_relief_mm = st.sidebar.number_input("軽減閾値 (mm)", min_value=0, max_value=200, value=ANTECEDENT_RELIEF_MM_DEFAULT, step=5)
else:
    antecedent_days      = 7
    antecedent_relief_mm = ANTECEDENT_RELIEF_MM_DEFAULT

st.sidebar.divider()

# ===== 低温補正設定 =====
st.sidebar.header("❄️ 低温補正 (地温考慮)")
use_low_temp = st.sidebar.checkbox(
    "低温補正を使用する", 
    value=True, 
    help="リスク期に日平均気温が低い日が一定数ある場合、病原菌の活動低下を見込んでリスクを1段階軽減します。"
)
if use_low_temp:
    low_temp_threshold = st.sidebar.number_input("低温基準 (℃)", value=10.0, step=0.5, help="この温度以下の日をカウントします")
    low_temp_days = st.sidebar.number_input("軽減に必要な日数 (日)", min_value=1, max_value=30, value=3, step=1, help="リスク期内にこの日数以上、低温基準以下の日があれば補正します")
else:
    low_temp_threshold = 10.0
    low_temp_days = 3


# ========== データ取得・パース ==========
DAILY_PARAMS = "temperature_2m_mean,precipitation_sum"

@st.cache_data(ttl=259200)
def _fetch_archive(lat, lon, start, end):
    url = (f"https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}"
           f"&timezone=Asia%2FTokyo&start_date={start}&end_date={end}")
    return requests.get(url, timeout=15).json()

@st.cache_data(ttl=21600)
def _fetch_forecast(lat, lon, start, end):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}"
           f"&timezone=Asia%2FTokyo&start_date={start}&end_date={end}")
    return requests.get(url, timeout=15).json()

def fetch_weather_data(lat, lon, start_date, end_analysis_date=None, pre_fetch_days=30):
    fetch_start = start_date - timedelta(days=pre_fetch_days)
    fetch_end   = (end_analysis_date if end_analysis_date else start_date) + timedelta(days=150)
    today       = date.today()
    cutoff_arc  = today - timedelta(days=5)

    frames = []
    if fetch_start <= cutoff_arc:
        arc_end = min(cutoff_arc, fetch_end)
        data = _fetch_archive(lat, lon, fetch_start.strftime('%Y-%m-%d'), arc_end.strftime('%Y-%m-%d'))
        if 'daily' in data:
            frames.append(pd.DataFrame(data['daily']))

    fcast_start = max(fetch_start, cutoff_arc + timedelta(days=1))
    fcast_end   = min(fetch_end, today + timedelta(days=15))
    if fcast_start <= fcast_end:
        data = _fetch_forecast(lat, lon, fcast_start.strftime('%Y-%m-%d'), fcast_end.strftime('%Y-%m-%d'))
        if 'daily' in data:
            frames.append(pd.DataFrame(data['daily']))

    if not frames:
        raise ValueError("気象データが見つかりません。")
    df = pd.concat(frames).drop_duplicates('time').sort_values('time')
    df['time'] = pd.to_datetime(df['time'])
    return df

def parse_amedas_text(text_data):
    try:
        if not text_data.strip():
            raise ValueError("データが入力されていません。")

        df = pd.read_csv(io.StringIO(text_data.strip()), sep=None, engine='python')
        
        time_col = None
        temp_col = None
        precip_col = None

        for col in df.columns:
            c_str = str(col).lower()
            if not time_col and any(k in c_str for k in ["年", "月", "日", "time", "date", "日時"]):
                time_col = col
            elif not temp_col and any(k in c_str for k in ["気温", "temp"]):
                temp_col = col
            elif not precip_col and any(k in c_str for k in ["降水", "precip", "雨"]):
                precip_col = col

        if not (time_col and temp_col and precip_col):
            raise ValueError(f"テキストから必要な列を特定できませんでした。現在の列名: {list(df.columns)}")

        df = df.rename(columns={
            time_col: 'time',
            temp_col: 'temperature_2m_mean',
            precip_col: 'precipitation_sum'
        })
        
        df['time'] = pd.to_datetime(df['time'], errors='coerce')
        df['temperature_2m_mean'] = pd.to_numeric(df['temperature_2m_mean'].astype(str).replace(r'[^\d\.-]', '', regex=True), errors='coerce')
        df['precipitation_sum'] = pd.to_numeric(df['precipitation_sum'].astype(str).replace(r'[^\d\.-]', '', regex=True), errors='coerce')
        
        df = df.dropna(subset=['time']).sort_values('time').reset_index(drop=True)
        
        if df.empty:
            raise ValueError("有効な気象データ行が見つかりませんでした。")
            
        return df

    except Exception as e:
        raise Exception(f"テキスト読み込みエラー: {str(e)}")


# ========== リスク計算 ==========
RISK_MAP = {
    2: ("高 (High)",   "#FF4B4B"),
    1: ("中 (Medium)", "#FFA500"),
    0: ("低 (Low)",    "#0068C9"),
}

def calculate_scab_risk(p_date, weather_df, b_temp, g_start, g_end,
                        t_high, t_med, use_ante, ante_days, ante_relief_mm,
                        use_temp, temp_thresh, temp_days):
    df_after = weather_df[weather_df['time'] >= pd.Timestamp(p_date)].copy()
    if df_after.empty:
        return None

    df_after['gdd_daily'] = (df_after['temperature_2m_mean'].fillna(0) - b_temp).clip(lower=0)
    df_after['gdd_cum']   = df_after['gdd_daily'].cumsum()

    start_w = df_after[df_after['gdd_cum'] >= g_start]
    if start_w.empty:
        return {'status': 'GDD未到達', 'planting_date': p_date}

    start_date_w = start_w.iloc[0]['time']
    end_w        = df_after[df_after['gdd_cum'] >= g_end]
    reached_end  = not end_w.empty
    end_date_w   = end_w.iloc[0]['time'] if reached_end else df_after['time'].iloc[-1]

    risk_df      = df_after[(df_after['time'] >= start_date_w) & (df_after['time'] <= end_date_w)]
    total_precip = risk_df['precipitation_sum'].sum()
    
    low_temp_count = (risk_df['temperature_2m_mean'] <= temp_thresh).sum() if not risk_df.empty else 0

    ante_start_ts     = pd.Timestamp(p_date) - timedelta(days=ante_days)
    ante_end_ts       = pd.Timestamp(p_date) - timedelta(days=1)
    ante_df           = weather_df[(weather_df['time'] >= ante_start_ts) & (weather_df['time'] <= ante_end_ts)]
    antecedent_precip = ante_df['precipitation_sum'].sum() if not ante_df.empty else 0.0
    ante_available    = not ante_df.empty

    if total_precip < t_high:
        base_risk_v = 2
    elif total_precip < t_med:
        base_risk_v = 1
    else:
        base_risk_v = 0

    relief_points = 0
    ante_corrected = False
    temp_corrected = False
    
    if use_ante and ante_available and antecedent_precip >= ante_relief_mm:
        relief_points += 1
        ante_corrected = True
        
    if use_temp and low_temp_count >= temp_days:
        relief_points += 1
        temp_corrected = True

    corrected_risk_v = max(0, base_risk_v - relief_points)
    risk_l, risk_c = RISK_MAP[corrected_risk_v]

    return {
        'status':             '判定完了',
        'planting_date':      p_date,
        'start_date_w':       start_date_w,
        'end_date_w':         end_date_w,
        'reached_end':        reached_end,
        'total_precip':       total_precip,
        'antecedent_precip':  antecedent_precip,
        'low_temp_count':     low_temp_count,
        'ante_available':     ante_available,
        'ante_corrected':     ante_corrected,
        'temp_corrected':     temp_corrected,
        'base_risk_value':    base_risk_v,
        'risk_value':         corrected_risk_v,
        'risk_level':         risk_l,
        'risk_color':         risk_c,
        'risk_df':            risk_df,
        'plot_df':            df_after[df_after['time'] <= end_date_w],
    }


# ========== 日付軸ユーティリティ ==========
def apply_date_axis(ax, span_days=None):
    interval = 10
    if span_days is not None:
        if span_days > 150: interval = 20
        elif span_days > 60: interval = 10
        elif span_days > 30: interval = 5
        else: interval = 2

    locator = mdates.DayLocator(interval=interval)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha('right')
        lbl.set_color('white')


# ========== 期間分析グラフ ==========
def plot_period_analysis(results_df, t_high, t_med):
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#1a1d24")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444")

    df_plot = results_df[results_df['status'] == '判定完了'].copy()
    if df_plot.empty:
        ax.text(0.5, 0.5, "判定を完了した日がありません", color="white", fontsize=15, ha='center', va='center')
        return fig

    df_plot['planting_date'] = pd.to_datetime(df_plot['planting_date'])
    span_days = (df_plot['planting_date'].max() - df_plot['planting_date'].min()).days

    ax.scatter(df_plot['planting_date'], df_plot['total_precip'],
               c=df_plot['risk_color'], s=50, edgecolors='white', linewidths=0.5, zorder=3)
    ax.plot(df_plot['planting_date'], df_plot['total_precip'],
            color="white", alpha=0.3, linestyle="-", linewidth=1.5, zorder=2)

    # 補正マーカー
    if 'ante_corrected' in df_plot.columns:
        a_corr = df_plot[df_plot['ante_corrected'] == True]
        if not a_corr.empty:
            ax.scatter(a_corr['planting_date'], a_corr['total_precip'],
                       marker='D', s=100, edgecolors='white', facecolors='none',
                       linewidths=1.2, zorder=4, alpha=0.9)
                       
    if 'temp_corrected' in df_plot.columns:
        t_corr = df_plot[df_plot['temp_corrected'] == True]
        if not t_corr.empty:
            ax.scatter(t_corr['planting_date'], t_corr['total_precip'],
                       marker='s', s=130, edgecolors='cyan', facecolors='none',
                       linewidths=1.5, zorder=5, alpha=0.9)

    x_min = df_plot['planting_date'].min()
    ax.axhline(t_high, color="#FF4B4B", linestyle=":", linewidth=1.5, alpha=0.8)
    ax.axhline(t_med,  color="#FFA500",  linestyle=":", linewidth=1.5, alpha=0.8)
    ax.text(x_min, t_high + 2, f"↑ 高リスク境界 {t_high}mm（以下で高リスク）", color="#FF4B4B", fontsize=9)
    ax.text(x_min, t_med  + 2, f"↑ 中リスク境界 {t_med}mm（以下で中リスク）",  color="#FFA500",  fontsize=9)

    ax.set_ylabel("リスク期内の積算降水量 (mm)  ※少ないほど高リスク", color="white")
    ax.set_xlabel("植え付け日", color="white")
    ax.yaxis.label.set_color("white")
    apply_date_axis(ax, span_days=span_days)

    handles = [
        mpatches.Patch(color="#FF4B4B", label="高リスク (High)：乾燥"),
        mpatches.Patch(color="#FFA500", label="中リスク (Medium)"),
        mpatches.Patch(color="#0068C9", label="低リスク (Low)：湿潤"),
    ]
    if 'ante_corrected' in df_plot.columns and df_plot['ante_corrected'].any():
        handles.append(mlines.Line2D([], [], marker='D', color='white', markerfacecolor='none', markersize=8, label="先行降水量補正あり", linestyle='None'))
    if 'temp_corrected' in df_plot.columns and df_plot['temp_corrected'].any():
        handles.append(mlines.Line2D([], [], marker='s', color='cyan', markerfacecolor='none', markersize=9, label="低温補正あり", linestyle='None'))
        
    ax.legend(handles=handles, loc="best", facecolor="#1a1d24", labelcolor="white")

    plt.tight_layout()
    return fig


# ========== CSV生成 ==========
def build_csv(results_df: pd.DataFrame, ante_days: int) -> bytes:
    cols_src = ['target_year', 'planting_date', 'start_date_w', 'end_date_w', 'reached_end',
                'antecedent_precip', 'ante_corrected', 'low_temp_count', 'temp_corrected',
                'total_precip', 'base_risk_value', 'risk_level']
    avail   = [c for c in cols_src if c in results_df.columns]
    show_df = results_df[results_df['status'] == '判定完了'][avail].copy()

    date_cols = ['planting_date', 'start_date_w', 'end_date_w']
    for c in date_cols:
        if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
    if 'reached_end'        in show_df: show_df['reached_end']        = show_df['reached_end'].map({True: '到達', False: '未到達'})
    if 'ante_corrected'     in show_df: show_df['ante_corrected']     = show_df['ante_corrected'].map({True: '補正あり', False: '-'})
    if 'temp_corrected'     in show_df: show_df['temp_corrected']     = show_df['temp_corrected'].map({True: '補正あり', False: '-'})
    if 'total_precip'       in show_df: show_df['total_precip']       = show_df['total_precip'].round(1)
    if 'antecedent_precip'  in show_df: show_df['antecedent_precip']  = show_df['antecedent_precip'].round(1)

    rename_dict = {
        'target_year':      '対象年',
        'planting_date':    '植え付け日',
        'start_date_w':     'リスク期開始日',
        'end_date_w':       'リスク期終了日',
        'reached_end':      'GDD終了閾値到達',
        'antecedent_precip': f'先行{ante_days}日間降水量(mm)',
        'ante_corrected':   '先行降水補正',
        'low_temp_count':   'リスク期 低温日数(日)',
        'temp_corrected':   '低温補正',
        'total_precip':     'リスク期積算降水量(mm)',
        'base_risk_value':  '基本リスク値(補正前)',
        'risk_level':       'リスクレベル(補正後)',
    }
    show_df.rename(columns=rename_dict, inplace=True)
    return show_df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')


# ========== 実行処理 ==========
if st.sidebar.button("▶ リスク分析を実行", type="primary"):

    if threshold_high >= threshold_med:
        st.error("高リスク上限は中リスク上限より小さい値を設定してください。")
        st.stop()

    # ─── 単一日の判定 ───────────────────────────
    if analysis_mode == "単一日の判定":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try:
                    weather_df = fetch_weather_data(
                        lat, lon, planting_date, pre_fetch_days=antecedent_days + 5
                    )
                except Exception as e:
                    st.error(f"気象データ取得エラー: {e}")
                    st.stop()
            else:
                if not pasted_data:
                    st.error("⚠️ AMeDASのデータが入力されていません。")
                    st.stop()
                try:
                    weather_df = parse_amedas_text(pasted_data)
                except Exception as e:
                    st.error(e)
                    st.stop()

        res = calculate_scab_risk(
            planting_date, weather_df, base_temp, gdd_start, gdd_end,
            threshold_high, threshold_med,
            use_antecedent, antecedent_days, antecedent_relief_mm,
            use_low_temp, low_temp_threshold, low_temp_days
        )
        if res is None or res['status'] != '判定完了':
            st.warning("指定日のデータが不足しているか、リスク期に達していません。")
            st.stop()

        st.subheader(f"📊 判定結果（植え付け日: {planting_date.strftime('%Y/%m/%d')}）")
        st.info("ℹ️ リスク期の降水量が少ないほど **高リスク（乾燥条件）** と判定します。")

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("リスク期 開始", res['start_date_w'].strftime('%Y/%m/%d'), f"{gdd_start} GDD")
        col2.metric("リスク期 終了", res['end_date_w'].strftime('%Y/%m/%d'),
                    f"{gdd_end} GDD" if res['reached_end'] else "進行中")
        col3.metric("リスク期 積算降水量", f"{res['total_precip']:.1f} mm")
        col4.metric(f"リスク期 {low_temp_threshold}℃以下の日数", f"{res['low_temp_count']} 日")
        col5.metric(f"先行{antecedent_days}日間 降水量",
                    f"{res['antecedent_precip']:.1f} mm" if res['ante_available'] else "データなし")

        if use_antecedent and res['ante_corrected']:
            st.success(f"💧 先行{antecedent_days}日間の降水量 **{res['antecedent_precip']:.1f}mm** ≥ {antecedent_relief_mm}mm のため、リスクを軽減しました。")
            
        if use_low_temp and res['temp_corrected']:
            st.success(f"❄️ リスク期に {low_temp_threshold}℃以下の日が **{res['low_temp_count']}日**（基準{low_temp_days}日以上）あったため、病菌活動低下とみなしリスクを軽減しました。")

        st.markdown(f"""
        <div style="background-color:{res['risk_color']}18; border-left:5px solid {res['risk_color']};
                    padding:15px; border-radius:5px; margin-top:10px;">
            <h3 style="color:{res['risk_color']}; margin:0;">最終判定: {res['risk_level']}</h3>
            <p style="margin-top:8px; font-size:15px;">
                リスク期積算降水量: {res['total_precip']:.1f} mm ／ 
                基本リスク(補正前): {RISK_MAP[res['base_risk_value']][0]}
            </p>
        </div>
        """, unsafe_allow_html=True)

        st.subheader("📈 気象データの推移（リスク期を強調表示）")
        plot_span = (res['end_date_w'] - res['start_date_w']).days
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

        ax1.plot(res['plot_df']['time'], res['plot_df']['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
        ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7)
        ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7)
        highlight_risk(ax1, res['risk_color'])
        ax1.set_ylabel("積算温度 (℃·day)", color="white")
        ax1.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")
        ax1.yaxis.label.set_color("white")

        colors_bar = [
            res['risk_color'] if (res['start_date_w'] <= t <= res['end_date_w']) else "#4a90d9"
            for t in res['plot_df']['time']
        ]
        ax2.bar(res['plot_df']['time'], res['plot_df']['precipitation_sum'].fillna(0),
                color=colors_bar, width=0.8, alpha=0.85)
        highlight_risk(ax2, res['risk_color'])

        if not res['risk_df'].empty:
            cum_df = res['risk_df'].copy()
            cum_df['cum_precip'] = cum_df['precipitation_sum'].fillna(0).cumsum()
            ax2b = ax2.twinx()
            ax2b.plot(cum_df['time'], cum_df['cum_precip'], color="white", linewidth=1.5, alpha=0.8, label="リスク期積算降水量")
            ax2b.set_ylabel("リスク期積算降水量 (mm)", color="white")
            ax2b.yaxis.label.set_color("white")
            ax2b.tick_params(colors="white")
            for spine in ax2b.spines.values(): spine.set_color("#444")
            ax2b.axhline(threshold_high, color="#FF4B4B", linestyle=":", linewidth=1.2, alpha=0.7)
            ax2b.axhline(threshold_med,  color="#FFA500",  linestyle=":", linewidth=1.2, alpha=0.7)

        ax2.set_ylabel("日降水量 (mm)", color="white")
        ax2.set_xlabel("日付", color="white")
        apply_date_axis(ax2, span_days=plot_span)
        plt.tight_layout()
        st.pyplot(fig)

        single_csv = pd.DataFrame([{
            '植え付け日':                           planting_date.strftime('%Y/%m/%d'),
            'リスク期開始日':                       res['start_date_w'].strftime('%Y/%m/%d'),
            'リスク期終了日':                       res['end_date_w'].strftime('%Y/%m/%d'),
            'GDD終了閾値到達':                       '到達' if res['reached_end'] else '未到達',
            f'先行{antecedent_days}日間降水量(mm)': round(res['antecedent_precip'], 1),
            '先行降水補正':                         '補正あり' if res['ante_corrected'] else '-',
            'リスク期 低温日数(日)':                res['low_temp_count'],
            '低温補正':                             '補正あり' if res['temp_corrected'] else '-',
            'リスク期積算降水量(mm)':               round(res['total_precip'], 1),
            '基本リスク値(補正前)':                 res['base_risk_value'],
            'リスクレベル(補正後)':                 res['risk_level'],
        }]).to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')

        st.download_button(
            label="📥 判定結果をCSVでダウンロード",
            data=single_csv,
            file_name=f"scab_risk_{planting_date.strftime('%Y%m%d')}_{loc_name}.csv",
            mime="text/csv",
        )

    # ─── 植え付け期間分析 (1シーズン) ─────────────
    elif analysis_mode == "植え付け期間分析":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try:
                    weather_df = fetch_weather_data(
                        lat, lon, planting_date, analysis_end_date,
                        pre_fetch_days=antecedent_days + 5
                    )
                except Exception as e:
                    st.error(f"気象データ取得エラー: {e}")
                    st.stop()
            else:
                if not pasted_data:
                    st.error("⚠️ AMeDASのデータが入力されていません。")
                    st.stop()
                try:
                    weather_df = parse_amedas_text(pasted_data)
                except Exception as e:
                    st.error(e)
                    st.stop()

        date_list = []
        cur = planting_date
        while cur <= analysis_end_date:
            date_list.append(cur)
            cur += timedelta(days=1)

        results_list    = []
        bar             = st.progress(0)
        update_interval = max(1, len(date_list) // 20)

        for i, p_date in enumerate(date_list):
            res = calculate_scab_risk(
                p_date, weather_df, base_temp, gdd_start, gdd_end,
                threshold_high, threshold_med,
                use_antecedent, antecedent_days, antecedent_relief_mm,
                use_low_temp, low_temp_threshold, low_temp_days
            )
            if res: results_list.append(res)
            if i % update_interval == 0:
                bar.progress((i + 1) / len(date_list), text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(results_list)
        if results_df.empty:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()

        st.subheader("📈 植え付け日による感染リスクの変化")
        st.info("ℹ️ グラフの **縦軸（積算降水量）が低いほど高リスク**（乾燥条件）。\n\n赤い点=高リスク、◇=先行降水量補正あり、□=低温補正あり。")

        fig_period = plot_period_analysis(results_df, threshold_high, threshold_med)
        st.pyplot(fig_period)

        csv_data = build_csv(results_df, antecedent_days)
        filename = (f"scab_risk_period_{planting_date.strftime('%Y%m%d')}"
                    f"_{analysis_end_date.strftime('%Y%m%d')}_{loc_name}.csv")

        col_dl, col_info = st.columns([1, 3])
        with col_dl:
            st.download_button(
                label="📥 期間分析結果をCSVでダウンロード",
                data=csv_data,
                file_name=filename,
                mime="text/csv",
            )
        with col_info:
            completed = results_df[results_df['status'] == '判定完了']
            if not completed.empty:
                high_n = (completed['risk_value'] == 2).sum()
                med_n  = (completed['risk_value'] == 1).sum()
                low_n  = (completed['risk_value'] == 0).sum()
                corr_a = int(completed['ante_corrected'].sum()) if 'ante_corrected' in completed.columns else 0
                corr_t = int(completed['temp_corrected'].sum()) if 'temp_corrected' in completed.columns else 0
                st.markdown(
                    f"分析完了: **{len(completed)}日分** ／ "
                    f"🔴 高リスク: **{high_n}日** ／ "
                    f"🟠 中リスク: **{med_n}日** ／ "
                    f"🔵 低リスク: **{low_n}日**\n\n"
                    f"（💧 先行降水補正: **{corr_a}日** ／ ❄️ 低温補正: **{corr_t}日**）"
                )

        with st.expander("📋 分析結果の詳細データテーブル"):
            disp_cols = ['planting_date', 'antecedent_precip', 'ante_corrected', 
                         'low_temp_count', 'temp_corrected',
                         'total_precip', 'risk_level', 'start_date_w', 'end_date_w', 'reached_end']
            disp_cols = [c for c in disp_cols if c in completed.columns]
            show_df   = completed[disp_cols].copy()

            date_cols2 = ['planting_date', 'start_date_w', 'end_date_w']
            for c in date_cols2:
                if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
            if 'reached_end'        in show_df: show_df['reached_end']        = show_df['reached_end'].map({True: '到達', False: '未到達'})
            if 'ante_corrected'     in show_df: show_df['ante_corrected']     = show_df['ante_corrected'].map({True: '補正あり', False: '-'})
            if 'temp_corrected'     in show_df: show_df['temp_corrected']     = show_df['temp_corrected'].map({True: '補正あり', False: '-'})
            if 'total_precip'       in show_df: show_df['total_precip']       = show_df['total_precip'].round(1)
            if 'antecedent_precip'  in show_df: show_df['antecedent_precip']  = show_df['antecedent_precip'].round(1)

            show_df.rename(columns={
                'planting_date':    '植え付け日',
                'antecedent_precip': f'先行{antecedent_days}日間降水量(mm)',
                'ante_corrected':   '先行降水補正',
                'low_temp_count':   'リスク期 低温日数(日)',
                'temp_corrected':   '低温補正',
                'total_precip':     'リスク期 降水量(mm)',
                'risk_level':       'リスクレベル(補正後)',
                'start_date_w':     'リスク期 開始日',
                'end_date_w':       'リスク期 終了日',
                'reached_end':      'GDD終了閾値到達',
            }, inplace=True)
            st.dataframe(show_df, use_container_width=True)

    # ─── 複数年比較分析 ────────────────────────
    elif analysis_mode == "複数年比較分析":
        if not compare_years:
            st.error("⚠️ 比較する年を1つ以上選択してください。")
            st.stop()

        # 年をまたぐ期間かどうかの判定 (例: 9/1 〜 2/28)
        is_cross_year = (start_md_date.month > end_md_date.month) or \
                        (start_md_date.month == end_md_date.month and start_md_date.day > end_md_date.day)

        min_year = min(compare_years)
        max_year = max(compare_years)
        
        overall_start = date(min_year, start_md_date.month, start_md_date.day)
        overall_end   = date(max_year + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)

        with st.spinner("対象となる全期間の気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try:
                    weather_df = fetch_weather_data(
                        lat, lon, overall_start, overall_end,
                        pre_fetch_days=antecedent_days + 5
                    )
                except Exception as e:
                    st.error(f"気象データ取得エラー: {e}")
                    st.stop()
            else:
                if not pasted_data:
                    st.error("⚠️ AMeDASのデータが入力されていません。")
                    st.stop()
                try:
                    weather_df = parse_amedas_text(pasted_data)
                except Exception as e:
                    st.error(e)
                    st.stop()

        all_results = []
        bar = st.progress(0)
        
        # 処理する全日数を計算してプログレスバーに使用
        date_lists = {}
        total_days = 0
        for y in sorted(compare_years):
            s_date = date(y, start_md_date.month, start_md_date.day)
            # 終了日が年をまたぐ場合は年を+1する
            e_date = date(y + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)
            
            d_list = []
            cur = s_date
            while cur <= e_date:
                d_list.append(cur)
                cur += timedelta(days=1)
            date_lists[y] = d_list
            total_days += len(d_list)

        processed_days = 0
        for y in sorted(compare_years):
            for p_date in date_lists[y]:
                res = calculate_scab_risk(
                    p_date, weather_df, base_temp, gdd_start, gdd_end,
                    threshold_high, threshold_med,
                    use_antecedent, antecedent_days, antecedent_relief_mm,
                    use_low_temp, low_temp_threshold, low_temp_days
                )
                if res:
                    res['target_year'] = f"{y}年"
                    all_results.append(res)
                
                processed_days += 1
                if processed_days % max(1, total_days // 20) == 0:
                    bar.progress(processed_days / total_days, text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(all_results)
        if results_df.empty:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()

        st.subheader("📈 複数年比較 感染リスクの変化")
        st.info("ℹ️ 指定した月日での植え付けリスクを年ごとに比較します。縦軸（積算降水量）が低いほど高リスク（乾燥条件）です。")

        # 年ごとのグラフを縦に並べて表示
        for y in sorted(compare_years):
            st.markdown(f"#### ▼ {y}年シーズン（{start_md_date.strftime('%m/%d')} 〜 {end_md_date.strftime('%m/%d')} 植え付け）")
            df_year = results_df[results_df['target_year'] == f"{y}年"]
            
            if df_year.empty or (df_year['status'] == '判定完了').sum() == 0:
                st.warning(f"{y}年シーズンの判定完了データがありません。（気象データ不足など）")
                continue
            
            fig_period = plot_period_analysis(df_year, threshold_high, threshold_med)
            st.pyplot(fig_period)

        csv_data = build_csv(results_df, antecedent_days)
        filename = f"scab_risk_multiyear_{loc_name}.csv"

        st.divider()
        st.download_button(
            label="📥 複数年比較の全分析結果をCSVでダウンロード",
            data=csv_data,
            file_name=filename,
            mime="text/csv",
        )

        with st.expander("📋 分析結果の詳細データテーブル"):
            completed = results_df[results_df['status'] == '判定完了']
            disp_cols = ['target_year', 'planting_date', 'antecedent_precip', 'ante_corrected', 
                         'low_temp_count', 'temp_corrected',
                         'total_precip', 'risk_level', 'start_date_w', 'end_date_w', 'reached_end']
            disp_cols = [c for c in disp_cols if c in completed.columns]
            show_df   = completed[disp_cols].copy()

            date_cols2 = ['planting_date', 'start_date_w', 'end_date_w']
            for c in date_cols2:
                if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
            if 'reached_end'        in show_df: show_df['reached_end']        = show_df['reached_end'].map({True: '到達', False: '未到達'})
            if 'ante_corrected'     in show_df: show_df['ante_corrected']     = show_df['ante_corrected'].map({True: '補正あり', False: '-'})
            if 'temp_corrected'     in show_df: show_df['temp_corrected']     = show_df['temp_corrected'].map({True: '補正あり', False: '-'})
            if 'total_precip'       in show_df: show_df['total_precip']       = show_df['total_precip'].round(1)
            if 'antecedent_precip'  in show_df: show_df['antecedent_precip']  = show_df['antecedent_precip'].round(1)

            show_df.rename(columns={
                'target_year':      '対象年',
                'planting_date':    '植え付け日',
                'antecedent_precip': f'先行{antecedent_days}日間降水量(mm)',
                'ante_corrected':   '先行降水補正',
                'low_temp_count':   'リスク期 低温日数(日)',
                'temp_corrected':   '低温補正',
                'total_precip':     'リスク期 降水量(mm)',
                'risk_level':       'リスクレベル(補正後)',
                'start_date_w':     'リスク期 開始日',
                'end_date_w':       'リスク期 終了日',
                'reached_end':      'GDD終了閾値到達',
            }, inplace=True)
            st.dataframe(show_df, use_container_width=True)

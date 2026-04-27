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

# ============================================================
# 地点プリセット
# ============================================================
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

# ============================================================
# リスクマップ
# ============================================================
RISK_MAP = {
    2: ("高 (High)",   "#FF4B4B"),
    1: ("中 (Medium)", "#FFA500"),
    0: ("低 (Low)",    "#0068C9"),
}

# ============================================================
# ページ設定
# ============================================================
st.set_page_config(page_title="そうか病 感染リスク判定・期間分析", layout="wide")
st.title("🌱 そうか病 感染リスク判定システム")
st.markdown("""
マルチ栽培を前提とし、植え付け日からの積算温度で塊茎の初期肥大期を推定します。  
**積算温度が設定GDD閾値**の期間を「感染リスク期」とし、その間の降水量でリスクを判定します。  
⚠️ **判定基準**: そうか病は乾燥条件で感染拡大するため、リスク期の**降水量が少ないほど高リスク**と判定します。  
💧 **先行降水補正**: 植え付け前の降水量が多い場合、初期土壌水分が高いとみなしリスクを1段階軽減します（初期定着直後の土壌水分への影響に限定）。  
❄️ **低温補正**: リスク期に地上2m気温（地温の代替指標）が低い日が続く場合、病原菌の活動が抑制されるためリスクを1段階軽減します。  
⚠️ **注意**: マルチ栽培下の実際の地温は気温より3〜7℃高い傾向があります。低温補正の閾値設定にはこの点を考慮してください。  
⚠️ **注意**: 先行降水補正と低温補正が同時に成立する場合でも、軽減は最大1段階に制限されます。
""")

# ============================================================
# サイドバー
# ============================================================
st.sidebar.header("📡 データソース設定")
data_source = st.sidebar.radio(
    "気象データの取得元を選択",
    ["Open-Meteo (API自動取得)", "AMeDAS (テキスト貼り付け)"]
)

pasted_data = None
if data_source == "AMeDAS (テキスト貼り付け)":
    st.sidebar.info(
        "💡 **データの注意**: ExcelやCSVからデータをコピーし、下の枠に貼り付けてください。\n\n"
        "対応形式: タブ区切り / カンマ区切り\n\n"
        "必要列: **日時（年月日）** / **平均気温** / **降水量（日合計）**"
    )
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

# グローバル用の変数初期化
bw_mode = False

# --- 分析モード別の日付設定 ---
if analysis_mode == "単一日の判定":
    planting_date     = st.sidebar.date_input("植え付け日", date(2025, 9, 30))
    analysis_end_date = None

elif analysis_mode == "植え付け期間分析":
    planting_period = st.sidebar.date_input(
        "植え付け分析期間（開始日〜終了日）",
        (date(2025, 9, 30), date(2026, 1, 1)),
        help="分析したい植え付け日の範囲を選択してください。"
    )
    if isinstance(planting_period, (tuple, list)) and len(planting_period) == 2:
        planting_date     = planting_period[0]
        analysis_end_date = planting_period[1]
    elif isinstance(planting_period, (tuple, list)) and len(planting_period) == 1:
        planting_date     = planting_period[0]
        analysis_end_date = planting_date
        st.sidebar.warning("終了日を選択してください。")
    else:
        planting_date     = planting_period
        analysis_end_date = planting_date
        st.sidebar.warning("終了日を選択してください。")

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
    if isinstance(planting_period, (tuple, list)) and len(planting_period) == 2:
        start_md_date = planting_period[0]
        end_md_date   = planting_period[1]
    elif isinstance(planting_period, (tuple, list)) and len(planting_period) == 1:
        start_md_date = planting_period[0]
        end_md_date   = start_md_date
        st.sidebar.warning("終了日を選択してください。")
    else:
        start_md_date = planting_period
        end_md_date   = planting_period
        st.sidebar.warning("終了日を選択してください。")
    
    # 💥【修正箇所】画面消去バグを防ぐため、ラジオボタンをサイドバーに移動
    st.sidebar.divider()
    st.sidebar.header("🎨 グラフ表示設定 (複数年比較)")
    overlay_mode = st.sidebar.radio(
        "複数年比較グラフの表示モード",
        ["🌈 通常（カラー）", "🖨️ 白黒印刷用"],
        horizontal=True
    )
    bw_mode = (overlay_mode == "🖨️ 白黒印刷用")

base_temp = st.sidebar.number_input(
    "ベース温度 (℃)",
    min_value=0.0, max_value=15.0, value=7.0, step=0.5,
    help="ジャガイモ塊茎肥大のGDD計算では一般にTb=7〜10℃が使用されます。"
)

st.sidebar.divider()
st.sidebar.header("⚙️ GDD閾値")
gdd_start = st.sidebar.number_input("開始 GDD", value=300, step=10)
gdd_end   = st.sidebar.number_input("終了 GDD", value=600, step=10)

if gdd_start >= gdd_end:
    st.sidebar.error("⚠️ GDD開始閾値は終了閾値より小さい値を設定してください。")

st.sidebar.divider()
st.sidebar.header("🌧️ リスク判定閾値（降水量）")
st.sidebar.caption("⬇️ 降水量が**少ない**ほど感染リスクが高くなります。")

threshold_high = st.sidebar.number_input(
    "高リスク境界値 (mm)：この値未満で高リスク",
    value=THRESHOLD_HIGH_DEFAULT
)
threshold_med = st.sidebar.number_input(
    "中リスク境界値 (mm)：この値未満で中リスク",
    value=THRESHOLD_MED_DEFAULT
)
if threshold_high >= threshold_med:
    st.sidebar.error("⚠️ 高リスク境界値は中リスク境界値より小さい値を設定してください。")

st.sidebar.divider()

# ===== 先行降水量設定 =====
st.sidebar.header("💧 先行降水量補正")
use_antecedent = st.sidebar.checkbox("先行降水量補正を使用する", value=True)
if use_antecedent:
    antecedent_days      = st.sidebar.number_input("集計期間（植え付け前 日数）", min_value=1, max_value=30, value=7, step=1)
    antecedent_relief_mm = st.sidebar.number_input(
        "軽減閾値 (mm)：この値以上で1段階軽減",
        min_value=0, max_value=200, value=ANTECEDENT_RELIEF_MM_DEFAULT, step=5
    )
else:
    antecedent_days      = 7
    antecedent_relief_mm = ANTECEDENT_RELIEF_MM_DEFAULT

st.sidebar.divider()

# ===== 低温補正設定 =====
st.sidebar.header("❄️ 低温補正（地温考慮）")
st.sidebar.caption("使用データは地上2m気温です。マルチ下の地温は実際には3〜7℃高い傾向があります。")
use_low_temp = st.sidebar.checkbox(
    "低温補正を使用する",
    value=True,
    help="リスク期に日平均気温が低い日が一定数ある場合、病原菌の活動低下を見込んでリスクを1段階軽減します。先行降水補正との同時適用は最大1段階に制限されます。"
)
if use_low_temp:
    low_temp_threshold = st.sidebar.number_input(
        "低温基準 (℃)", value=10.0, step=0.5,
        help="この温度以下の日をカウントします（2m気温）"
    )
    low_temp_days = st.sidebar.number_input(
        "軽減に必要な日数 (日)", min_value=1, max_value=30, value=3, step=1,
        help="リスク期内にこの日数以上、低温基準以下の日があれば補正します"
    )
else:
    low_temp_threshold = 10.0
    low_temp_days      = 3


# ============================================================
# 日付処理ユーティリティ (💥うるう年バグ修正)
# ============================================================
def get_safe_date(year, month, day):
    """うるう年ではない年に2月29日を設定しようとした際のエラーを回避し、2月28日に丸める"""
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise

# ============================================================
# 気象データ取得
# ============================================================
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
    df = pd.concat(frames).drop_duplicates('time').sort_values('time').reset_index(drop=True)
    df['time'] = pd.to_datetime(df['time'])
    return df


# ============================================================
# AMeDASテキストパーサー
# ============================================================
def parse_amedas_text(text_data):
    if not text_data.strip():
        raise ValueError("データが入力されていません。")

    try:
        df = pd.read_csv(io.StringIO(text_data.strip()), sep=None, engine='python', dtype=str)
    except Exception as e:
        raise ValueError(f"CSVパースエラー: {str(e)}")

    cols = list(df.columns)

    TIME_PRIORITY = ["年月日", "日時", "date", "time", "日"]
    time_col = None
    for key in TIME_PRIORITY:
        for col in cols:
            if key in str(col).lower().replace(" ", ""):
                time_col = col
                break
        if time_col:
            break

    year_col  = next((c for c in cols if str(c).strip() in ["年", "西暦年"]), None)
    month_col = next((c for c in cols if str(c).strip() in ["月"]), None)
    day_col   = next((c for c in cols if str(c).strip() in ["日"]), None)

    if not time_col and year_col and month_col and day_col:
        df['_combined_date'] = df[year_col].astype(str) + '-' + df[month_col].astype(str) + '-' + df[day_col].astype(str)
        time_col = '_combined_date'

    TEMP_PRIORITY = ["平均気温", "日平均気温", "mean_temp", "気温(平均)", "temperature_2m_mean"]
    TEMP_FALLBACK = ["気温", "temp"]
    temp_col = None
    for key in TEMP_PRIORITY:
        for col in cols:
            if key in str(col).replace(" ", ""):
                temp_col = col
                break
        if temp_col:
            break
    if not temp_col:
        for key in TEMP_FALLBACK:
            for col in cols:
                c_str = str(col).lower().replace(" ", "")
                if key in c_str and col != time_col:
                    temp_col = col
                    break
            if temp_col:
                break

    PRECIP_KEYS = ["降水量(日合計)", "日降水量", "降水量合計", "降水", "precip", "precipitation", "雨量", "雨"]
    precip_col = None
    for key in PRECIP_KEYS:
        for col in cols:
            if key in str(col).replace(" ", "") and col != time_col and col != temp_col:
                precip_col = col
                break
        if precip_col:
            break

    missing = []
    if not time_col:  missing.append("日時")
    if not temp_col:  missing.append("平均気温")
    if not precip_col: missing.append("降水量")
    if missing:
        raise ValueError(
            f"以下の列を特定できませんでした: {missing}\n"
            f"現在の列名: {cols}\n\n"
            "列名に「年月日」「平均気温」「降水量」を含めることを推奨します。"
        )

    df = df.rename(columns={
        time_col:  'time',
        temp_col:  'temperature_2m_mean',
        precip_col:'precipitation_sum'
    })

    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    df['temperature_2m_mean'] = pd.to_numeric(
        df['temperature_2m_mean'].astype(str).str.replace(r'[^\d\.-]', '', regex=True),
        errors='coerce'
    )
    df['precipitation_sum'] = pd.to_numeric(
        df['precipitation_sum'].astype(str).str.replace(r'[^\d\.-]', '', regex=True),
        errors='coerce'
    )

    df = df.dropna(subset=['time']).sort_values('time').reset_index(drop=True)
    if df.empty:
        raise ValueError("有効な気象データ行が見つかりませんでした。")

    return df


# ============================================================
# リスク計算
# ============================================================
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
        last_date  = df_after['time'].iloc[-1].date()
        today      = date.today()
        near_future_limit = today + timedelta(days=15)
        if last_date >= near_future_limit:
            return {'status': 'GDD未到達（データ不足）', 'planting_date': p_date}
        else:
            return {'status': 'GDD未到達（予報期間外）', 'planting_date': p_date}

    start_date_w = start_w.iloc[0]['time']
    end_w        = df_after[df_after['gdd_cum'] >= g_end]
    reached_end  = not end_w.empty
    end_date_w   = end_w.iloc[0]['time'] if reached_end else df_after['time'].iloc[-1]

    risk_df      = df_after[(df_after['time'] >= start_date_w) & (df_after['time'] <= end_date_w)]
    total_days_in_risk = len(risk_df)

    missing_temp_days   = risk_df['temperature_2m_mean'].isna().sum()
    missing_precip_days = risk_df['precipitation_sum'].isna().sum()
    total_precip        = risk_df['precipitation_sum'].fillna(0).sum()

    low_temp_count = int((risk_df['temperature_2m_mean'].fillna(999) <= temp_thresh).sum())

    ante_start_ts     = pd.Timestamp(p_date) - timedelta(days=ante_days)
    ante_end_ts       = pd.Timestamp(p_date) - timedelta(days=1)
    ante_df           = weather_df[(weather_df['time'] >= ante_start_ts) & (weather_df['time'] <= ante_end_ts)]
    antecedent_precip = ante_df['precipitation_sum'].fillna(0).sum() if not ante_df.empty else 0.0
    ante_available    = not ante_df.empty

    if total_precip < t_high:
        base_risk_v = 2
    elif total_precip < t_med:
        base_risk_v = 1
    else:
        base_risk_v = 0

    ante_corrected = use_ante and ante_available and antecedent_precip >= ante_relief_mm
    temp_corrected = use_temp and low_temp_count >= temp_days
    any_correction = ante_corrected or temp_corrected
    corrected_risk_v = max(0, base_risk_v - (1 if any_correction else 0))

    risk_l, risk_c = RISK_MAP[corrected_risk_v]

    return {
        'status':               '判定完了',
        'planting_date':        p_date,
        'start_date_w':         start_date_w,
        'end_date_w':           end_date_w,
        'reached_end':          reached_end,
        'total_precip':         total_precip,
        'antecedent_precip':    antecedent_precip,
        'low_temp_count':       low_temp_count,
        'ante_available':       ante_available,
        'ante_corrected':       ante_corrected,
        'temp_corrected':       temp_corrected,
        'any_correction':       any_correction,
        'base_risk_value':      base_risk_v,
        'risk_value':           corrected_risk_v,
        'risk_level':           risk_l,
        'risk_color':           risk_c,
        'missing_temp_days':    int(missing_temp_days),
        'missing_precip_days':  int(missing_precip_days),
        'total_days_in_risk':   total_days_in_risk,
        'risk_df':              risk_df,
        'plot_df':              df_after[df_after['time'] <= end_date_w],
    }


# ============================================================
# グラフ描画・UI ユーティリティ
# ============================================================
def apply_date_axis(ax, span_days=None):
    interval = 10
    if span_days is not None:
        if span_days > 150:  interval = 20
        elif span_days > 60: interval = 10
        elif span_days > 30: interval = 5
        else:                interval = 2

    ax.xaxis.set_major_locator(mdates.DayLocator(interval=interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha('right')
        lbl.set_color('white')

def plot_period_analysis(results_df, t_high, t_med, title_suffix=""):
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
    if title_suffix:
        ax.set_title(title_suffix, color="white", fontsize=12)
    apply_date_axis(ax, span_days=span_days)

    handles = [
        mpatches.Patch(color="#FF4B4B", label="高リスク (High)：乾燥"),
        mpatches.Patch(color="#FFA500", label="中リスク (Medium)"),
        mpatches.Patch(color="#0068C9", label="低リスク (Low)：湿潤"),
    ]
    if 'ante_corrected' in df_plot.columns and df_plot['ante_corrected'].any():
        handles.append(mlines.Line2D([], [], marker='D', color='white', markerfacecolor='none',
                                     markersize=8, label="先行降水量補正あり（最大1段階）", linestyle='None'))
    if 'temp_corrected' in df_plot.columns and df_plot['temp_corrected'].any():
        handles.append(mlines.Line2D([], [], marker='s', color='cyan', markerfacecolor='none',
                                     markersize=9, label="低温補正あり（最大1段階）", linestyle='None'))

    ax.legend(handles=handles, loc="best", facecolor="#1a1d24", labelcolor="white")
    plt.tight_layout()
    return fig


def plot_multiyear_overlay(results_df, t_high, t_med, compare_years, start_md_date, bw_mode: bool = False):
    BW_STYLES = [
        ("-",   1.8, "o", 7, "none"),   ("-",   1.8, "^", 8, "full"),
        ("--",  1.8, "v", 8, "none"),   ("--",  1.8, "o", 7, "full"),
        ("-.",  1.8, "s", 7, "none"),   ("-.",  1.8, "P", 8, "none"),
        (":",   2.0, "D", 7, "none"),   (":",   2.0, "X", 8, "none"),
        ("-",   1.8, "h", 8, "none"),   ("--",  2.0, "D", 7, "full"),
    ]
    BW_GRAYS = ["#000000", "#333333", "#555555", "#777777",
                "#111111", "#444444", "#222222", "#666666", "#888888", "#1a1a1a"]

    COLOR_STYLES = [
        ("-",  1.8, "o", 7, "none"),    ("-",  1.8, "^", 8, "full"),
        ("--", 1.8, "v", 8, "none"),    ("--", 1.8, "o", 7, "full"),
        ("-.", 1.8, "s", 7, "none"),    ("-.", 1.8, "P", 8, "none"),
        (":",  2.0, "D", 7, "none"),    (":",  2.0, "X", 8, "none"),
        ("-",  1.8, "h", 8, "none"),    ("--", 2.0, "D", 7, "full"),
    ]
    COLOR_PALETTE = [
        "#4fc3f7", "#ef5350", "#66bb6a", "#ffa726",
        "#ab47bc", "#26c6da", "#ff7043", "#d4e157",
        "#ec407a", "#42a5f5",
    ]

    fig, ax = plt.subplots(figsize=(13, 6))

    if bw_mode:
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")
        fg_main, spine_c, grid_c = "black", "#aaaaaa", "#dddddd"
        leg_face, leg_edge = "white", "#aaaaaa"
        h_line_color, h_line_style = "black", ":"
        m_line_color, m_line_style = "#555555", "--"
    else:
        fig.patch.set_facecolor("#0e1117")
        ax.set_facecolor("#1a1d24")
        fg_main, spine_c, grid_c = "white", "#444444", "#2a2d34"
        leg_face, leg_edge = "#1a1d24", "#444444"
        h_line_color, h_line_style = "#FF4B4B", ":"
        m_line_color, m_line_style = "#FFA500", ":"

    ax.tick_params(colors=fg_main)
    ax.xaxis.label.set_color(fg_main)
    ax.yaxis.label.set_color(fg_main)
    ax.title.set_color(fg_main)
    for spine in ax.spines.values():
        spine.set_color(spine_c)

    legend_handles = []

    for i, y in enumerate(sorted(compare_years)):
        df_y = results_df[
            results_df['target_year'].isin([f"{y}年", f"{y}/{y+1}シーズン"])
            & (results_df['status'] == '判定完了')
        ].copy()

        if df_y.empty:
            continue

        df_y['planting_date'] = pd.to_datetime(df_y['planting_date'])

        # 💥【修正箇所】うるう年の2月29日→平年変換時のエラー防止
        def to_md_date(d):
            base_y = 2000 if d.month >= start_md_date.month else 2001
            try:
                return d.replace(year=base_y)
            except ValueError:
                return d.replace(year=base_y, day=28)

        df_y['md_date'] = df_y['planting_date'].apply(to_md_date)
        df_y = df_y.sort_values('md_date')

        if bw_mode:
            ls, lw, mk, ms, fs = BW_STYLES[i % len(BW_STYLES)]
            color = BW_GRAYS[i % len(BW_GRAYS)]
        else:
            ls, lw, mk, ms, fs = COLOR_STYLES[i % len(COLOR_STYLES)]
            color = COLOR_PALETTE[i % len(COLOR_PALETTE)]

        mfc = color if fs == "full" else "none"

        ax.plot(df_y['md_date'], df_y['total_precip'],
                color=color, linestyle=ls, linewidth=lw, alpha=0.9, zorder=3)
        ax.scatter(df_y['md_date'], df_y['total_precip'],
                   marker=mk, s=ms ** 2, zorder=4,
                   facecolors=mfc, edgecolors=color, linewidths=1.5)

        season_label = results_df[results_df['target_year'].isin([f"{y}年", f"{y}/{y+1}シーズン"])]['target_year'].iloc[0]
        legend_handles.append(
            mlines.Line2D([], [], color=color, linestyle=ls, linewidth=lw,
                          marker=mk, markersize=ms, fillstyle=fs,
                          markerfacecolor=mfc, markeredgecolor=color,
                          label=season_label)
        )

    ax.axhline(t_high, color=h_line_color, linestyle=h_line_style, linewidth=1.8, alpha=0.85)
    ax.axhline(t_med,  color=m_line_color, linestyle=m_line_style, linewidth=1.5, alpha=0.80)

    xlim = ax.get_xlim()
    x_label_pos = pd.Timestamp("2000-01-01") + timedelta(days=max(0, int(xlim[0])))
    ax.annotate(f" 高リスク境界 {t_high}mm", xy=(x_label_pos, t_high),
                va='bottom', color=h_line_color, fontsize=8.5)
    ax.annotate(f" 中リスク境界 {t_med}mm",  xy=(x_label_pos, t_med),
                va='bottom', color=m_line_color, fontsize=8.5)

    legend_handles += [
        mlines.Line2D([], [], color=h_line_color, linestyle=h_line_style, linewidth=1.8, label=f"高リスク境界 {t_high}mm"),
        mlines.Line2D([], [], color=m_line_color, linestyle=m_line_style, linewidth=1.5, label=f"中リスク境界 {t_med}mm"),
    ]

    ax.xaxis.set_major_locator(mdates.DayLocator(interval=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45)
        lbl.set_ha('right')
        lbl.set_color(fg_main)

    ax.set_ylabel("リスク期内の積算降水量 (mm)  ※少ないほど高リスク")
    ax.set_xlabel("植え付け日（月/日）")
    mode_label = "【白黒印刷用】" if bw_mode else "【通常】"
    ax.set_title(f"複数年重ね合わせ比較 {mode_label}", fontsize=12)

    ax.legend(handles=legend_handles, loc="best", facecolor=leg_face, edgecolor=leg_edge, labelcolor=fg_main, framealpha=0.92)
    ax.grid(axis='y', color=grid_c, linewidth=0.7, linestyle='-', zorder=0)

    plt.tight_layout()
    return fig


def build_csv(results_df: pd.DataFrame, ante_days: int) -> bytes:
    cols_src = ['target_year', 'planting_date', 'start_date_w', 'end_date_w', 'reached_end',
                'antecedent_precip', 'ante_corrected', 'low_temp_count', 'temp_corrected',
                'total_precip', 'missing_precip_days', 'base_risk_value', 'risk_level']
    avail   = [c for c in cols_src if c in results_df.columns]
    show_df = results_df[results_df['status'] == '判定完了'][avail].copy()

    date_cols = ['planting_date', 'start_date_w', 'end_date_w']
    for c in date_cols:
        if c in show_df:
            show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
    if 'reached_end'       in show_df: show_df['reached_end']    = show_df['reached_end'].map({True: '到達', False: '未到達'})
    if 'ante_corrected'    in show_df: show_df['ante_corrected'] = show_df['ante_corrected'].map({True: '補正あり', False: '-'})
    if 'temp_corrected'    in show_df: show_df['temp_corrected'] = show_df['temp_corrected'].map({True: '補正あり', False: '-'})
    if 'total_precip'      in show_df: show_df['total_precip']   = show_df['total_precip'].round(1)
    if 'antecedent_precip' in show_df: show_df['antecedent_precip'] = show_df['antecedent_precip'].round(1)
    if 'base_risk_value'   in show_df: show_df['base_risk_value'] = show_df['base_risk_value'].map({2: '高(High)', 1: '中(Medium)', 0: '低(Low)'})

    rename_dict = {
        'target_year':         '対象年',
        'planting_date':       '植え付け日',
        'start_date_w':        'リスク期開始日',
        'end_date_w':          'リスク期終了日',
        'reached_end':         'GDD終了閾値到達',
        'antecedent_precip':   f'先行{ante_days}日間降水量(mm)',
        'ante_corrected':      '先行降水補正',
        'low_temp_count':      'リスク期 低温日数(日)',
        'temp_corrected':      '低温補正',
        'total_precip':        'リスク期積算降水量(mm)',
        'missing_precip_days': 'リスク期 降水欠測日数(日)',
        'base_risk_value':     '基本リスク(補正前)',
        'risk_level':          'リスクレベル(補正後)',
    }
    show_df.rename(columns=rename_dict, inplace=True)
    return show_df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')


def warn_missing(missing_precip_days, total_days_in_risk):
    if missing_precip_days > 0:
        ratio = missing_precip_days / max(1, total_days_in_risk)
        msg = (f"⚠️ リスク期 {total_days_in_risk}日中 **{missing_precip_days}日** の降水量データが欠測です"
               f"（欠測率 {ratio:.0%}）。欠測日は0mm扱いのため、実際より**高リスク方向**に判定が偏る可能性があります。")
        if ratio > 0.2:
            st.error(msg)
        else:
            st.warning(msg)


# ============================================================
# 実行処理
# ============================================================
if st.sidebar.button("▶ リスク分析を実行", type="primary"):

    if gdd_start >= gdd_end:
        st.error("GDD開始閾値は終了閾値より小さい値を設定してください。")
        st.stop()
    if threshold_high >= threshold_med:
        st.error("高リスク境界値は中リスク境界値より小さい値を設定してください。")
        st.stop()

    # ────────────────────────────────────────────────────────
    # 単一日の判定
    # ────────────────────────────────────────────────────────
    if analysis_mode == "単一日の判定":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try:
                    weather_df = fetch_weather_data(lat, lon, planting_date, pre_fetch_days=antecedent_days + 5)
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

        if res is None:
            st.warning("指定日の気象データが存在しません。")
            st.stop()

        if res['status'] != '判定完了':
            if res['status'] == 'GDD未到達（予報期間外）':
                st.warning(f"⚠️ {planting_date.strftime('%Y/%m/%d')} 植え付けでは、取得できる予報期間内にGDD到達しません。")
            else:
                st.warning(f"リスク期に達していません: {res['status']}")
            st.stop()

        st.subheader(f"📊 判定結果（植え付け日: {planting_date.strftime('%Y/%m/%d')}）")
        st.caption(f"データソース: {data_source} ／ 地点: {loc_name}")
        st.info("ℹ️ リスク期の降水量が少ないほど **高リスク（乾燥条件）** と判定します。")

        warn_missing(res['missing_precip_days'], res['total_days_in_risk'])

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("リスク期 開始", res['start_date_w'].strftime('%Y/%m/%d'), f"{gdd_start} GDD")
        col2.metric("リスク期 終了", res['end_date_w'].strftime('%Y/%m/%d'),
                    f"{gdd_end} GDD" if res['reached_end'] else "進行中/予報端")
        col3.metric("リスク期 積算降水量", f"{res['total_precip']:.1f} mm")
        col4.metric(f"低温日数（≤{low_temp_threshold}℃）", f"{res['low_temp_count']} 日")
        col5.metric(f"先行{antecedent_days}日間 降水量",
                    f"{res['antecedent_precip']:.1f} mm" if res['ante_available'] else "データなし")

        if res['any_correction']:
            reasons = []
            if res['ante_corrected']: reasons.append(f"先行降水量 ≥ {antecedent_relief_mm}mm")
            if res['temp_corrected']: reasons.append(f"低温日数 ≥ {low_temp_days}日")
            st.success(f"✅ 補正適用（最大1段階軽減）: {' ／ '.join(reasons)}\n\n"
                       f"基本リスク: **{RISK_MAP[res['base_risk_value']][0]}** → 補正後: **{res['risk_level']}**")

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
            for spine in ax.spines.values(): spine.set_color("#444")

        risk_start_num = matplotlib.dates.date2num(res['start_date_w'])
        risk_end_num   = matplotlib.dates.date2num(res['end_date_w'])

        def highlight_risk(ax, color_code):
            ax.axvspan(risk_start_num, risk_end_num, color=color_code, alpha=0.15, label="感染リスク期")
            ax.axvline(risk_start_num, color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.axvline(risk_end_num,   color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)

        ax1.plot(res['plot_df']['time'], res['plot_df']['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
        ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7, label=f"GDD開始 {gdd_start}")
        ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7, label=f"GDD終了 {gdd_end}")
        highlight_risk(ax1, res['risk_color'])
        ax1.set_ylabel("積算温度 (℃·day)", color="white")
        ax1.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")
        ax1.yaxis.label.set_color("white")

        colors_bar = [res['risk_color'] if (res['start_date_w'] <= t <= res['end_date_w']) else "#4a90d9" for t in res['plot_df']['time']]
        ax2.bar(res['plot_df']['time'], res['plot_df']['precipitation_sum'].fillna(0), color=colors_bar, width=0.8, alpha=0.85)
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
            ax2b.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")

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
            '補正適用（最大1段階）':                '適用' if res['any_correction'] else '-',
            'リスク期積算降水量(mm)':               round(res['total_precip'], 1),
            'リスク期 降水欠測日数(日)':            res['missing_precip_days'],
            '基本リスク(補正前)':                   RISK_MAP[res['base_risk_value']][0],
            'リスクレベル(補正後)':                 res['risk_level'],
            'データソース':                         data_source,
            '地点':                                 loc_name,
        }]).to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')

        st.download_button("📥 判定結果をCSVでダウンロード", single_csv, file_name=f"scab_risk_{planting_date.strftime('%Y%m%d')}_{loc_name}.csv", mime="text/csv")

    # ────────────────────────────────────────────────────────
    # 植え付け期間分析
    # ────────────────────────────────────────────────────────
    elif analysis_mode == "植え付け期間分析":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try:
                    weather_df = fetch_weather_data(lat, lon, planting_date, analysis_end_date, pre_fetch_days=antecedent_days + 5)
                except Exception as e:
                    st.error(f"気象データ取得エラー: {e}")
                    st.stop()
            else:
                if not pasted_data: st.error("⚠️ データがありません。"); st.stop()
                try: weather_df = parse_amedas_text(pasted_data)
                except Exception as e: st.error(e); st.stop()

        date_list = [planting_date + timedelta(days=x) for x in range((analysis_end_date - planting_date).days + 1)]
        SKIP_KEYS = {'risk_df', 'plot_df'}
        results_list = []
        bar = st.progress(0)
        update_interval = max(1, len(date_list) // 20)

        for i, p_date in enumerate(date_list):
            res = calculate_scab_risk(p_date, weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med,
                                      use_antecedent, antecedent_days, antecedent_relief_mm, use_low_temp, low_temp_threshold, low_temp_days)
            if res: results_list.append({k: v for k, v in res.items() if k not in SKIP_KEYS})
            if i % update_interval == 0: bar.progress((i + 1) / len(date_list), text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(results_list)
        if results_df.empty:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()

        st.subheader("📈 植え付け日による感染リスクの変化")
        st.caption(f"データソース: {data_source} ／ 地点: {loc_name}")
        st.info("ℹ️ グラフの **縦軸（積算降水量）が低いほど高リスク**（乾燥条件）。\n\n◇=先行降水量補正あり、□=低温補正あり（いずれも最大1段階軽減）。")

        fig_period = plot_period_analysis(results_df, threshold_high, threshold_med)
        st.pyplot(fig_period)

        csv_data = build_csv(results_df, antecedent_days)
        filename = f"scab_risk_period_{planting_date.strftime('%Y%m%d')}_{analysis_end_date.strftime('%Y%m%d')}_{loc_name}.csv"

        col_dl, col_info = st.columns([1, 3])
        with col_dl: st.download_button("📥 期間分析結果をCSVでダウンロード", csv_data, file_name=filename, mime="text/csv")
        with col_info:
            completed = results_df[results_df['status'] == '判定完了']
            if not completed.empty:
                high_n, med_n, low_n = (completed['risk_value'] == 2).sum(), (completed['risk_value'] == 1).sum(), (completed['risk_value'] == 0).sum()
                st.markdown(f"分析完了: **{len(completed)}日分** ／ 🔴 高リスク: **{high_n}日** ／ 🟠 中リスク: **{med_n}日** ／ 🔵 低リスク: **{low_n}日**")

        with st.expander("📋 分析結果の詳細データテーブル"):
            completed = results_df[results_df['status'] == '判定完了']
            disp_cols = ['planting_date', 'antecedent_precip', 'ante_corrected', 'low_temp_count', 'temp_corrected', 'any_correction',
                         'total_precip', 'missing_precip_days', 'risk_level', 'start_date_w', 'end_date_w', 'reached_end']
            show_df = completed[[c for c in disp_cols if c in completed.columns]].copy()
            for c in ['planting_date', 'start_date_w', 'end_date_w']:
                if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
            st.dataframe(show_df, use_container_width=True)

    # ────────────────────────────────────────────────────────
    # 複数年比較分析
    # ────────────────────────────────────────────────────────
    elif analysis_mode == "複数年比較分析":
        if not compare_years: st.error("⚠️ 比較する年を選択してください。"); st.stop()

        is_cross_year = (start_md_date.month > end_md_date.month) or (start_md_date.month == end_md_date.month and start_md_date.day > end_md_date.day)
        min_year, max_year = min(compare_years), max(compare_years)

        # 💥【修正箇所】うるう年（2月29日）の平年パースエラー防止
        overall_start = get_safe_date(min_year, start_md_date.month, start_md_date.day)
        overall_end   = get_safe_date(max_year + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)

        with st.spinner("対象となる全期間の気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try: weather_df = fetch_weather_data(lat, lon, overall_start, overall_end, pre_fetch_days=antecedent_days + 5)
                except Exception as e: st.error(f"気象データ取得エラー: {e}"); st.stop()
            else:
                if not pasted_data: st.error("⚠️ データがありません。"); st.stop()
                try: weather_df = parse_amedas_text(pasted_data)
                except Exception as e: st.error(e); st.stop()

        SKIP_KEYS = {'risk_df', 'plot_df'}
        all_results = []
        bar = st.progress(0)

        date_lists, total_days = {}, 0
        for y in sorted(compare_years):
            s_date = get_safe_date(y, start_md_date.month, start_md_date.day)
            e_date = get_safe_date(y + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)
            d_list = [s_date + timedelta(days=x) for x in range((e_date - s_date).days + 1)]
            date_lists[y] = d_list
            total_days += len(d_list)

        processed_days = 0
        for y in sorted(compare_years):
            season_label = f"{y}/{y+1}シーズン" if is_cross_year else f"{y}年"
            for p_date in date_lists[y]:
                res = calculate_scab_risk(p_date, weather_df, base_temp, gdd_start, gdd_end, threshold_high, threshold_med,
                                          use_antecedent, antecedent_days, antecedent_relief_mm, use_low_temp, low_temp_threshold, low_temp_days)
                if res:
                    row = {k: v for k, v in res.items() if k not in SKIP_KEYS}
                    row['target_year'] = season_label
                    all_results.append(row)
                processed_days += 1
                if processed_days % max(1, total_days // 20) == 0:
                    bar.progress(processed_days / total_days, text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(all_results)
        if results_df.empty:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()

        st.subheader("📈 複数年比較 感染リスクの変化")
        st.caption(f"データソース: {data_source} ／ 地点: {loc_name}")
        st.info("ℹ️ 縦軸（積算降水量）が低いほど高リスク（乾燥条件）です。先行降水補正・低温補正は最大1段階軽減。")

        # 💥【修正箇所】消失バグ回避のため、ラジオボタンはサイドバーに移動済み
        st.markdown("#### ▼ 全年重ね合わせ比較")
        fig_overlay = plot_multiyear_overlay(results_df, threshold_high, threshold_med, compare_years, start_md_date, bw_mode=bw_mode)
        st.pyplot(fig_overlay)

        st.markdown("---")
        st.markdown("#### ▼ 年別グラフ")
        for season_label in sorted(results_df['target_year'].unique()):
            st.markdown(f"**{season_label}** （{start_md_date.strftime('%m/%d')} 〜 {end_md_date.strftime('%m/%d')} 植え付け）")
            df_year = results_df[results_df['target_year'] == season_label]
            if df_year.empty or (df_year['status'] == '判定完了').sum() == 0:
                st.warning(f"{season_label} の判定完了データがありません。")
                continue
            fig_y = plot_period_analysis(df_year, threshold_high, threshold_med, title_suffix=season_label)
            st.pyplot(fig_y)

        csv_data = build_csv(results_df, antecedent_days)
        st.divider()
        st.download_button("📥 複数年比較の全分析結果をCSVでダウンロード", csv_data, file_name=f"scab_risk_multiyear_{loc_name}.csv", mime="text/csv")

        with st.expander("📋 分析結果の詳細データテーブル"):
            completed = results_df[results_df['status'] == '判定完了']
            disp_cols = ['target_year', 'planting_date', 'antecedent_precip', 'ante_corrected', 'low_temp_count', 'temp_corrected',
                         'any_correction', 'total_precip', 'missing_precip_days', 'risk_level', 'start_date_w', 'end_date_w', 'reached_end']
            show_df = completed[[c for c in disp_cols if c in completed.columns]].copy()
            for c in ['planting_date', 'start_date_w', 'end_date_w']:
                if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
            st.dataframe(show_df, use_container_width=True)

        gdd_not_reached = results_df[results_df['status'].str.startswith('GDD未到達', na=False)]
        if not gdd_not_reached.empty:
            st.warning(f"⚠️ 全期間で **{len(gdd_not_reached)}日分** はGDD閾値未到達のため判定から除外されました。")

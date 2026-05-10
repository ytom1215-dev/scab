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
import re

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
マルチ栽培を前提とし、塊茎の初期肥大期を推定して「感染リスク期」とします。  
このリスク期の降水量が少ないほど（乾燥条件）、そうか病の感染リスクが高まると判定します。  
⏱️ **期間の推定**: 「積算温度(GDD)」または「植え付け後の経過日数」を選択できます。  
💧 **先行降水補正**: リスク開始前の降水量が多い場合、初期土壌水分が高いとみなしリスクを1段階軽減します。  
❄️ **低温補正**: リスク期に地上2m気温（地温の代替指標）が低い日が続く場合、病原菌の活動が抑制されるためリスクを1段階軽減します。
""")


# ============================================================
# 気象庁データパーサー (CSV / タブ区切り 両対応)
# ============================================================
def parse_jma_csv(decoded_text):
    delimiter = '\t' if '\t' in decoded_text else ','
    all_lines = [l for l in decoded_text.splitlines() if l.strip()]

    data_start = None
    for i, line in enumerate(all_lines):
        first = line.split(delimiter)[0].strip().strip('"')
        if not first:
            continue
        if re.match(r'^\d{4}[/-]\d{1,2}[/-]\d{1,2}$', first):
            try:
                if pd.notna(pd.to_datetime(first)):
                    data_start = i
                    break
            except Exception:
                pass

    if data_start is None:
        raise ValueError(
            "日付列を含むデータ行が見つかりません。\n"
            "気象庁からダウンロードしたデータをそのままの形式で入力してください。"
        )

    header_lines = all_lines[max(0, data_start - 4): data_start]
    header_grid = []
    for line in header_lines:
        row = [c.strip().strip('"') for c in line.split(delimiter)]
        header_grid.append(row)

    data_text = '\n'.join(all_lines[data_start:])
    df_raw = pd.read_csv(io.StringIO(data_text), header=None, sep=delimiter)
    n_cols = df_raw.shape[1]

    def ffill_row(row, length):
        result = [''] * length
        last_val = ''
        for i in range(length):
            val = row[i] if i < len(row) else ''
            if val:
                last_val = val
            result[i] = last_val
        return result

    ffilled_grid = []
    for ri, row in enumerate(header_grid):
        if ri == 0:
            ffilled_grid.append(ffill_row(row, n_cols))
        else:
            ffilled_grid.append(row + [''] * (n_cols - len(row)))

    col_labels = []
    for ci in range(n_cols):
        parts = []
        for row in ffilled_grid:
            val = row[ci] if ci < len(row) else ''
            if val and val not in parts:
                parts.append(val)
        col_labels.append('|'.join(parts))

    QUALITY_KEYS = ['品質', '均質']

    def find_col(must, exclude=None):
        if exclude is None:
            exclude = []
        exclude = exclude + QUALITY_KEYS
        for i, label in enumerate(col_labels):
            if all(k in label for k in must) and not any(k in label for k in exclude):
                return i
        return None

    col_map = {
        'temp_mean':        find_col(['気温', '平均'], ['最高', '最低', '平年']),
        'temp_mean_normal': find_col(['気温', '平均', '平年'], ['最高', '最低']),
        'precip':           find_col(['降水量'], ['平年']),
        'precip_normal':    find_col(['降水量', '平年']),
        'temp_max':         find_col(['最高気温'], ['平年']),
        'temp_max_normal':  find_col(['最高気温', '平年']),
        'temp_min':         find_col(['最低気温'], ['平年']),
        'temp_min_normal':  find_col(['最低気温', '平年']),
        'sun_hours':        find_col(['日照時間'], ['平年']),
        'sun_hours_normal': find_col(['日照時間', '平年']),
    }

    valid = {k: v for k, v in col_map.items() if v is not None}
    df_clean = df_raw.iloc[:, [0] + list(valid.values())].copy()
    df_clean.columns = ['date'] + list(valid.keys())

    df_clean['date'] = pd.to_datetime(df_clean['date'], errors='coerce')
    df_clean = df_clean.dropna(subset=['date']).reset_index(drop=True)

    for col in df_clean.columns:
        if col != 'date':
            df_clean[col] = pd.to_numeric(df_clean[col], errors='coerce')

    for col in col_map:
        if col not in df_clean.columns:
            df_clean[col] = float('nan')

    return df_clean, col_labels, col_map


def parse_jma_to_weather_df(decoded_text):
    df, col_labels, col_map = parse_jma_csv(decoded_text)

    if df['temp_mean'].isna().all() and not df['temp_max'].isna().all() and not df['temp_min'].isna().all():
        df['temp_mean'] = (df['temp_max'] + df['temp_min']) / 2

    missing = []
    if df['temp_mean'].isna().all():
        missing.append("平均気温（気温|平均 列）")
    if df['precip'].isna().all():
        missing.append("降水量")
    if missing:
        raise ValueError(f"以下の列を検出できませんでした: {missing}\n検出列: {col_labels}")

    weather_df = df.rename(columns={
        'date':             'time',
        'temp_mean':        'temperature_2m_mean',
        'precip':           'precipitation_sum',
        'temp_mean_normal': 'temperature_2m_mean_normal',
        'precip_normal':    'precipitation_sum_normal',
    })[['time', 'temperature_2m_mean', 'precipitation_sum', 'temperature_2m_mean_normal', 'precipitation_sum_normal']]

    return weather_df, col_labels, col_map


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
    st.sidebar.markdown(
        "気象庁のデータをExcel等で開き、**見出し行も含めて全選択してコピー**し、下に貼り付けてください。\n\n"
        "対応形式: タブ区切り（Excelコピペ） / カンマ区切り（CSV）\n\n"
        "必要列: **平均気温** / **降水量（日合計）**\n\n"
        "💡 **平年値**（気温・降水量）が含まれている場合、平年との比較分析が自動で有効になります。"
    )
    pasted_data = st.sidebar.text_area("ここにデータを貼り付け", height=150)

st.sidebar.divider()

st.sidebar.header("🗺️ アプリのモードと地点")

analysis_mode = st.sidebar.radio(
    "利用する機能を選択",
    [
        "🦠 リスク判定: 単一日の判定",
        "🦠 リスク判定: 植え付け期間分析",
        "🦠 リスク判定: 複数年比較分析"
    ]
)

loc_name = st.sidebar.selectbox("地点を選択", list(LOCATIONS.keys()))
if LOCATIONS[loc_name] is None:
    lat = st.sidebar.number_input("緯度", value=30.73, format="%.4f")
    lon = st.sidebar.number_input("経度", value=131.00, format="%.4f")
else:
    lat, lon = LOCATIONS[loc_name]
    st.sidebar.caption(f"緯度: {lat}  経度: {lon}")

st.sidebar.divider()

bw_mode = False

# ============================================================
# 分析モード別の日付設定UI
# ============================================================
if analysis_mode == "🦠 リスク判定: 単一日の判定":
    planting_date     = st.sidebar.date_input("植え付け日", date(2025, 9, 30))
    analysis_end_date = None

elif analysis_mode == "🦠 リスク判定: 植え付け期間分析":
    planting_period = st.sidebar.date_input(
        "植え付け分析期間（開始日〜終了日）",
        (date(2025, 9, 30), date(2026, 1, 1))
    )
    if isinstance(planting_period, (tuple, list)) and len(planting_period) == 2:
        planting_date, analysis_end_date = planting_period[0], planting_period[1]
    else:
        planting_date = planting_period[0] if isinstance(planting_period, (tuple, list)) else planting_period
        analysis_end_date = planting_date
        st.sidebar.warning("終了日を選択してください。")

elif analysis_mode == "🦠 リスク判定: 複数年比較分析":
    compare_years = st.sidebar.multiselect("比較する年を選択", list(range(2020, 2030)), default=[2023, 2024, 2025])
    planting_period = st.sidebar.date_input("分析期間（月日）", (date(2025, 9, 1), date(2025, 12, 31)))
    if isinstance(planting_period, (tuple, list)) and len(planting_period) == 2:
        start_md_date, end_md_date = planting_period[0], planting_period[1]
    else:
        start_md_date = planting_period[0] if isinstance(planting_period, (tuple, list)) else planting_period
        end_md_date   = start_md_date
        st.sidebar.warning("終了日を選択してください。")

    st.sidebar.divider()
    st.sidebar.header("🎨 グラフ表示設定")
    overlay_mode = st.sidebar.radio("複数年比較グラフの表示モード", ["🌈 通常（カラー）", "🖨️ 白黒印刷用"], horizontal=True)
    bw_mode = (overlay_mode == "🖨️ 白黒印刷用")

st.sidebar.divider()
st.sidebar.header("🌱 栽培パラメータ")
base_temp = st.sidebar.number_input("ベース温度 (℃)", min_value=0.0, max_value=15.0, value=7.0, step=0.5)

st.sidebar.divider()
st.sidebar.header("⏱️ リスク期間の推定方法")
risk_period_method = st.sidebar.radio(
    "推定方法を選択",
    ["積算温度(GDD)で推定", "植え付け後日数で指定"]
)

if risk_period_method == "積算温度(GDD)で推定":
    gdd_start = st.sidebar.number_input("開始 GDD", value=300, step=10)
    gdd_end   = st.sidebar.number_input("終了 GDD", value=600, step=10)
    risk_day_start, risk_day_end = 40, 70
else:
    risk_day_start = st.sidebar.number_input("開始日数 (植え付け後 日数)", value=40, step=1)
    risk_day_end   = st.sidebar.number_input("終了日数 (植え付け後 日数)", value=70, step=1)
    gdd_start, gdd_end = 300, 600

st.sidebar.divider()
st.sidebar.header("🌧️ リスク判定閾値（降水量）")
threshold_high = st.sidebar.number_input("高リスク境界値 (mm)", value=THRESHOLD_HIGH_DEFAULT)
threshold_med  = st.sidebar.number_input("中リスク境界値 (mm)", value=THRESHOLD_MED_DEFAULT)

st.sidebar.divider()
st.sidebar.header("💧 先行降水量補正 (リスク開始前)")
use_antecedent = st.sidebar.checkbox("先行降水補正を使用する", value=True)
if use_antecedent:
    antecedent_days      = st.sidebar.number_input("集計期間（日前）", value=7, step=1)
    antecedent_relief_mm = st.sidebar.number_input("軽減閾値 (mm)", value=ANTECEDENT_RELIEF_MM_DEFAULT, step=5)
else:
    antecedent_days, antecedent_relief_mm = 7, ANTECEDENT_RELIEF_MM_DEFAULT

st.sidebar.divider()
st.sidebar.header("❄️ 低温補正（地温考慮）")
use_low_temp = st.sidebar.checkbox("低温補正を使用する", value=True)
if use_low_temp:
    low_temp_threshold = st.sidebar.number_input("低温基準 (℃)", value=10.0, step=0.5)
    low_temp_days      = st.sidebar.number_input("軽減に必要な日数", value=3, step=1)
else:
    low_temp_threshold, low_temp_days = 10.0, 3


# ============================================================
# AMeDASデータをパース
# ============================================================
def load_amedas_weather_df(pasted_data):
    if not pasted_data or not pasted_data.strip():
        st.error("⚠️ データがありません。サイドバーにデータを貼り付けてください。")
        st.stop()

    weather_df, col_labels, col_map = parse_jma_to_weather_df(pasted_data)

    with st.expander("🔍 列検出ログ（変換がおかしい場合に確認）", expanded=False):
        debug_rows = [
            {"変数名": k, "列番号": v, "ヘッダー": col_labels[v] if v is not None else "（未検出）"}
            for k, v in col_map.items()
        ]
        st.dataframe(pd.DataFrame(debug_rows))
        st.write(f"**データ件数:** {len(weather_df)} 行")
        if not weather_df.empty:
            st.write(f"**期間:** {weather_df['time'].min().date()} 〜 {weather_df['time'].max().date()}")

    return weather_df


# ============================================================
# 日付処理ユーティリティ
# ============================================================
def get_safe_date(year, month, day):
    try: return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29: return date(year, 2, 28)
        raise

# ============================================================
# 気象データ取得 (Open-Meteo)
# ============================================================
DAILY_PARAMS = "temperature_2m_mean,precipitation_sum"

@st.cache_data(ttl=259200)
def _fetch_archive(lat, lon, start, end):
    url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}&timezone=Asia%2FTokyo&start_date={start}&end_date={end}"
    return requests.get(url, timeout=15).json()

@st.cache_data(ttl=21600)
def _fetch_forecast(lat, lon, start, end):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily={DAILY_PARAMS}&timezone=Asia%2FTokyo&start_date={start}&end_date={end}"
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
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))

    fcast_start = max(fetch_start, cutoff_arc + timedelta(days=1))
    fcast_end   = min(fetch_end, today + timedelta(days=15))
    if fcast_start <= fcast_end:
        data = _fetch_forecast(lat, lon, fcast_start.strftime('%Y-%m-%d'), fcast_end.strftime('%Y-%m-%d'))
        if 'daily' in data: frames.append(pd.DataFrame(data['daily']))

    if not frames: raise ValueError("気象データが見つかりません。")
    df = pd.concat(frames).drop_duplicates('time').sort_values('time').reset_index(drop=True)
    df['time'] = pd.to_datetime(df['time'])
    
    # APIデータの場合、平年値カラムは空として追加
    df['temperature_2m_mean_normal'] = float('nan')
    df['precipitation_sum_normal'] = float('nan')
    
    return df


# ============================================================
# リスク計算関数
# ============================================================
def calculate_scab_risk(p_date, weather_df, b_temp, g_start, g_end, t_high, t_med,
                        use_ante, ante_days, ante_relief_mm, use_temp, temp_thresh, temp_days,
                        risk_method="積算温度(GDD)で推定", day_start=40, day_end=70):
    df_after = weather_df[weather_df['time'] >= pd.Timestamp(p_date)].copy()
    if df_after.empty: return None

    # --- 実データによる計算 ---
    df_after['gdd_daily'] = (df_after['temperature_2m_mean'].fillna(0) - b_temp).clip(lower=0)
    df_after['gdd_cum']   = df_after['gdd_daily'].cumsum()

    if risk_method == "積算温度(GDD)で推定":
        start_w = df_after[df_after['gdd_cum'] >= g_start]
        if start_w.empty:
            last_date, today = df_after['time'].iloc[-1].date(), date.today()
            if last_date >= today + timedelta(days=15): return {'status': 'GDD未到達（データ不足）', 'planting_date': p_date}
            else: return {'status': 'GDD未到達（予報期間外）', 'planting_date': p_date}
        start_date_w = start_w.iloc[0]['time']
        end_w        = df_after[df_after['gdd_cum'] >= g_end]
        reached_end  = not end_w.empty
        end_date_w   = end_w.iloc[0]['time'] if reached_end else df_after['time'].iloc[-1]
    else:
        start_date_w    = pd.Timestamp(p_date) + pd.Timedelta(days=day_start)
        target_end_date = pd.Timestamp(p_date) + pd.Timedelta(days=day_end)
        if start_date_w > df_after['time'].max():
            last_date, today = df_after['time'].iloc[-1].date(), date.today()
            if last_date >= today + timedelta(days=15): return {'status': '開始日数未到達（データ不足）', 'planting_date': p_date}
            else: return {'status': '開始日数未到達（予報期間外）', 'planting_date': p_date}
        reached_end = target_end_date <= df_after['time'].max()
        end_date_w  = target_end_date if reached_end else df_after['time'].max()

    risk_df             = df_after[(df_after['time'] >= start_date_w) & (df_after['time'] <= end_date_w)]
    total_days_in_risk  = len(risk_df)
    missing_temp_days   = risk_df['temperature_2m_mean'].isna().sum()
    missing_precip_days = risk_df['precipitation_sum'].isna().sum()
    total_precip        = risk_df['precipitation_sum'].fillna(0).sum()
    low_temp_count      = int((risk_df['temperature_2m_mean'].fillna(999) <= temp_thresh).sum())

    ante_df = weather_df[
        (weather_df['time'] >= start_date_w - pd.Timedelta(days=ante_days)) &
        (weather_df['time'] <= start_date_w - pd.Timedelta(days=1))
    ]
    antecedent_precip = ante_df['precipitation_sum'].fillna(0).sum() if not ante_df.empty else 0.0
    ante_available    = not ante_df.empty

    base_risk_v    = 2 if total_precip < t_high else (1 if total_precip < t_med else 0)
    ante_corrected = use_ante and ante_available and antecedent_precip >= ante_relief_mm
    temp_corrected = use_temp and low_temp_count >= temp_days
    any_correction = ante_corrected or temp_corrected
    corrected_risk_v = max(0, base_risk_v - (1 if any_correction else 0))
    risk_l, risk_c = RISK_MAP[corrected_risk_v]

    res = {
        'status': '判定完了', 'planting_date': p_date,
        'start_date_w': start_date_w, 'end_date_w': end_date_w,
        'reached_end': reached_end, 'total_precip': total_precip,
        'antecedent_precip': antecedent_precip, 'low_temp_count': low_temp_count,
        'ante_available': ante_available, 'ante_corrected': ante_corrected,
        'temp_corrected': temp_corrected, 'any_correction': any_correction,
        'base_risk_value': base_risk_v, 'risk_value': corrected_risk_v,
        'risk_level': risk_l, 'risk_color': risk_c,
        'missing_temp_days': int(missing_temp_days),
        'missing_precip_days': int(missing_precip_days),
        'total_days_in_risk': total_days_in_risk,
        'risk_df': risk_df,
        'plot_df': df_after[df_after['time'] <= end_date_w],
        'has_normal': False
    }

    # --- 平年値データの計算 ---
    if 'temperature_2m_mean_normal' in df_after.columns and 'precipitation_sum_normal' in df_after.columns:
        if not df_after['precipitation_sum_normal'].isna().all():
            res['has_normal'] = True
            df_after['gdd_daily_normal'] = (df_after['temperature_2m_mean_normal'].fillna(0) - b_temp).clip(lower=0)
            df_after['gdd_cum_normal']   = df_after['gdd_daily_normal'].cumsum()

            if risk_method == "積算温度(GDD)で推定":
                start_w_n = df_after[df_after['gdd_cum_normal'] >= g_start]
                start_date_w_n = start_w_n.iloc[0]['time'] if not start_w_n.empty else df_after['time'].iloc[-1]
                end_w_n = df_after[df_after['gdd_cum_normal'] >= g_end]
                end_date_w_n = end_w_n.iloc[0]['time'] if not end_w_n.empty else df_after['time'].iloc[-1]
            else:
                start_date_w_n = pd.Timestamp(p_date) + pd.Timedelta(days=day_start)
                end_date_w_n = pd.Timestamp(p_date) + pd.Timedelta(days=day_end)
            
            risk_df_normal = df_after[(df_after['time'] >= start_date_w_n) & (df_after['time'] <= end_date_w_n)]
            res['total_precip_normal'] = risk_df_normal['precipitation_sum_normal'].fillna(0).sum()

    return res


def apply_date_axis(ax, span_days=None):
    interval = 10
    if span_days is not None:
        if span_days > 150: interval = 20
        elif span_days > 60: interval = 10
        elif span_days > 30: interval = 5
        else: interval = 2
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45); lbl.set_ha('right'); lbl.set_color('white')


def plot_period_analysis(results_df, t_high, t_med, title_suffix=""):
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#0e1117"); ax.set_facecolor("#1a1d24"); ax.tick_params(colors="white")
    for spine in ax.spines.values(): spine.set_color("#444")

    df_plot = results_df[results_df['status'] == '判定完了'].copy()
    if df_plot.empty: return fig

    df_plot['planting_date'] = pd.to_datetime(df_plot['planting_date'])

    # 平年値のプロット（データがある場合）
    if 'has_normal' in df_plot.columns and df_plot['has_normal'].any():
        ax.plot(df_plot['planting_date'], df_plot['total_precip_normal'], color="#00d4aa", linestyle="--", linewidth=2.0, alpha=0.8, label="平年値（同条件下の推定降水量）", zorder=2)

    ax.scatter(df_plot['planting_date'], df_plot['total_precip'], c=df_plot['risk_color'], s=50, edgecolors='white', linewidths=0.5, zorder=3)
    ax.plot(df_plot['planting_date'], df_plot['total_precip'], color="white", alpha=0.3, linestyle="-", linewidth=1.5, zorder=2)

    if 'ante_corrected' in df_plot.columns:
        a_corr = df_plot[df_plot['ante_corrected'] == True]
        if not a_corr.empty:
            ax.scatter(a_corr['planting_date'], a_corr['total_precip'], marker='D', s=100, edgecolors='white', facecolors='none', linewidths=1.2, zorder=4, alpha=0.9)
    if 'temp_corrected' in df_plot.columns:
        t_corr = df_plot[df_plot['temp_corrected'] == True]
        if not t_corr.empty:
            ax.scatter(t_corr['planting_date'], t_corr['total_precip'], marker='s', s=130, edgecolors='cyan', facecolors='none', linewidths=1.5, zorder=5, alpha=0.9)

    ax.axhline(t_high, color="#FF4B4B", linestyle=":", linewidth=1.5, alpha=0.8)
    ax.axhline(t_med,  color="#FFA500", linestyle=":", linewidth=1.5, alpha=0.8)

    ax.set_ylabel("リスク期内の積算降水量 (mm)  ※少ないほど高リスク", color="white")
    ax.set_xlabel("植え付け日", color="white")
    ax.yaxis.label.set_color("white")
    if title_suffix: ax.set_title(title_suffix, color="white", fontsize=12)
    apply_date_axis(ax, span_days=(df_plot['planting_date'].max() - df_plot['planting_date'].min()).days)

    handles = [
        mpatches.Patch(color="#FF4B4B", label="高リスク (High)：乾燥"),
        mpatches.Patch(color="#FFA500", label="中リスク (Medium)"),
        mpatches.Patch(color="#0068C9", label="低リスク (Low)：湿潤"),
    ]
    if 'has_normal' in df_plot.columns and df_plot['has_normal'].any():
        handles.append(mlines.Line2D([], [], color="#00d4aa", linestyle="--", linewidth=2.0, label="平年値"))

    ax.legend(handles=handles, loc="best", facecolor="#1a1d24", labelcolor="white")
    plt.tight_layout()
    return fig


def plot_multiyear_overlay(results_df, t_high, t_med, compare_years, start_md_date, bw_mode=False):
    BW_STYLES    = [("-",1.8,"o",7,"none"), ("--",1.8,"v",8,"none"), ("-.",1.8,"s",7,"none"), (":",2.0,"D",7,"none"), ("-",1.8,"^",8,"full")]
    BW_GRAYS     = ["#000000", "#333333", "#555555", "#777777", "#111111"]
    COLOR_STYLES = [("-",1.8,"o",7,"none"), ("--",1.8,"v",8,"none"), ("-.",1.8,"s",7,"none"), (":",2.0,"D",7,"none"), ("-",1.8,"^",8,"full")]
    COLOR_PALETTE= ["#4fc3f7", "#ef5350", "#66bb6a", "#ffa726", "#ab47bc"]

    fig, ax = plt.subplots(figsize=(13, 6))
    if bw_mode:
        fig.patch.set_facecolor("white"); ax.set_facecolor("white")
        fg_main, spine_c, grid_c = "black", "#aaaaaa", "#dddddd"
        h_line_color, m_line_color = "black", "#555555"
    else:
        fig.patch.set_facecolor("#0e1117"); ax.set_facecolor("#1a1d24")
        fg_main, spine_c, grid_c = "white", "#444444", "#2a2d34"
        h_line_color, m_line_color = "#FF4B4B", "#FFA500"

    ax.tick_params(colors=fg_main)
    ax.xaxis.label.set_color(fg_main); ax.yaxis.label.set_color(fg_main); ax.title.set_color(fg_main)
    for spine in ax.spines.values(): spine.set_color(spine_c)

    legend_handles = []
    
    # 平年値データのプロット（一番最初のみ描画）
    normal_plotted = False

    for i, y in enumerate(sorted(compare_years)):
        df_y = results_df[
            results_df['target_year'].isin([f"{y}年", f"{y}/{y+1}シーズン"]) &
            (results_df['status'] == '判定完了')
        ].copy()
        if df_y.empty: continue
        df_y['planting_date'] = pd.to_datetime(df_y['planting_date'])

        def to_md_date(d):
            base_y = 2000 if d.month >= start_md_date.month else 2001
            try: return d.replace(year=base_y)
            except ValueError: return d.replace(year=base_y, day=28)

        df_y['md_date'] = df_y['planting_date'].apply(to_md_date)
        df_y = df_y.sort_values('md_date')

        # 平年値が利用可能なら描画
        if not normal_plotted and 'has_normal' in df_y.columns and df_y['has_normal'].any():
            normal_col = "#777777" if bw_mode else "#00d4aa"
            ax.plot(df_y['md_date'], df_y['total_precip_normal'], color=normal_col, linestyle="--", linewidth=2.0, alpha=0.8, zorder=2)
            legend_handles.append(mlines.Line2D([], [], color=normal_col, linestyle="--", linewidth=2.0, label="平年値"))
            normal_plotted = True

        ls, lw, mk, ms, fs = BW_STYLES[i % len(BW_STYLES)] if bw_mode else COLOR_STYLES[i % len(COLOR_STYLES)]
        color = BW_GRAYS[i % len(BW_GRAYS)] if bw_mode else COLOR_PALETTE[i % len(COLOR_PALETTE)]
        mfc = color if fs == "full" else "none"

        ax.plot(df_y['md_date'], df_y['total_precip'], color=color, linestyle=ls, linewidth=lw, alpha=0.9, zorder=3)
        ax.scatter(df_y['md_date'], df_y['total_precip'], marker=mk, s=ms**2, zorder=4, facecolors=mfc, edgecolors=color, linewidths=1.5)
        legend_handles.append(mlines.Line2D([], [], color=color, linestyle=ls, linewidth=lw, marker=mk, markersize=ms,
                                             fillstyle=fs, markerfacecolor=mfc, markeredgecolor=color,
                                             label=df_y['target_year'].iloc[0]))

    ax.axhline(t_high, color=h_line_color, linestyle=":", linewidth=1.8, alpha=0.85)
    ax.axhline(t_med,  color=m_line_color, linestyle=":", linewidth=1.5, alpha=0.80)

    ax.xaxis.set_major_locator(mdates.DayLocator(interval=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(45); lbl.set_ha('right'); lbl.set_color(fg_main)

    ax.set_ylabel("リスク期内の積算降水量 (mm)")
    ax.set_xlabel("植え付け日（月/日）")
    ax.legend(handles=legend_handles, loc="best", facecolor="white" if bw_mode else "#1a1d24",
              edgecolor=spine_c, labelcolor=fg_main, framealpha=0.92)
    ax.grid(axis='y', color=grid_c, linewidth=0.7, linestyle='-', zorder=0)
    plt.tight_layout()
    return fig


def build_csv(results_df: pd.DataFrame, ante_days: int) -> bytes:
    cols_src = ['target_year', 'planting_date', 'start_date_w', 'end_date_w', 'reached_end',
                'antecedent_precip', 'ante_corrected', 'low_temp_count', 'temp_corrected',
                'total_precip', 'total_precip_normal', 'missing_precip_days', 'base_risk_value', 'risk_level']
    show_df = results_df[results_df['status'] == '判定完了'][[c for c in cols_src if c in results_df.columns]].copy()
    for c in ['planting_date', 'start_date_w', 'end_date_w']:
        if c in show_df: show_df[c] = pd.to_datetime(show_df[c]).dt.strftime('%Y/%m/%d')
    if 'reached_end'       in show_df: show_df['reached_end']    = show_df['reached_end'].map({True: '到達', False: '未到達'})
    if 'ante_corrected'    in show_df: show_df['ante_corrected']  = show_df['ante_corrected'].map({True: '補正あり', False: '-'})
    if 'temp_corrected'    in show_df: show_df['temp_corrected']  = show_df['temp_corrected'].map({True: '補正あり', False: '-'})
    if 'total_precip'      in show_df: show_df['total_precip']    = show_df['total_precip'].round(1)
    if 'total_precip_normal' in show_df: show_df['total_precip_normal'] = show_df['total_precip_normal'].round(1)
    if 'antecedent_precip' in show_df: show_df['antecedent_precip'] = show_df['antecedent_precip'].round(1)
    if 'base_risk_value'   in show_df: show_df['base_risk_value'] = show_df['base_risk_value'].map({2: '高(High)', 1: '中(Medium)', 0: '低(Low)'})
    
    rename_dict = {
        'target_year': '対象年', 'planting_date': '植え付け日',
        'start_date_w': 'リスク期開始日', 'end_date_w': 'リスク期終了日',
        'reached_end': '期間終了到達',
        'antecedent_precip': f'リスク開始前{ante_days}日間降水量(mm)',
        'ante_corrected': 'リスク開始前降水補正', 'low_temp_count': 'リスク期 低温日数(日)',
        'temp_corrected': '低温補正', 'total_precip': 'リスク期積算降水量(mm)',
        'total_precip_normal': '平年積算降水量(mm)',
        'missing_precip_days': 'リスク期 降水欠測日数(日)',
        'base_risk_value': '基本リスク(補正前)', 'risk_level': 'リスクレベル(補正後)',
    }
    show_df.rename(columns=rename_dict, inplace=True)
    return show_df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')


# ============================================================
# 実行処理
# ============================================================
if st.sidebar.button("▶ 実行 (分析・表示)", type="primary"):

    if risk_period_method == "積算温度(GDD)で推定" and gdd_start >= gdd_end:
        st.error("GDD開始閾値は終了閾値より小さい値を設定してください。"); st.stop()
    if risk_period_method == "植え付け後日数で指定" and risk_day_start >= risk_day_end:
        st.error("開始日数は終了日数より小さい値を設定してください。"); st.stop()
    if threshold_high >= threshold_med:
        st.error("高リスク境界値は中リスク境界値より小さい値を設定してください。"); st.stop()

    # ──────────────────────────────────────────────────────
    # 🦠 単一日の判定
    # ──────────────────────────────────────────────────────
    if analysis_mode == "🦠 リスク判定: 単一日の判定":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try: weather_df = fetch_weather_data(lat, lon, planting_date, pre_fetch_days=antecedent_days + 5)
                except Exception as e: st.error(f"気象データ取得エラー: {e}"); st.stop()
            else:
                try: weather_df = load_amedas_weather_df(pasted_data)
                except Exception as e: st.error(e); st.stop()

        res = calculate_scab_risk(
            planting_date, weather_df, base_temp, gdd_start, gdd_end,
            threshold_high, threshold_med, use_antecedent, antecedent_days,
            antecedent_relief_mm, use_low_temp, low_temp_threshold, low_temp_days,
            risk_period_method, risk_day_start, risk_day_end
        )

        if res is None: st.warning("指定日の気象データが存在しません。"); st.stop()
        if res['status'] != '判定完了':
            if res['status'] in ['GDD未到達（予報期間外）', '開始日数未到達（予報期間外）']:
                st.warning(f"⚠️ {planting_date.strftime('%Y/%m/%d')} 植え付けでは、予報期間内に条件（{res['status'].split('（')[0]}）に到達しません。")
            else: st.warning(f"リスク期に達していません: {res['status']}")
            st.stop()

        st.subheader(f"📊 判定結果（植え付け日: {planting_date.strftime('%Y/%m/%d')}）")
        st.caption(f"データソース: {data_source} ／ 地点: {loc_name}")
        st.info("ℹ️ リスク期の降水量が少ないほど **高リスク（乾燥条件）** と判定します。")

        col1, col2, col3, col4, col5 = st.columns(5)
        if risk_period_method == "積算温度(GDD)で推定":
            col1.metric("リスク期 開始", res['start_date_w'].strftime('%Y/%m/%d'), f"{gdd_start} GDD")
            col2.metric("リスク期 終了", res['end_date_w'].strftime('%Y/%m/%d'), f"{gdd_end} GDD" if res['reached_end'] else "進行中/予報端")
        else:
            col1.metric("リスク期 開始", res['start_date_w'].strftime('%Y/%m/%d'), f"{risk_day_start}日後")
            col2.metric("リスク期 終了", res['end_date_w'].strftime('%Y/%m/%d'), f"{risk_day_end}日後" if res['reached_end'] else "進行中/予報端")

        delta_precip = f"{res['total_precip'] - res['total_precip_normal']:.1f} mm (対平年)" if res.get('has_normal') else None
        col3.metric("リスク期 積算降水量", f"{res['total_precip']:.1f} mm", delta=delta_precip, delta_color="inverse")
        col4.metric(f"低温日数（≤{low_temp_threshold}℃）", f"{res['low_temp_count']} 日")
        col5.metric(f"リスク開始前{antecedent_days}日間降水", f"{res['antecedent_precip']:.1f} mm" if res['ante_available'] else "データなし")

        if res['any_correction']:
            reasons = []
            if res['ante_corrected']: reasons.append(f"リスク開始前降水量 ≥ {antecedent_relief_mm}mm")
            if res['temp_corrected']: reasons.append(f"低温日数 ≥ {low_temp_days}日")
            st.success(f"✅ 補正適用（最大1段階軽減）: {' ／ '.join(reasons)}\n\n基本リスク: **{RISK_MAP[res['base_risk_value']][0]}** → 補正後: **{res['risk_level']}**")

        st.markdown(f"""
        <div style="background-color:{res['risk_color']}18; border-left:5px solid {res['risk_color']}; padding:15px; border-radius:5px; margin-top:10px;">
            <h3 style="color:{res['risk_color']}; margin:0;">最終判定: {res['risk_level']}</h3>
            <p style="margin-top:8px; font-size:15px;">リスク期積算降水量: {res['total_precip']:.1f} mm ／ 基本リスク(補正前): {RISK_MAP[res['base_risk_value']][0]}</p>
        </div>
        """, unsafe_allow_html=True)

        st.subheader("📈 気象データの推移（リスク期を強調表示）")
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        fig.patch.set_facecolor("#0e1117")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1a1d24"); ax.tick_params(colors="white")
            for spine in ax.spines.values(): spine.set_color("#444")

        r_start = matplotlib.dates.date2num(res['start_date_w'])
        r_end   = matplotlib.dates.date2num(res['end_date_w'])

        def highlight_risk(ax, col):
            ax.axvspan(r_start, r_end, color=col, alpha=0.15, label="感染リスク期")
            ax.axvline(r_start, color=col, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.axvline(r_end,   color=col, linestyle="--", linewidth=1.2, alpha=0.8)

        ax1.plot(res['plot_df']['time'], res['plot_df']['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
        if risk_period_method == "積算温度(GDD)で推定":
            ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7, label=f"GDD開始 {gdd_start}")
            ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7, label=f"GDD終了 {gdd_end}")
        highlight_risk(ax1, res['risk_color'])
        ax1.set_ylabel("積算温度 (℃·day)", color="white")
        ax1.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")
        ax1.yaxis.label.set_color("white")

        c_bar = [res['risk_color'] if (res['start_date_w'] <= t <= res['end_date_w']) else "#4a90d9"
                 for t in res['plot_df']['time']]
        ax2.bar(res['plot_df']['time'], res['plot_df']['precipitation_sum'].fillna(0), color=c_bar, width=0.8, alpha=0.85)
        highlight_risk(ax2, res['risk_color'])

        if not res['risk_df'].empty:
            cum_df = res['risk_df'].copy()
            cum_df['cum_precip'] = cum_df['precipitation_sum'].fillna(0).cumsum()
            ax2b = ax2.twinx()
            ax2b.plot(cum_df['time'], cum_df['cum_precip'], color="white", linewidth=1.5, alpha=0.8, label="リスク期積算降水量")
            ax2b.set_ylabel("リスク期積算降水量 (mm)", color="white")
            ax2b.yaxis.label.set_color("white"); ax2b.tick_params(colors="white")
            for spine in ax2b.spines.values(): spine.set_color("#444")
            ax2b.axhline(threshold_high, color="#FF4B4B", linestyle=":", linewidth=1.2, alpha=0.7)
            ax2b.axhline(threshold_med,  color="#FFA500", linestyle=":", linewidth=1.2, alpha=0.7)
            ax2b.legend(loc="upper left", facecolor="#1a1d24", labelcolor="white")

        ax2.set_ylabel("日降水量 (mm)", color="white")
        ax2.set_xlabel("日付", color="white")
        apply_date_axis(ax2, span_days=(res['end_date_w'] - res['start_date_w']).days)
        plt.tight_layout()
        st.pyplot(fig)

    # ──────────────────────────────────────────────────────
    # 🦠 植え付け期間分析
    # ──────────────────────────────────────────────────────
    elif analysis_mode == "🦠 リスク判定: 植え付け期間分析":
        with st.spinner("気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try: weather_df = fetch_weather_data(lat, lon, planting_date, analysis_end_date, pre_fetch_days=antecedent_days + 5)
                except Exception as e: st.error(f"気象データ取得エラー: {e}"); st.stop()
            else:
                try: weather_df = load_amedas_weather_df(pasted_data)
                except Exception as e: st.error(e); st.stop()

        date_list = [planting_date + timedelta(days=x) for x in range((analysis_end_date - planting_date).days + 1)]
        results_list, bar = [], st.progress(0)

        for i, p_date in enumerate(date_list):
            res = calculate_scab_risk(
                p_date, weather_df, base_temp, gdd_start, gdd_end,
                threshold_high, threshold_med, use_antecedent, antecedent_days,
                antecedent_relief_mm, use_low_temp, low_temp_threshold, low_temp_days,
                risk_period_method, risk_day_start, risk_day_end
            )
            if res: results_list.append({k: v for k, v in res.items() if k not in {'risk_df', 'plot_df'}})
            if i % max(1, len(date_list)//20) == 0:
                bar.progress((i+1)/len(date_list), text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(results_list)
        if results_df.empty: st.warning("データがありません。"); st.stop()

        st.subheader("📈 植え付け日による感染リスクの変化")
        if 'has_normal' in results_df.columns and results_df['has_normal'].any():
            st.info("💡 提供されたデータに平年値が含まれているため、平年の推定リスク（破線）を表示しています。")
            
        st.pyplot(plot_period_analysis(results_df, threshold_high, threshold_med))

        csv_data = build_csv(results_df, antecedent_days)
        st.download_button("📥 期間分析結果をCSVでダウンロード", csv_data,
                           file_name=f"scab_risk_period_{loc_name}.csv", mime="text/csv")

    # ──────────────────────────────────────────────────────
    # 🦠 複数年比較分析
    # ──────────────────────────────────────────────────────
    elif analysis_mode == "🦠 リスク判定: 複数年比較分析":
        if not compare_years: st.error("⚠️ 比較する年を選択してください。"); st.stop()

        is_cross_year = (start_md_date.month > end_md_date.month) or \
                        (start_md_date.month == end_md_date.month and start_md_date.day > end_md_date.day)
        overall_start = get_safe_date(min(compare_years), start_md_date.month, start_md_date.day)
        overall_end   = get_safe_date(max(compare_years) + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)

        with st.spinner("対象となる全期間の気象データを取得・解析中..."):
            if data_source == "Open-Meteo (API自動取得)":
                try: weather_df = fetch_weather_data(lat, lon, overall_start, overall_end, pre_fetch_days=antecedent_days + 5)
                except Exception as e: st.error(e); st.stop()
            else:
                try: weather_df = load_amedas_weather_df(pasted_data)
                except Exception as e: st.error(e); st.stop()

        all_results, bar = [], st.progress(0)
        date_lists, total_days = {}, 0
        for y in sorted(compare_years):
            s_date = get_safe_date(y, start_md_date.month, start_md_date.day)
            e_date = get_safe_date(y + (1 if is_cross_year else 0), end_md_date.month, end_md_date.day)
            d_list = [s_date + timedelta(days=x) for x in range((e_date - s_date).days + 1)]
            date_lists[y] = d_list; total_days += len(d_list)

        processed_days = 0
        for y in sorted(compare_years):
            season_label = f"{y}/{y+1}シーズン" if is_cross_year else f"{y}年"
            for p_date in date_lists[y]:
                res = calculate_scab_risk(
                    p_date, weather_df, base_temp, gdd_start, gdd_end,
                    threshold_high, threshold_med, use_antecedent, antecedent_days,
                    antecedent_relief_mm, use_low_temp, low_temp_threshold, low_temp_days,
                    risk_period_method, risk_day_start, risk_day_end
                )
                if res:
                    row = {k: v for k, v in res.items() if k not in {'risk_df', 'plot_df'}}
                    row['target_year'] = season_label
                    all_results.append(row)
                processed_days += 1
                if processed_days % max(1, total_days//20) == 0:
                    bar.progress(processed_days/total_days, text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        bar.empty()

        results_df = pd.DataFrame(all_results)
        if results_df.empty: st.warning("データがありません。"); st.stop()

        st.subheader("📈 複数年比較 感染リスクの変化")
        if 'has_normal' in results_df.columns and results_df['has_normal'].any():
            st.info("💡 提供されたデータに平年値が含まれているため、平年のベースライン（破線）を表示しています。")
            
        st.pyplot(plot_multiyear_overlay(results_df, threshold_high, threshold_med, compare_years, start_md_date, bw_mode=bw_mode))

        for season_label in sorted(results_df['target_year'].unique()):
            st.markdown(f"**{season_label}**")
            st.pyplot(plot_period_analysis(results_df[results_df['target_year'] == season_label],
                                           threshold_high, threshold_med, title_suffix=season_label))

        csv_data = build_csv(results_df, antecedent_days)
        st.divider()
        st.download_button("📥 複数年比較の全分析結果をCSVでダウンロード", csv_data,
                           file_name=f"scab_risk_multiyear_{loc_name}.csv", mime="text/csv")

import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta, date
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
マルチ栽培を前提とし、植え付け日からの積算温度（ベース温度可変）で塊茎の初期肥大期を推定します。  
**積算温度が設定GDD閾値（デフォルト: 300〜600度日）**の期間を「感染リスク期」とし、その間の降水量でリスクを判定します。
""")

# ========== サイドバー ==========
st.sidebar.header("🗺️ 分析モードと地点")

# --- 分析モードの選択 ---
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

# --- モードによって入力を切り替え ---
if analysis_mode == "単一日の判定":
    planting_date = st.sidebar.date_input(
        "植え付け日",
        datetime.today() - timedelta(days=40)
    )
    analysis_end_date = None
else:
    # 期間入力
    default_period = (date.today() - timedelta(days=90), date.today() - timedelta(days=30))
    planting_period = st.sidebar.date_input(
        "植え付け分析期間（開始日〜終了日）",
        default_period,
        help="分析したい植え付け日の範囲を選択してください。"
    )
    if len(planting_period) == 2:
        planting_date, analysis_end_date = planting_period
    else:
        planting_date, analysis_end_date = planting_period[0], planting_period[0] # 初期状態
        st.sidebar.warning("開始日と終了日を選択してください。")

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

# 過去データ
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

# 予報データ
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

def fetch_weather_data(lat, lon, start_date, end_analysis_date=None):
    """データを結合するラッパー関数。
    end_analysis_dateが指定された場合は、その日の120日後までデータを取得する。
    """
    if end_analysis_date is None:
        fetch_end_date = start_date + timedelta(days=120)
    else:
        fetch_end_date = end_analysis_date + timedelta(days=120) # 分析終了日から十分な期間を取る

    today      = datetime.today().date()
    cutoff_arc = today - timedelta(days=5)
    
    frames = []
    # --- 過去分 (archive) ---
    if start_date <= cutoff_arc:
        arc_end = min(cutoff_arc, fetch_end_date)
        try:
            data = _fetch_archive(lat, lon, start_date, arc_end)
            if 'daily' in data:
                frames.append(pd.DataFrame(data['daily']))
        except Exception as e:
            st.warning(f"過去データの取得に失敗しました: {e}")

    # --- 未来分 (forecast) ---
    fcast_start = max(start_date, cutoff_arc + timedelta(days=1))
    fcast_end   = min(fetch_end_date, today + timedelta(days=15))
    
    if fcast_start <= fcast_end:
        try:
            data = _fetch_forecast(lat, lon, fcast_start, fcast_end)
            if 'daily' in data:
                frames.append(pd.DataFrame(data['daily']))
        except Exception as e:
            st.warning(f"予報データの取得に失敗しました: {e}")

    if not frames:
        raise ValueError("取得できる期間がありません。植え付け日を確認してください。")

    df = pd.concat(frames, ignore_index=True)
    df['time'] = pd.to_datetime(df['time'])
    # 文字列日付が混在する場合があるので統一
    df = df.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    return df

# ========== リスク計算・判定関数（共通化） ==========
def calculate_scab_risk(p_date, weather_df, b_temp, g_start, g_end, t_high, t_med):
    """特定の日付を植え付け日とした場合のリスクを計算する"""
    # 植え付け日以降のデータをスライス
    df_after = weather_df[weather_df['time'] >= pd.Timestamp(p_date)].copy()
    
    if df_after.empty:
        return None # データがない

    # --- GDD計算 ---
    df_after['gdd_daily'] = (df_after['temperature_2m_mean'].fillna(0) - b_temp).clip(lower=0)
    df_after['gdd_cum']   = df_after['gdd_daily'].cumsum()

    # --- リスク期の特定 ---
    start_w_df = df_after[df_after['gdd_cum'] >= g_start]
    if start_w_df.empty:
        # まだリスク期が始まっていない
        return {
            'status': 'GDD未到達',
            'planting_date': p_date,
            'gdd_cum_max': df_after['gdd_cum'].max()
        }

    start_date_w = start_w_df.iloc[0]['time']
    end_w_df     = df_after[df_after['gdd_cum'] >= g_end]
    
    reached_end  = not end_w_df.empty
    if reached_end:
        end_date_w = end_w_df.iloc[0]['time']
    else:
        end_date_w = df_after['time'].iloc[-1] # 最新データまで

    # --- 期間内降水量 ---
    risk_df = df_after[(df_after['time'] >= start_date_w) & (df_after['time'] <= end_date_w)]
    total_precip = risk_df['precipitation_sum'].sum()

    # --- リスク判定 ---
    if total_precip < t_high:
        risk_level = "高 (High)"
        risk_color = "#FF4B4B" # Red
        risk_value = 2 # 図示用
    elif total_precip < t_med:
        risk_level = "中 (Medium)"
        risk_color = "#FFA500" # Orange
        risk_value = 1
    else:
        risk_level = "低 (Low)"
        risk_color = "#0068C9" # Blue
        risk_value = 0

    return {
        'status': '判定完了',
        'planting_date': p_date,
        'start_date_w': start_date_w,
        'end_date_w': end_date_w,
        'reached_end': reached_end,
        'total_precip': total_precip,
        'risk_level': risk_level,
        'risk_color': risk_color,
        'risk_value': risk_value,
        'risk_df': risk_df, # 1日判定グラフ用
        'plot_df': df_after[(df_after['time'] >= pd.Timestamp(p_date)) & (df_after['time'] <= end_date_w)] # 1日判定グラフ用
    }

# ========== 図の生成関数（期間分析） ==========
def plot_period_analysis(results_df, t_high, t_med):
    """期間分析の結果を図にする"""
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#1a1d24")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#444")

    # データが空の場合
    if results_df.empty:
        ax.text(0.5, 0.5, "データがありません", color="white", fontsize=15, ha='center', va='center', fontproperties=JP_FONT)
        return fig

    # 判定完了データのみ使用
    df_plot = results_df[results_df['status'] == '判定完了'].copy()
    
    if df_plot.empty:
        ax.text(0.5, 0.5, "判定を完了した日がありません", color="white", fontsize=15, ha='center', va='center', fontproperties=JP_FONT)
        return fig

    # x軸の日付を数値に変換（Matplotlib用）
    p_dates_num = matplotlib.dates.date2num(df_plot['planting_date'])

    # --- 散布図で降水量をプロット ---
    ax.scatter(p_dates_num, df_plot['total_precip'], 
               c=df_plot['risk_color'], s=50, edgecolors='white', linewidths=0.5, label="植え付け日ごとの結果")
    
    # 折れ線で繋ぐ
    ax.plot(p_dates_num, df_plot['total_precip'], color="white", alpha=0.2, linestyle="-", linewidth=1)

    # --- リスク閾値の水平線 ---
    ax.axhline(t_high, color="#FF4B4B", linestyle=":", linewidth=1.5, alpha=0.7)
    ax.axhline(t_med,  color="#FFA500",  linestyle=":", linewidth=1.5, alpha=0.7)
    
    ax.text(p_dates_num[0], t_high + 2, f"高リスク境界 {t_high}mm", color="#FF4B4B", fontsize=9, fontproperties=JP_FONT)
    ax.text(p_dates_num[0], t_med + 2,  f"中リスク境界 {t_med}mm",  color="#FFA500",  fontsize=9, fontproperties=JP_FONT)

    # --- 軸ラベル設定 ---
    ax.set_ylabel("リスク期内の積算降水量 (mm)", color="white", fontproperties=JP_FONT)
    ax.set_xlabel("植え付け日", color="white", fontproperties=JP_FONT)
    ax.yaxis.label.set_color("white")
    ax.xaxis.label.set_color("white")

    # x軸のフォーマット（日付）
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y/%m/%d'))
    fig.autofmt_xdate()

    # --- 凡例 ---
    high_patch = mpatches.Patch(color="#FF4B4B", label="高リスク (High)")
    med_patch  = mpatches.Patch(color="#FFA500", label="中リスク (Medium)")
    low_patch  = mpatches.Patch(color="#0068C9", label="低リスク (Low)")
    ax.legend(handles=[high_patch, med_patch, low_patch], loc="best", 
              prop=JP_FONT, facecolor="#1a1d24", labelcolor="white")

    plt.tight_layout()
    return fig

# ========== 実行ボタン ==========
if st.sidebar.button("▶ リスク分析を実行", type="primary"):
    with st.spinner("気象データを取得・解析中..."):
        try:
            # 期間全体の気象データを一度に取得
            start_fetch_date = planting_date
            weather_df = fetch_weather_data(lat, lon, start_fetch_date, analysis_end_date)
        except Exception as e:
            st.error(f"気象データ取得エラー: {e}")
            st.stop()

    if analysis_mode == "単一日の判定":
        # ========== 単一日の判定ロジック ==========
        result = calculate_scab_risk(
            planting_date, weather_df, 
            base_temp, gdd_start, gdd_end, 
            threshold_high, threshold_med
        )

        if result is None:
            st.error("指定された日付のデータが見つかりません。")
            st.stop()
        
        if result['status'] == 'GDD未到達':
            st.warning(f"植え付け日 {planting_date} から現在までの積算温度が {result['gdd_cum_max']:.1f} 度日であり、まだ {gdd_start} 度日に達していません（リスク期未到達）。")
            st.stop()

        # 結果表示（元のコードのまま）
        st.subheader(f"📊 判定結果（植え付け日: {planting_date.strftime('%Y/%m/%d')}）")
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("リスク期 開始", result['start_date_w'].strftime('%Y/%m/%d'), f"{gdd_start} GDD")
        col2.metric("リスク期 終了", result['end_date_w'].strftime('%Y/%m/%d'), 
                    f"{gdd_end} GDD" if result['reached_end'] else "進行中")
        col3.metric("期間中の積算降水量", f"{result['total_precip']:.1f} mm")
        col4.metric("ベース温度", f"{base_temp} ℃")

        st.markdown(f"""
        <div style="background-color:{result['risk_color']}18; border-left:5px solid {result['risk_color']}; padding:15px; border-radius:5px; margin-top:10px;">
            <h3 style="color:{result['risk_color']}; margin:0;">リスクレベル: {result['risk_level']}</h3>
            <p style="margin-top:8px; font-size:15px;">土壌が{'乾燥' if result['risk_value'] > 0 else '十分湿潤'}（{result['total_precip']:.1f} mm）。{'感染リスクに注意が必要です。' if result['risk_value'] > 0 else '拮抗菌が優占しやすい状態です。'}</p>
        </div>
        """, unsafe_allow_html=True)

        # グラフ（元のコードのまま）
        st.subheader("📈 気象データの推移（リスク期を強調表示）")
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        fig.patch.set_facecolor("#0e1117")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1a1d24")
            ax.tick_params(colors="white")
            for spine in ax.spines.values():
                spine.set_color("#444")

        risk_start_num = matplotlib.dates.date2num(result['start_date_w'])
        risk_end_num   = matplotlib.dates.date2num(result['end_date_w'])

        def highlight_risk(ax, color_code):
            ax.axvspan(risk_start_num, risk_end_num,
                       color=color_code, alpha=0.15, label="感染リスク期")
            ax.axvline(risk_start_num, color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.axvline(risk_end_num,   color=color_code, linestyle="--", linewidth=1.2, alpha=0.8)

        # 上段: 積算GDD
        ax1.plot(result['plot_df']['time'], result['plot_df']['gdd_cum'], color="#00d4aa", linewidth=2, label="積算GDD")
        ax1.axhline(gdd_start, color="#ffcc00", linestyle=":", linewidth=1, alpha=0.7)
        ax1.axhline(gdd_end,   color="#ff8800", linestyle=":", linewidth=1, alpha=0.7)
        highlight_risk(ax1, result['risk_color'])
        ax1.set_ylabel(f"積算温度 (℃·day, ベース{base_temp}℃)", color="white", fontproperties=JP_FONT)
        ax1.legend(loc="upper left", prop=JP_FONT, facecolor="#1a1d24", labelcolor="white")
        ax1.yaxis.label.set_color("white")
        
        # 下段: 日降水量
        colors_bar = [result['risk_color'] if (result['start_date_w'] <= t <= result['end_date_w']) else "#4a90d9"
                      for t in result['plot_df']['time']]
        ax2.bar(result['plot_df']['time'], result['plot_df']['precipitation_sum'].fillna(0),
                color=colors_bar, width=0.8, alpha=0.85)
        highlight_risk(ax2, result['risk_color'])
        
        # リスク期積算降水量を重ね表示
        if not result['risk_df'].empty:
            cum_precip_df = result['risk_df'].copy()
            cum_precip_df['cum_precip'] = cum_precip_df['precipitation_sum'].fillna(0).cumsum()
            ax2b = ax2.twinx()
            ax2b.plot(cum_precip_df['time'], cum_precip_df['cum_precip'],
                      color="white", linewidth=1.5, linestyle="-", alpha=0.6, label="リスク期積算降水量")
            ax2b.set_ylabel("リスク期積算降水量 (mm)", color="white", fontproperties=JP_FONT)
            ax2b.yaxis.label.set_color("white")
            ax2b.tick_params(colors="white")
            for spine in ax2b.spines.values():
                spine.set_color("#444")
            # 閾値ライン
            ax2b.axhline(threshold_high, color="#FF4B4B", linestyle=":", linewidth=1.2, alpha=0.7)
            ax2b.axhline(threshold_med,  color="#FFA500",  linestyle=":", linewidth=1.2, alpha=0.7)

        ax2.set_ylabel("日降水量 (mm)", color="white", fontproperties=JP_FONT)
        ax2.set_xlabel("日付", color="white", fontproperties=JP_FONT)
        fig.autofmt_xdate()
        plt.tight_layout()
        st.pyplot(fig)

    else:
        # ========== 植え付け期間分析ロジック ==========
        if analysis_end_date is None or planting_date == analysis_end_date:
            st.error("期間を正しく選択してください。")
            st.stop()

        st.subheader(f"📅 植え付け期間分析: {planting_date.strftime('%Y/%m/%d')} 〜 {analysis_end_date.strftime('%Y/%m/%d')}")
        
        # --- 期間内の各日でループ計算 ---
        results_list = []
        
        # 日付リストを生成
        current_p_date = planting_date
        date_list = []
        while current_p_date <= analysis_end_date:
            date_list.append(current_p_date)
            current_p_date += timedelta(days=1)
        
        # プログレスバー
        bar = st.progress(0)
        num_dates = len(date_list)
        
        for i, p_date in enumerate(date_list):
            res = calculate_scab_risk(
                p_date, weather_df, 
                base_temp, gdd_start, gdd_end, 
                threshold_high, threshold_med
            )
            if res:
                results_list.append(res)
            
            # プログレスバー更新
            if i % 5 == 0:
                bar.progress((i + 1) / num_dates, text=f"分析中... {p_date.strftime('%Y/%m/%d')}")
        
        bar.empty()
        
        if not results_list:
            st.warning("指定された期間で分析できるデータがありませんでした。")
            st.stop()
            
        results_df = pd.DataFrame(results_list)
        
        # ========== 図の出力 ==========
        st.subheader("📈 植え付け日による感染リスクの変化（図）")
        st.markdown("""
        このグラフは、横軸を**「植え付け日」**とし、その日に植えた場合の「リスク期」の**「積算降水量」**をプロットしたものです。
        点の色は、判定されたリスクレベル（青:低、橙:中、赤:高）を表します。境界線（点線）より下にある日は、降水量が少なく高リスクであることを示します。
        """)
        
        fig_period = plot_period_analysis(results_df, threshold_high, threshold_med)
        st.pyplot(fig_period)
        
        # ========== データテーブル表示（オプション） ==========
        with st.expander("📋 分析結果の詳細データテーブル"):
            # 判定完了データのみ整形して表示
            show_df = results_df[results_df['status'] == '判定完了'][
                ['planting_date', 'total_precip', 'risk_level', 'start_date_w', 'end_date_w']
            ].copy()
            show_df['planting_date'] = show_df['planting_date'].dt.strftime('%Y/%m/%d')
            show_df['start_date_w'] = show_df['start_date_w'].dt.strftime('%Y/%m/%d')
            show_df['end_date_w'] = show_df['end_date_w'].dt.strftime('%Y/%m/%d')
            
            show_df.columns = ['植え付け日', 'リスク期 降水量(mm)', 'リスクレベル', 'リスク期 開始日', 'リスク期 終了日']
            st.dataframe(show_df, use_container_width=True)

#!/usr/bin/env python3
"""
ETF 量化信号助手 - 高对比度深色主题版
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time

st.set_page_config(
    page_title="ETF信号助手",
    page_icon="📈",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# 高对比度深色主题
st.markdown("""
<style>
    /* 整体背景 */
    .stApp {
        background-color: #0b0f19;
        color: #f1f5f9;
    }
    
    /* 主标题 */
    .main-header {
        font-size: 1.7rem;
        font-weight: 800;
        color: #f8fafc !important;
        text-align: center;
        margin-bottom: 0.3rem;
    }
    .sub-header {
        font-size: 0.95rem;
        color: #94a3b8 !important;
        text-align: center;
        margin-bottom: 1.5rem;
    }
    
    /* 决策结果 */
    .decision-buy {
        background: linear-gradient(135deg, #065f46, #047857);
        color: #ecfdf5 !important;
        padding: 1.3rem;
        border-radius: 14px;
        text-align: center;
        font-size: 1.5rem;
        font-weight: 800;
        border: 1px solid #34d399;
    }
    .decision-sell {
        background: linear-gradient(135deg, #7f1d1d, #b91c1c);
        color: #fef2f2 !important;
        padding: 1.3rem;
        border-radius: 14px;
        text-align: center;
        font-size: 1.5rem;
        font-weight: 800;
        border: 1px solid #f87171;
    }
    .decision-hold {
        background: linear-gradient(135deg, #78350f, #b45309);
        color: #fffbeb !important;
        padding: 1.3rem;
        border-radius: 14px;
        text-align: center;
        font-size: 1.5rem;
        font-weight: 800;
        border: 1px solid #fbbf24;
    }
    .reason-text {
        color: #e2e8f0 !important;
        text-align: center;
        margin: 1rem 0 1.2rem;
        font-size: 1rem;
    }
    
    /* 新闻颜色 */
    .news-pos { color: #4ade80 !important; font-weight: 600; }
    .news-neg { color: #f87171 !important; font-weight: 600; }
    .news-neu { color: #94a3b8 !important; }
    
    /* 强制提高 Streamlit 组件文字对比度 */
    .stMarkdown, .stMarkdown p, .stMarkdown span, .stText, label, 
    .stRadio label, .stSelectbox label, .stTextInput label {
        color: #f1f5f9 !important;
    }
    
    /* 输入框 */
    .stTextInput input {
        background-color: #1e293b !important;
        color: #f8fafc !important;
        border: 1px solid #475569 !important;
        border-radius: 10px !important;
    }
    
    /* 单选按钮文字 */
    .stRadio > label, .stRadio div[role="radiogroup"] label {
        color: #f1f5f9 !important;
    }
    
    /* metric 卡片 */
    [data-testid="stMetricValue"] {
        color: #f8fafc !important;
        font-weight: 700 !important;
    }
    [data-testid="stMetricLabel"] {
        color: #94a3b8 !important;
    }
    
    /* info / warning 盒子 */
    .stAlert {
        background-color: #1e293b !important;
        color: #f1f5f9 !important;
        border: 1px solid #334155 !important;
    }
    
    /* 按钮 */
    .stButton > button {
        background: linear-gradient(135deg, #4f46e5, #7c3aed) !important;
        color: white !important;
        font-weight: 700 !important;
        border: none !important;
        border-radius: 10px !important;
    }
    
    /* 分隔线 */
    hr {
        border-color: #334155 !important;
    }
    
    /* expander */
    .streamlit-expanderHeader {
        color: #f1f5f9 !important;
        background-color: #1e293b !important;
    }
</style>
""", unsafe_allow_html=True)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_with_retry(symbol: str, max_retries: int = 3):
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=500)).strftime("%Y%m%d")
    last_error = None
    for attempt in range(max_retries):
        try:
            df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))
        try:
            df = ak.fund_etf_hist_em(symbol=symbol, period="daily", adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_error = e
            time.sleep(1.2)
    raise Exception(f"多次尝试后仍无法获取行情数据。最后错误: {str(last_error)[:120]}")

@st.cache_data(ttl=180, show_spinner=False)
def get_etf_data(symbol: str):
    df = fetch_with_retry(symbol)
    df = df.sort_values("日期").reset_index(drop=True)
    df["日期"] = pd.to_datetime(df["日期"])
    df["MA20"] = df["收盘"].rolling(20).mean()
    df["MA60"] = df["收盘"].rolling(60).mean()
    df["RSI"] = calculate_rsi(df["收盘"], 14)
    return df

def analyze(df):
    if len(df) < 60:
        return None, "数据不足（需要至少60个交易日）"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    golden_cross = (prev["MA20"] <= prev["MA60"]) and (latest["MA20"] > latest["MA60"])
    death_cross = (prev["MA20"] >= prev["MA60"]) and (latest["MA20"] < latest["MA60"])
    ma20_above = latest["MA20"] > latest["MA60"]
    status = {
        "日期": latest["日期"].strftime("%Y-%m-%d"),
        "收盘价": round(float(latest["收盘"]), 4),
        "MA20": round(float(latest["MA20"]), 4),
        "MA60": round(float(latest["MA60"]), 4),
        "RSI": round(float(latest["RSI"]), 2),
        "金叉": golden_cross,
        "死叉": death_cross,
        "MA20在MA60上方": ma20_above,
    }
    if golden_cross and latest["RSI"] < 65:
        tech_signal, tech_score = "买入信号（金叉 + RSI过滤通过）", 1.0
    elif death_cross:
        tech_signal, tech_score = "卖出信号（死叉）", -1.0
    elif not ma20_above:
        tech_signal, tech_score = "偏空（MA20在MA60下方）", -0.5
    elif ma20_above and latest["RSI"] < 65:
        tech_signal, tech_score = "持有/观望（趋势向上但无新金叉）", 0.3
    else:
        tech_signal, tech_score = "观望", 0.0
    return status, tech_signal, tech_score

def final_decision(tech_score, news_score):
    if news_score <= -2:
        return "【强制卖出】", "出现重大利空，无论技术信号如何，优先减仓或清仓", "sell"
    if tech_score <= -0.5:
        return "【卖出/减仓】", "技术面出现死叉或趋势转弱", "sell"
    if tech_score >= 1 and news_score >= 2:
        return "【买入】", "技术金叉成立 + 重大利好确认，允许买入", "buy"
    if tech_score >= 1 and news_score < 2:
        return "【观望（等待利好）】", "技术已出金叉，但缺少重大利好过滤器，暂不买入", "hold"
    if tech_score > 0 and news_score >= 2:
        return "【可小仓试探】", "趋势尚可且有利好，但非标准金叉，建议轻仓", "hold"
    return "【观望】", "当前无明确买卖信号，继续等待", "hold"

# 新闻相关
POSITIVE_WORDS = ["获批", "成功", "超预期", "增长", "BD", "授权", "利好", "突破", "上涨", "创新高", "通过", "积极", "强劲", "上调", "盈利", "合作", "上市"]
NEGATIVE_WORDS = ["失败", "不及预期", "下调", "警告", "利空", "下跌", "暂停", "终止", "推迟", "亏损", "减持", "风险", "调查", "处罚", "不及"]

def simple_sentiment(title: str) -> str:
    title = str(title)
    pos = sum(1 for w in POSITIVE_WORDS if w in title)
    neg = sum(1 for w in NEGATIVE_WORDS if w in title)
    if pos > neg: return "利好"
    if neg > pos: return "利空"
    return "中性"

def suggest_news_score(sentiments: list) -> int:
    if not sentiments: return 0
    score = sum(1 if s == "利好" else -1 if s == "利空" else 0 for s in sentiments)
    if score >= 3: return 2
    if score >= 1: return 1
    if score <= -3: return -2
    if score <= -1: return -1
    return 0

@st.cache_data(ttl=300, show_spinner=False)
def try_fetch_related_news(symbol: str, max_items: int = 10):
    import akshare as ak
    news_list = []
    keywords = ["创新药", "港股创新药", "信达生物", "百济神州", "药明生物", "康方生物", "石药集团"]
    try:
        try:
            df = ak.stock_news_em(symbol=symbol)
            if df is not None and not df.empty:
                for _, row in df.head(max_items).iterrows():
                    title = str(row.get("新闻标题", row.get("title", "")))
                    if title:
                        news_list.append({
                            "title": title,
                            "time": str(row.get("发布时间", row.get("时间", "")))[:16],
                            "sentiment": simple_sentiment(title)
                        })
        except Exception:
            pass
        if len(news_list) < 3:
            try:
                df = ak.news_economic_baidu(symbol="经济")
                if df is not None and not df.empty:
                    for _, row in df.head(20).iterrows():
                        title = str(row.iloc[0]) if len(row) > 0 else ""
                        if any(k in title for k in keywords):
                            news_list.append({
                                "title": title[:80],
                                "time": "",
                                "sentiment": simple_sentiment(title)
                            })
                            if len(news_list) >= max_items:
                                break
            except Exception:
                pass
    except Exception:
        pass
    return news_list[:max_items]

# ==================== 主界面 ====================
st.markdown('<div class="main-header">📈 ETF 量化信号助手</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">双均线 + RSI + 消息过滤器 · 自动拉取数据</div>', unsafe_allow_html=True)

col1, col2 = st.columns([3, 1])
with col1:
    symbol = st.text_input("输入ETF/基金代码", value="513120", placeholder="例如 513120、510300、159915").strip()
with col2:
    st.write("")
    st.write("")
    run_btn = st.button("开始分析", type="primary", use_container_width=True)

st.markdown("---")

news_list = []
suggested_score = 0
if symbol:
    with st.spinner("正在尝试获取相关新闻..."):
        news_list = try_fetch_related_news(symbol)
        if news_list:
            sentiments = [n["sentiment"] for n in news_list]
            suggested_score = suggest_news_score(sentiments)

st.markdown("#### 📰 相关新闻参考（自动获取，仅供参考）")
if news_list:
    for i, n in enumerate(news_list, 1):
        color_class = "news-pos" if n["sentiment"] == "利好" else ("news-neg" if n["sentiment"] == "利空" else "news-neu")
        st.markdown(f"{i}. <span class='{color_class}'>[{n['sentiment']}]</span> {n['title']} <small style='color:#64748b'>{n['time']}</small>", unsafe_allow_html=True)
    st.caption(f"系统根据关键词初步建议消息评分： **{suggested_score}** （你仍可手动修改）")
else:
    st.info("暂未能自动获取到相关新闻（云端接口限制较常见）。请根据你自己看到的重仓股消息手动评分。")

st.markdown("#### ✏️ 最终消息评分（可修改）")
news_options = {
    "+2 重大利好": 2,
    "+1 一般利好": 1,
    "0 无重要消息": 0,
    "-1 一般利空": -1,
    "-2 重大利空": -2
}
default_idx = list(news_options.values()).index(suggested_score) if suggested_score in news_options.values() else 2
news_label = st.radio("选择当前消息评分", list(news_options.keys()), index=default_idx, horizontal=True)
news_score = news_options[news_label]

st.markdown("---")

if run_btn:
    if not symbol:
        st.error("请输入代码")
    else:
        with st.spinner(f"正在获取 {symbol} 行情数据..."):
            try:
                df = get_etf_data(symbol)
                result = analyze(df)
                if result[0] is None:
                    st.error(result[1])
                else:
                    status, tech_signal, tech_score = result
                    decision, reason, css = final_decision(tech_score, news_score)
                    
                    if css == "buy":
                        st.markdown(f'<div class="decision-buy">{decision}</div>', unsafe_allow_html=True)
                    elif css == "sell":
                        st.markdown(f'<div class="decision-sell">{decision}</div>', unsafe_allow_html=True)
                    else:
                        st.markdown(f'<div class="decision-hold">{decision}</div>', unsafe_allow_html=True)
                    
                    st.markdown(f'<div class="reason-text">{reason}</div>', unsafe_allow_html=True)
                    
                    st.markdown("#### 📊 技术指标")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("收盘价", f"{status['收盘价']}")
                    m2.metric("RSI(14)", f"{status['RSI']}")
                    m3.metric("MA20", f"{status['MA20']}")
                    m4.metric("MA60", f"{status['MA60']}")
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric("数据日期", status["日期"])
                    c2.metric("金叉", "是 ✅" if status["金叉"] else "否")
                    c3.metric("死叉", "是 ⚠️" if status["死叉"] else "否")
                    
                    st.info(f"**技术主信号**：{tech_signal}")
                    
                    st.markdown("#### 📈 近期走势")
                    chart_df = df.tail(120)[["日期", "收盘", "MA20", "MA60"]].set_index("日期")
                    st.line_chart(chart_df)
                    
                    with st.expander("操作提醒"):
                        st.markdown("""
                        - **重大利空（-2）优先级最高**，直接卖出  
                        - **买入必须同时满足**：金叉 + RSI < 65 + 重大利好(+2)  
                        - 建议单次买入不超过总仓位 30%-40%，严格执行 8%-10% 止损  
                        - 新闻仅为辅助参考，最终以你的判断为准
                        """)
            except Exception as e:
                st.error(f"分析失败：{str(e)}")
                st.warning("行情接口可能暂时受限，请过1-2分钟重试，或换个代码测试（如 510300）。")

st.markdown("---")
st.caption("数据来源：东方财富（akshare） · 新闻为关键词辅助判断 · 仅供学习参考")

#!/usr/bin/env python3
"""
ETF 量化信号助手 (Streamlit版)
支持输入代码（如 513120），自动拉取数据并分析
双均线(MA20/MA60) + RSI(14) + 消息过滤器
"""

import streamlit as st
import akshare as ak
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

# 自定义样式
st.markdown("""
<style>
    .stApp { background-color: #0f0f1a; }
    .main-header { font-size: 1.6rem; font-weight: 700; color: #e2e8f0; text-align: center; margin-bottom: 0.2rem; }
    .sub-header { font-size: 0.9rem; color: #94a3b8; text-align: center; margin-bottom: 1.5rem; }
    .metric-card { background: #1a1a2e; padding: 1rem; border-radius: 12px; text-align: center; }
    .decision-buy { background: linear-gradient(135deg, #064e3b, #065f46); color: #6ee7b7; padding: 1.2rem; border-radius: 12px; text-align: center; font-size: 1.4rem; font-weight: 800; }
    .decision-sell { background: linear-gradient(135deg, #7f1d1d, #991b1b); color: #fca5a5; padding: 1.2rem; border-radius: 12px; text-align: center; font-size: 1.4rem; font-weight: 800; }
    .decision-hold { background: linear-gradient(135deg, #78350f, #92400e); color: #fcd34d; padding: 1.2rem; border-radius: 12px; text-align: center; font-size: 1.4rem; font-weight: 800; }
    .reason-text { color: #94a3b8; text-align: center; margin: 0.8rem 0 1.2rem; font-size: 0.95rem; }
</style>
""", unsafe_allow_html=True)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

@st.cache_data(ttl=300)  # 缓存5分钟
def get_etf_data(symbol: str, days: int = 400):
    """获取ETF/基金历史数据"""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    
    # 尝试多种接口
    try:
        df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
    except Exception:
        try:
            # 有些代码需要加市场前缀
            df = ak.fund_etf_hist_em(symbol=symbol, period="daily", adjust="qfq")
        except Exception as e:
            raise Exception(f"无法获取 {symbol} 的数据，请检查代码是否正确。错误信息: {str(e)}")
    
    if df is None or df.empty:
        raise Exception(f"获取到的 {symbol} 数据为空")
    
    df = df.sort_values("日期").reset_index(drop=True)
    df["日期"] = pd.to_datetime(df["日期"])
    
    # 计算指标
    df["MA20"] = df["收盘"].rolling(20).mean()
    df["MA60"] = df["收盘"].rolling(60).mean()
    df["RSI"] = calculate_rsi(df["收盘"], 14)
    df["成交量MA5"] = df["成交量"].rolling(5).mean()
    
    return df

def analyze(df):
    """分析技术信号"""
    if len(df) < 60:
        return None, "数据不足（需要至少60个交易日）"
    
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    golden_cross = (prev["MA20"] <= prev["MA60"]) and (latest["MA20"] > latest["MA60"])
    death_cross = (prev["MA20"] >= prev["MA60"]) and (latest["MA20"] < latest["MA60"])
    
    ma20_above = latest["MA20"] > latest["MA60"]
    price_above_ma20 = latest["收盘"] > latest["MA20"]
    volume_expand = False
    if pd.notna(latest["成交量MA5"]) and latest["成交量MA5"] > 0:
        volume_expand = latest["成交量"] >= latest["成交量MA5"] * 1.2
    
    status = {
        "日期": latest["日期"].strftime("%Y-%m-%d"),
        "收盘价": round(float(latest["收盘"]), 4),
        "MA20": round(float(latest["MA20"]), 4),
        "MA60": round(float(latest["MA60"]), 4),
        "RSI": round(float(latest["RSI"]), 2),
        "金叉": golden_cross,
        "死叉": death_cross,
        "MA20在MA60上方": ma20_above,
        "价格在MA20上方": price_above_ma20,
        "放量": volume_expand,
    }
    
    # 技术评分
    if golden_cross and latest["RSI"] < 65:
        tech_signal = "买入信号（金叉 + RSI过滤通过）"
        tech_score = 1.0
    elif death_cross:
        tech_signal = "卖出信号（死叉）"
        tech_score = -1.0
    elif not ma20_above:
        tech_signal = "偏空（MA20在MA60下方）"
        tech_score = -0.5
    elif ma20_above and latest["RSI"] < 65:
        tech_signal = "持有/观望（趋势向上但无新金叉）"
        tech_score = 0.3
    else:
        tech_signal = "观望"
        tech_score = 0.0
    
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

# ==================== 主界面 ====================
st.markdown('<div class="main-header">📈 ETF 量化信号助手</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">双均线 + RSI + 消息过滤器 · 自动拉取数据</div>', unsafe_allow_html=True)

# 输入区
col1, col2 = st.columns([3, 1])
with col1:
    symbol = st.text_input("输入ETF/基金代码", value="513120", placeholder="例如 513120、510300、159915").strip()
with col2:
    st.write("")  # 占位
    st.write("")
    run_btn = st.button("开始分析", type="primary", use_container_width=True)

# 消息评分
st.markdown("#### 📰 消息评分（前几大重仓股）")
news_options = {
    "+2 重大利好": 2,
    "+1 一般利好": 1,
    "0 无重要消息": 0,
    "-1 一般利空": -1,
    "-2 重大利空": -2
}
news_label = st.radio("选择当前消息评分", list(news_options.keys()), index=2, horizontal=True)
news_score = news_options[news_label]

st.markdown("---")

if run_btn or st.session_state.get("auto_run", False):
    if not symbol:
        st.error("请输入代码")
    else:
        with st.spinner(f"正在获取 {symbol} 数据并计算指标..."):
            try:
                df = get_etf_data(symbol)
                result = analyze(df)
                
                if result[0] is None:
                    st.error(result[1])
                else:
                    status, tech_signal, tech_score = result
                    decision, reason, css = final_decision(tech_score, news_score)
                    
                    # 决策结果
                    if css == "buy":
                        st.markdown(f'<div class="decision-buy">{decision}</div>', unsafe_allow_html=True)
                    elif css == "sell":
                        st.markdown(f'<div class="decision-sell">{decision}</div>', unsafe_allow_html=True)
                    else:
                        st.markdown(f'<div class="decision-hold">{decision}</div>', unsafe_allow_html=True)
                    
                    st.markdown(f'<div class="reason-text">{reason}</div>', unsafe_allow_html=True)
                    
                    # 关键指标
                    st.markdown("#### 📊 技术指标")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("收盘价", f"{status['收盘价']}")
                    m2.metric("RSI(14)", f"{status['RSI']}")
                    m3.metric("MA20", f"{status['MA20']}")
                    m4.metric("MA60", f"{status['MA60']}")
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric("数据日期", status["日期"])
                    c2.metric("金叉", "是" if status["金叉"] else "否")
                    c3.metric("死叉", "是" if status["死叉"] else "否")
                    
                    st.info(f"**技术主信号**：{tech_signal}")
                    
                    # 简单走势图
                    st.markdown("#### 📈 近期走势（收盘价 + 均线）")
                    chart_df = df.tail(120)[["日期", "收盘", "MA20", "MA60"]].set_index("日期")
                    st.line_chart(chart_df)
                    
                    # 提醒
                    with st.expander("操作提醒", expanded=False):
                        st.markdown("""
                        - **重大利空（-2）优先级最高**，直接卖出，不等技术信号  
                        - **买入必须同时满足**：金叉 + RSI < 65 + 重大利好(+2)  
                        - 建议单次买入不超过总仓位 **30%-40%**，严格执行 **8%-10%止损**  
                        - 本工具仅供参考，不构成投资建议，风险自担
                        """)
                        
            except Exception as e:
                st.error(f"分析失败：{str(e)}")
                st.caption("常见原因：代码不存在、网络问题、或该代码暂不支持自动拉取。可尝试其他ETF代码如 510300、159915。")

st.markdown("---")
st.caption("数据来源：东方财富（akshare） · 缓存5分钟 · 仅供学习参考")
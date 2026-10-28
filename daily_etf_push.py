#!/usr/bin/env python3
"""
每日ETF信号推送脚本（配合 GitHub Actions + Server酱）
"""

import os
import time
from datetime import datetime, timedelta
import pandas as pd
import requests

# ==================== 配置 ====================
# 从环境变量读取（GitHub Secrets）
SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")

# 自选ETF列表
ETF_LIST = ["159545", "159941", "513120", "159330", "159699", "513330"]

# 债基
BOND_CODE = "270044"

# 单独基金
OTHER_FUND = "270023"

# ==================== 工具函数 ====================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_data(symbol, max_retries=3):
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    last_error = None
    for attempt in range(max_retries):
        try:
            # 优先尝试ETF接口
            df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_error = e
            time.sleep(1.5)
        try:
            # 普通基金接口兜底
            df = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势")
            # 不同接口返回格式可能不同，这里做简单兼容
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_error = e
            time.sleep(1.2)
        try:
            df = ak.fund_etf_hist_em(symbol=symbol, period="daily", adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            last_error = e
            time.sleep(1.0)
    print(f"[警告] {symbol} 获取失败: {last_error}")
    return None

def analyze_one(symbol):
    """分析单只，返回简短结果字符串和状态"""
    try:
        df = fetch_data(symbol)
        if df is None or len(df) < 60:
            return f"• {symbol}  数据不足", "unknown"
        
        # 统一列名处理
        if "收盘" not in df.columns and "单位净值" in df.columns:
            df = df.rename(columns={"单位净值": "收盘"})
        if "日期" not in df.columns and "净值日期" in df.columns:
            df = df.rename(columns={"净值日期": "日期"})
        
        df = df.sort_values("日期").reset_index(drop=True)
        df["MA20"] = df["收盘"].rolling(20).mean()
        df["MA60"] = df["收盘"].rolling(60).mean()
        df["RSI"] = calculate_rsi(df["收盘"], 14)
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        golden = (prev["MA20"] <= prev["MA60"]) and (latest["MA20"] > latest["MA60"])
        death = (prev["MA20"] >= prev["MA60"]) and (latest["MA20"] < latest["MA60"])
        ma20_above = latest["MA20"] > latest["MA60"]
        rsi = round(float(latest["RSI"]), 1)
        
        if golden and rsi < 65:
            signal = "买入信号"
            status = "buy"
        elif death:
            signal = "卖出/减仓"
            status = "sell"
        elif not ma20_above:
            signal = "偏空观望"
            status = "sell"
        elif ma20_above:
            signal = "持有/观望"
            status = "hold"
        else:
            signal = "观望"
            status = "hold"
        
        extra = ""
        if golden:
            extra = " 金叉"
        elif death:
            extra = " 死叉"
        
        return f"• {symbol}  {signal}  RSI:{rsi}{extra}", status
    except Exception as e:
        return f"• {symbol}  分析失败", "unknown"

def get_session_label():
    """根据当前时间判断盘前/盘中/盘后"""
    now = datetime.now()
    # 这里按北京时间粗略判断（GitHub Actions 是 UTC，后面会处理）
    hour = now.hour
    if hour < 10:
        return "盘前"
    elif hour < 13:
        return "盘中"
    else:
        return "盘后"

def build_message():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    session = get_session_label()
    
    lines = [f"【ETF信号助手】{now_str} {session}", ""]
    
    # 自选ETF
    lines.append("⭐ 自选信号汇总：")
    for code in ETF_LIST:
        text, _ = analyze_one(code)
        lines.append(text)
        time.sleep(0.8)  # 避免请求过快
    
    lines.append("")
    
    # 债市晴雨表
    lines.append("📊 债市晴雨表（270044）：")
    text, _ = analyze_one(BOND_CODE)
    lines.append(text)
    
    lines.append("")
    
    # 单独基金
    lines.append("📌 其他基金（270023）：")
    text, _ = analyze_one(OTHER_FUND)
    lines.append(text)
    
    lines.append("")
    lines.append("💡 有死叉或重大利空优先减仓，本信息仅供参考")
    
    return "\n".join(lines)

def send_serverchan(title, content):
    if not SENDKEY:
        print("未配置 SERVERCHAN_SENDKEY，跳过推送")
        print("消息内容预览：")
        print(content)
        return False
    
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = {
        "title": title,
        "desp": content
    }
    try:
        resp = requests.post(url, data=data, timeout=15)
        result = resp.json()
        print("推送结果：", result)
        return result.get("code") == 0
    except Exception as e:
        print("推送失败：", e)
        return False

if __name__ == "__main__":
    print("开始生成信号...")
    content = build_message()
    print(content)
    print("-" * 40)
    
    title = f"ETF信号助手 {get_session_label()}"
    success = send_serverchan(title, content)
    if success:
        print("推送成功")
    else:
        print("推送结束（可能未配置Key或接口异常）")

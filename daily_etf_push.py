#!/usr/bin/env python3
"""
每日ETF信号推送脚本（配合 GitHub Actions + Server酱）

时区说明：GitHub Actions 的 runner 是 UTC，本脚本所有与"现在几点"有关的计算统一换算成北京时间（UTC+8），
避免消息里显示 UTC 时间、时段标签判错。

用法：
    python daily_etf_push.py --detect-slot   只判定当前北京时间落在哪个推送窗口，输出给 Actions
    python daily_etf_push.py --slot close    按指定时段（pre / mid / close / manual）推送
    python daily_etf_push.py --slot mid --preview   只生成并打印消息，不发送（不占每日额度）
    python daily_etf_push.py                 按当前北京时间自动判定时段后推送

额度提醒：Server酱免费版每天只有 5 条，正常自动推送是一天 3 条，调试时优先用 --preview。
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone

# 防御：部分环境 stdout 不是 UTF-8（比如 Windows 下是 GBK），消息里的 emoji 会让 print 直接抛异常。
# 在 Actions 里这个异常会让 job 失败，而 workflow 是先占坑再推送，失败后本时段不会重试，所以强制转 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ==================== 配置 ====================
# 从环境变量读取（GitHub Secrets）
SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")

# 自选ETF列表
ETF_LIST = ["159545", "159941", "513120", "159330", "159699", "513330"]

# 债基
BOND_CODE = "270044"

# 单独基金
OTHER_FUND = "270023"

# 北京时间：中国无夏令时，用固定 +8 偏移，不依赖 runner 的时区数据库
BEIJING_TZ = timezone(timedelta(hours=8))

# 推送窗口：时段 → (标签, 窗口起点, 窗口终点)，均为北京时间
# 每个窗口内 workflow 每 10 分钟触发一次，用来扛住 GitHub schedule 的延迟；
# workflow 是「先占坑再推送」，所以同一时段只会真正发送一次，多出来的跳全部被 cache 挡掉
SLOT_WINDOWS = {
    "pre": ("盘前", (8, 50), (9, 20)),
    "mid": ("盘中", (11, 0), (11, 30)),
    "close": ("盘后", (15, 20), (15, 50)),
}

# 取多少天历史（算 MA60 / RSI14 足够）
HISTORY_DAYS = 400

# 少于这么多根K线就不出信号
MIN_BARS = 60


# ==================== 时间工具 ====================
def beijing_now():
    """当前北京时间（不论 runner 在哪个时区）"""
    return datetime.now(BEIJING_TZ)


def detect_slot(now):
    """判断 now 落在哪个推送窗口，不在任何窗口内返回空字符串"""
    minute_of_day = now.hour * 60 + now.minute
    for name, (_label, start, end) in SLOT_WINDOWS.items():
        if start[0] * 60 + start[1] <= minute_of_day <= end[0] * 60 + end[1]:
            return name
    return ""


def slot_label(slot):
    """时段 → 中文标签"""
    if slot in SLOT_WINDOWS:
        return SLOT_WINDOWS[slot][0]
    return "手动"


def is_trade_day(now):
    """是否交易日：节假日日历拿不到就退化成"周一至周五" """
    if now.weekday() >= 5:
        return False
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        trade_dates = {str(d) for d in cal["trade_date"]}
        return now.strftime("%Y-%m-%d") in trade_dates
    except Exception as e:
        print(f"[警告] 交易日历获取失败，按工作日处理: {e}")
        return True


# ==================== 数据与指标 ====================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def fetch_data(symbol, max_retries=3):
    import akshare as ak
    now = beijing_now()
    end_date = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
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
    """分析单只，返回 (展示文本, 数据基准日)

    数据基准日形如 '10-28'，用来暴露"盘前/盘中拿到的其实还是昨天数据"这种情况。
    """
    try:
        df = fetch_data(symbol)
        if df is None or len(df) < MIN_BARS:
            return f"• {symbol}  数据不足", ""

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
        bar_date = str(latest["日期"])[:10]

        golden = (prev["MA20"] <= prev["MA60"]) and (latest["MA20"] > latest["MA60"])
        death = (prev["MA20"] >= prev["MA60"]) and (latest["MA20"] < latest["MA60"])
        ma20_above = latest["MA20"] > latest["MA60"]
        rsi = round(float(latest["RSI"]), 1)

        if golden and rsi < 65:
            signal = "买入信号"
        elif death:
            signal = "卖出/减仓"
        elif not ma20_above:
            signal = "偏空观望"
        elif ma20_above:
            signal = "持有/观望"
        else:
            signal = "观望"

        extra = ""
        if golden:
            extra = " 金叉"
        elif death:
            extra = " 死叉"

        suffix = f" ({bar_date[5:]})" if bar_date else ""
        return f"• {symbol}  {signal}  RSI:{rsi}{extra}{suffix}", bar_date[5:]
    except Exception as e:
        print(f"[警告] {symbol} 分析失败: {e}")
        return f"• {symbol}  分析失败", ""


# ==================== 消息拼装与推送 ====================
def build_message(now, slot):
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%m-%d")

    lines = [f"【ETF信号助手】{now_str} {slot_label(slot)}", ""]
    stale = []

    # 自选ETF
    lines.append("⭐ 自选信号汇总：")
    for code in ETF_LIST:
        text, bar_date = analyze_one(code)
        lines.append(text)
        if bar_date and bar_date != today:
            stale.append(code)
        time.sleep(0.8)  # 避免请求过快

    lines.append("")

    # 债市晴雨表
    lines.append(f"📊 债市晴雨表（{BOND_CODE}）：")
    text, bar_date = analyze_one(BOND_CODE)
    lines.append(text)
    if bar_date and bar_date != today:
        stale.append(BOND_CODE)

    lines.append("")

    # 单独基金
    lines.append(f"📌 其他基金（{OTHER_FUND}）：")
    text, bar_date = analyze_one(OTHER_FUND)
    lines.append(text)
    if bar_date and bar_date != today:
        stale.append(OTHER_FUND)

    lines.append("")

    # 数据基准日不是今天的标的单独点出来（场外基金净值本身就滞后一天）
    if stale:
        lines.append(f"ℹ 数据未到今日：{'、'.join(stale)}（括号内为基准日）")
        lines.append("")

    lines.append("💡 有死叉或重大利空优先减仓，本信息仅供参考")

    return "\n".join(lines)


def send_serverchan(title, content, preview=False, max_retries=3):
    """发送 Server酱消息

    免费版每天 5 条、另有每分钟 50 条限制，所以这里分两类处理：
    - preview=True：只打印不发送，调试时不占额度
    - 网络/超时类失败在本函数内重试；额度用完或触发频控属于不可恢复，重试没意义，直接放弃
    """
    if preview:
        print("预览模式：不调用 Server酱，不消耗每日额度")
        print(f"标题：{title}")
        print("内容：")
        print(content)
        return True

    if not SENDKEY:
        print("未配置 SERVERCHAN_SENDKEY")
        return False

    import requests
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = {
        "title": title,
        "desp": content
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, data=data, timeout=15)
            result = resp.json()
        except Exception as e:
            print(f"[第{attempt}/{max_retries}次] 请求异常：{e}")
            if attempt < max_retries:
                time.sleep(10)
            continue

        print(f"[第{attempt}/{max_retries}次] 推送结果：", result)
        if result.get("code") == 0:
            return True

        # code 非 0 时 message 会说明原因
        message = str(result.get("message", ""))
        if any(k in message for k in ("LIMIT", "limit", "超出", "限制", "频繁", "额度", "频")):
            print("疑似当日 5 条额度已用完或触发频率限制，停止重试")
            return False

        if attempt < max_retries:
            time.sleep(10)

    return False


# ==================== 入口 ====================
def main():
    parser = argparse.ArgumentParser(description="ETF 信号分析并推送到微信（Server酱）")
    parser.add_argument("--slot", default=None, help="指定时段：pre / mid / close / manual，留空按当前北京时间判定")
    parser.add_argument("--detect-slot", action="store_true", help="只输出当前时段，供 GitHub Actions 使用")
    parser.add_argument("--preview", action="store_true", help="只生成并打印消息，不调用 Server酱（不占每天 5 条额度）")
    args = parser.parse_args()

    now = beijing_now()

    # Actions 用来决定"这一跳要不要真正干活"
    if args.detect_slot:
        print(f"date={now.strftime('%Y-%m-%d')}")
        print(f"slot={detect_slot(now)}")
        return 0

    slot = args.slot or detect_slot(now)
    if not slot:
        print(f"北京时间 {now.strftime('%H:%M')} 不在任何推送窗口内，跳过")
        return 0

    # 定时触发时校验交易日，节假日不打扰；手动触发不做限制
    if slot != "manual" and not is_trade_day(now):
        print(f"北京时间 {now.strftime('%Y-%m-%d')} 非交易日，跳过")
        return 0

    print(f"开始生成信号（北京时间 {now.strftime('%Y-%m-%d %H:%M')}，{slot_label(slot)}）...")
    content = build_message(now, slot)
    print(content)
    print("-" * 40)

    title = f"ETF信号助手 {slot_label(slot)}"
    if not send_serverchan(title, content, preview=args.preview):
        # 退出码非 0，让 Actions 变红，避免"静默失败"
        return 1

    print("预览完成（未发送）" if args.preview else "推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())

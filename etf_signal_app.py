#!/usr/bin/env python3
"""ETF 信号助手（Streamlit）

这里的每个数字都来自 daily_etf_push.py 的 evaluate()，本文件一行指标都不重算。

为什么改成这样：上一版 app 自带走一套算法，用简单移动平均算 RSI，
同一只 159941 在 2026-10-29 给出 34.3，而微信推送给出 48.6，差 14 个点，
还拿这个数去过滤金叉（RSI<65 才算买入信号），于是两边可以给出相反的结论。
双均线看着简单，各写一遍就必然分叉——现在只有一份实现，改推送就等于改这里。
"""

import os
import sys
import time

import pandas as pd
import streamlit as st

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import daily_etf_push as dp   # noqa: E402

# 推送端在仓库根运行，台账就落在仓库根；app 可能从别的目录启动，
# 把相对路径锚回脚本目录，否则永远读不到台账、溢价基准一直用绝对阈值
if dp.LEDGER_PATH == "signals.json":
    dp.LEDGER_PATH = os.path.join(_HERE, "signals.json")

st.set_page_config(
    page_title="ETF信号助手",
    page_icon="📈",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# 高对比度深色主题
st.markdown("""
<style>
    .stApp { background-color: #0b0f19; color: #f1f5f9; }
    .main-header { font-size: 1.7rem; font-weight: 800; color: #f8fafc !important; text-align: center; margin-bottom: 0.3rem; }
    .sub-header { font-size: 0.95rem; color: #94a3b8 !important; text-align: center; margin-bottom: 1.2rem; }
    .decision-up { background: linear-gradient(135deg, #065f46, #047857); color: #ecfdf5 !important; padding: 1.3rem; border-radius: 14px; text-align: center; font-size: 1.5rem; font-weight: 800; border: 1px solid #34d399; }
    .decision-down { background: linear-gradient(135deg, #7f1d1d, #b91c1c); color: #fef2f2 !important; padding: 1.3rem; border-radius: 14px; text-align: center; font-size: 1.5rem; font-weight: 800; border: 1px solid #f87171; }
    .decision-neutral { background: linear-gradient(135deg, #1e3a8a, #1d4ed8); color: #eff6ff !important; padding: 1.3rem; border-radius: 14px; text-align: center; font-size: 1.5rem; font-weight: 800; border: 1px solid #60a5fa; }
    .reason-text { color: #e2e8f0 !important; text-align: center; margin: 1rem 0 1.2rem; font-size: 1rem; }
    .stMarkdown, .stMarkdown p, .stMarkdown span, label, .stRadio label { color: #f1f5f9 !important; }
    .stTextInput input { background-color: #1e293b !important; color: #f8fafc !important; border: 1px solid #475569 !important; border-radius: 10px !important; }
    [data-testid="stMetricValue"] { color: #f8fafc !important; font-weight: 700 !important; }
    [data-testid="stMetricLabel"] { color: #94a3b8 !important; }
    .stAlert { background-color: #1e293b !important; color: #f1f5f9 !important; border: 1px solid #334155 !important; }
    .stButton > button { background: linear-gradient(135deg, #4f46e5, #7c3aed) !important; color: white !important; font-weight: 700 !important; border: none !important; border-radius: 10px !important; }
    hr { border-color: #334155 !important; }
</style>
""", unsafe_allow_html=True)


# ==================== 取数：只有这一条路径 ====================
@st.cache_data(ttl=180, show_spinner=False)
def load_signals(codes):
    """按推送的口径取一遍，返回 ({代码: evaluate 结果}, 溢价基准, 取数时间)

    只取两份原始数据：实时价（腾讯→东财）和净值快照（东财），然后逐只交给
    dp.evaluate。已经在这个函数外面看到"两边各算一遍"的下场了，所以这里
    不做任何二次加工——包括溢价：prem 原样带出，要不要加⚠由 dp.prem_flag 判。

    溢价基准从台账里取（最近 60 天中位数），台账不存在就给空 dict，
    evaluate 之外的展示层会自动退回绝对阈值。
    """
    codes = list(codes)
    now = dp.beijing_now()
    on_exchange = [c for c in codes if dp.is_onexchange(c)]
    spot = dp.get_spot_prices(on_exchange) if on_exchange else {}
    snap = dp.fetch_fund_snapshot(on_exchange) if on_exchange else {}
    base = dp.calc_prem_baseline(dp.load_ledger())

    items = {}
    for i, code in enumerate(codes):
        on_ex = dp.is_onexchange(code)
        items[code] = dp.evaluate(
            code, now, spot, use_spot=on_ex, fund_snap=snap, show_long=on_ex,
        )
        if i + 1 < len(codes):
            time.sleep(0.3)   # 连发会被东财限流，14 只速览时尤其明显
    return items, base, now


# ==================== 展示：文案规则也全部来自推送脚本 ====================
def card_class(signal):
    """颜色只表达方向，不表达建议

    类名从 decision-buy / decision-sell 改成 decision-up / decision-down。
    文案层花了大量篇幅说「金叉不是买点」，操作提醒第一条就是「这里没有买卖指令」，
    而视觉层给绿色大卡片挂一个叫 decision-buy 的类名——下次谁改样式，
    一定会按「买入」去理解，文案层的工作就被抵消了。
    按前缀匹配是因为 signal 可能是「金叉（3日前）」这种带后缀的形态。
    """
    if signal.startswith("金叉"):
        return "decision-up"
    if signal.startswith("死叉"):
        return "decision-down"
    return "decision-neutral"


def plain_reason(ev):
    """一句话解释这个状态是什么意思——以及它不是什么"""
    ago = ev.get("event_days_ago")
    when = f"这是 {ago} 个交易日前的交叉，不是今天。" if ago else ""
    if ev["event"] == "golden":
        return (f"MA20 上穿 MA60。{when}它在 30 只 ETF、22260 个观测的回测里对后续涨跌"
                "没有稳定预测力——这是一个标记，不是买点。")
    if ev["event"] == "death":
        return f"MA20 下穿 MA60。{when}同样只是标记，不是卖出指令。"
    if ev["ma20_above"]:
        return "MA20 仍在 MA60 上方，双均线排列没有被破坏。"
    return "MA20 已落到 MA60 下方。等它重新站上，再谈趋势是否修复。"


def long_text(long):
    if long == 1:
        return "长期↑"
    if long == 0:
        return "长期↓"
    return "—"


def long_hint(ev):
    if ev["long"] == 1:
        return "站上 MA200 缓冲带：长期趋势还没被破坏。"
    if ev["long"] == 0:
        return "跌破 MA200 缓冲带：长期趋势转弱。这是排除器，不是做空信号。"
    return "K 线不足 200 根，MA200 还没暖机，长期状态这一项留空。"


def premium_text(code, ev, base=None):
    """溢价片段：与推送同一条阈值线，只有 heavy 才加 ⚠

    只要取到就印，不再「不严重就不印」：不印的话读者无法区分
    「没有溢价数据」和「溢价正常」，而溢价是这个模型唯一还有证据支持的维度。
    """
    prem = ev.get("prem")
    if prem is None:
        return ""
    text = f"溢价 {prem:.2f}%"
    label = ev.get("prem_label") or ""
    if label:
        text += f"({label})"
    if dp.prem_flag(prem, (base or {}).get(code)) == "heavy":
        text += "⚠"
    return text


def summary_line(code, ev, base=None):
    """一行摘要，字段顺序与微信推送完全一致（对照着看不会错位）"""
    if not ev or not ev.get("ok"):
        return f"- ⚪ **{code}** {ev.get('reason', '分析失败') if ev else '分析失败'}"
    icon = dp.signal_icon(ev["signal"])
    name = dp.FUND_NAMES.get(code, "")
    parts = [f"- {icon} **{code}**" + (f" {name}" if name else "")]
    parts.append(f"**{ev['signal']}**" if ev["event"] else ev["signal"])
    parts.append(f"RSI {ev['rsi']}" if ev["rsi"] is not None else "RSI --")
    if ev["long"] == 1:
        parts.append("长期↑")
    elif ev["long"] == 0:
        parts.append("**长期↓**")
    prem = premium_text(code, ev, base)
    if prem:
        parts.append(prem)
    suffix = dp.bar_suffix(ev["bar_date"], dp.beijing_now())
    if suffix:
        parts.append(suffix)
    return " · ".join(parts)


def prem_note(code, ev, base):
    """把溢价的口径和阈值来源写清楚，省得读到 14.5% 却以为是今天盘中价"""
    prem = ev.get("prem")
    if prem is None:
        st.caption("溢价：东财这次没返回净值数据，跳过。")
        return
    label = ev.get("prem_label") or "净值日"
    line = (f"溢价 = {label} 当日收盘 / 单位净值 − 1，价格用不复权口径。"
            "净值本身滞后 1–2 天（跨境 T+2），所以它不随盘中价变动——"
            "盘中看到的溢价属于那个净值日，不是此刻。")
    bench = (base or {}).get(code)
    if bench is None:
        line += (f" 台账样本不足 {dp.LEDGER_PREM_MIN} 天，暂按绝对阈值 "
                 f"{dp.PREMIUM_WARN:g}% / {dp.PREMIUM_HEAVY:g}% 提示。")
    else:
        line += (f" 已用本品种最近 {dp.LEDGER_PREM_WINDOW} 天中位数 {bench:.2f}% 作基准，"
                 f"偏离 +{dp.PREMIUM_DEV_WARN:g}% 起提示、"
                 f"+{dp.PREMIUM_DEV_HEAVY:g}% 加 ⚠。")
    st.caption(line)


def render_detail(code, ev, base):
    signal = ev["signal"]
    st.markdown(
        f'<div class="{card_class(signal)}">{dp.signal_icon(signal)} {signal}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="reason-text">{plain_reason(ev)}</div>', unsafe_allow_html=True)

    st.markdown("#### 📊 技术指标")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("收盘价", f"{ev['close']:g}")
    m2.metric("RSI(14)", "—" if ev["rsi"] is None else f"{ev['rsi']}")
    m3.metric("MA20", f"{ev['ma20']:g}")
    m4.metric("MA60", f"{ev['ma60']:g}")

    c1, c2, c3 = st.columns(3)
    c1.metric("数据基准", dp.bar_suffix(ev["bar_date"], dp.beijing_now()) or ev["bar_date"])
    c2.metric("长期趋势", long_text(ev["long"]))
    c3.metric("溢价", "—" if ev["prem"] is None else f"{ev['prem']:.2f}%")

    st.caption(long_hint(ev))
    if ev.get("patched"):
        st.caption("今日这根 K 线是用实时价补的：历史日线接口还没收录当日，补价前已做跳变保护。")
    prem_note(code, ev, base)

    st.markdown("#### 📈 近 120 个交易日")
    df = ev["df"].copy()
    df["日期"] = pd.to_datetime(df["日期"])
    chart = df.tail(120)[["日期", "收盘", "MA20", "MA60"]].set_index("日期")
    st.line_chart(chart)


# ==================== 自选列表 ====================
if "watchlist" not in st.session_state:
    st.session_state.watchlist = ["513120"]


def add_to_watchlist(code):
    code = code.strip()
    if code and code not in st.session_state.watchlist:
        st.session_state.watchlist.append(code)
        st.success(f"已将 {code} 加入自选")
        st.rerun()


def remove_from_watchlist(code):
    if code in st.session_state.watchlist:
        st.session_state.watchlist.remove(code)
        st.rerun()


# ==================== 主界面 ====================
st.markdown('<div class="main-header">📈 ETF 信号助手</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">双均线 + Wilder RSI + MA200 缓冲带 · '
            '与每日微信推送同一套计算</div>', unsafe_allow_html=True)

st.markdown("#### ⭐ 我的自选")
if st.session_state.watchlist:
    cols = st.columns(min(len(st.session_state.watchlist), 4))
    for i, code in enumerate(st.session_state.watchlist):
        with cols[i % 4]:
            if st.button(f"{code}", key=f"wl_{code}", use_container_width=True):
                st.session_state["selected_code"] = code
                st.rerun()
else:
    st.caption("暂无自选，分析后可添加")

with st.expander("管理自选（删除）"):
    if st.session_state.watchlist:
        del_code = st.selectbox("选择要删除的代码", st.session_state.watchlist)
        if st.button("删除选中代码"):
            remove_from_watchlist(del_code)
    else:
        st.write("当前没有自选")

st.markdown("---")

default_code = st.session_state.get("selected_code", "513120")
col1, col2, col3 = st.columns([3, 1, 1])
with col1:
    symbol = st.text_input("输入ETF/基金代码", value=default_code,
                           placeholder="例如 513120、510300、159915").strip()
with col2:
    st.write("")
    st.write("")
    run_btn = st.button("开始分析", type="primary", use_container_width=True)
with col3:
    st.write("")
    st.write("")
    add_btn = st.button("加入自选", use_container_width=True)

if add_btn and symbol:
    add_to_watchlist(symbol)

st.markdown("---")

if run_btn:
    if not symbol:
        st.error("请输入代码")
    else:
        with st.spinner(f"正在取 {symbol} 的行情…"):
            items, base, _now = load_signals((symbol,))
        ev = items.get(symbol)
        if not ev or not ev.get("ok"):
            st.error(f"{symbol}：{(ev or {}).get('reason', '分析失败')}")
            st.warning("行情接口可能暂时受限，请过 1-2 分钟重试，"
                       "或换个代码试试（如 510300）。")
        else:
            render_detail(symbol, ev, base)
            if symbol not in st.session_state.watchlist:
                if st.button(f"⭐ 将 {symbol} 加入自选", key="add_after_analysis"):
                    add_to_watchlist(symbol)

st.markdown("---")

# ==================== 全池速览 ====================
with st.expander("📋 全部标的速览（与推送同顺序）"):
    st.caption("和微信推送一样按分组列一遍，第一次拉取约需 30–60 秒。")
    if st.button("拉取全部", key="batch_btn"):
        st.session_state["batch"] = True

    if st.session_state.get("batch"):
        codes = list(dp.ETF_LIST) + [dp.BOND_CODE, dp.OTHER_FUND]
        with st.spinner("正在取数…"):
            items, base, now = load_signals(tuple(codes))
        st.caption(f"取数时间 {now.strftime('%Y-%m-%d %H:%M')}")

        for group, group_codes in dp.ETF_GROUPS.items():
            lines = [f"**{group}**", ""]
            ups = downs = 0
            for c in group_codes:
                ev = items.get(c)
                if not ev:
                    continue
                lines.append(summary_line(c, ev, base))
                if ev.get("long") == 1:
                    ups += 1
                elif ev.get("long") == 0:
                    downs += 1
            # 组内只有一只时不写尾行：那只是把上面那行再念一遍，是冗余不是信息
            if ups + downs and len(group_codes) > 1:
                tail = f"- ▲ 本节 **{ups}/{ups + downs} 只↑**"
                if downs:
                    tail += f" · {downs} 只↓"
                lines += ["", tail]
            st.markdown("\n".join(lines))

        ups_all = sum(1 for ev in items.values() if ev.get("long") == 1)
        downs_all = sum(1 for ev in items.values() if ev.get("long") == 0)
        if ups_all + downs_all:
            width = f"- 📐 长期趋势宽度：**{ups_all}/{ups_all + downs_all} 只↑**"
            if downs_all:
                width += f" · {downs_all} 只↓"
            st.markdown(width)

        st.markdown("**📊 债市晴雨表**\n\n"
                    + summary_line(dp.BOND_CODE, items.get(dp.BOND_CODE), base))
        st.markdown("**📌 其他基金**\n\n"
                    + summary_line(dp.OTHER_FUND, items.get(dp.OTHER_FUND), base))

st.markdown("---")

with st.expander("操作提醒"):
    st.markdown(f"""
- **这里没有买卖指令。** 金叉 / 死叉 / 持有观望 / 偏空观望都是对行情的描述，不是让你买或卖的结论。
- **金叉死叉只是标记。** 30 只 ETF、22260 个观测的回测里，金叉和死叉之后 5/10/20 个交易日的超额收益都接近 0；照它交易等于付手续费做随机。
- **长期↓ 是排除器，不是做空信号。** 加入 MA200 缓冲带的回测把最大回撤从 19.1% 压到 18.4%，但上涨捕获率恒等于持仓率——它只帮你别在长期下跌里硬扛，不负责赚钱。
- **溢价是你为「买得到」多付的钱。** 跨境 ETF 常年溢价，溢价 12% 意味着净值要先涨 12% 你才回本。这里的溢价 =「净值日当天收盘 / 单位净值 − 1」，而净值本身滞后 1–2 天（跨境 T+2），价格却一直在动——所以括号里那个日期是它的口径，它不是你此刻看到的溢价。
- **溢价阈值随品种浮动。** 同一条 12% 对 159941 可能天天触发（它常年就是 10% 上下），对 510300 却几乎不出现。台账里该品种攒够 {dp.LEDGER_PREM_MIN} 个观测后，改用「本品种最近 {dp.LEDGER_PREM_WINDOW} 天中位数 + 偏离量」判轻重；不够才退回绝对阈值（{dp.PREMIUM_WARN:g}% / {dp.PREMIUM_HEAVY:g}%）。
- **「（N 日前）」是回看窗口。** 交叉当天没看到、隔一天再看，信号仍然显示，同时标明它距今天几个交易日；超过 5 个交易日的旧交叉不再提示——那已经是历史，不是状态变化。
- **仓位和止损由你自己定。** 这里不给任何固定比例——没有证据支持某条比例更好，而单只 ETF 能承受多大回撤，只有你自己知道。
- 自选列表保存在当前浏览器会话里，关掉就没了。
""")

st.caption("数据来源：东方财富 / 腾讯（akshare） · "
           "指标口径与 daily_etf_push.py 完全一致 · 仅供学习参考，不构成投资建议")

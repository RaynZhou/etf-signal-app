#!/usr/bin/env python3
"""ETF 信号助手（Streamlit）

这里的每个数字都来自 daily_etf_push.py 的 evaluate()，本文件一行指标都不重算。

为什么改成这样：上一版 app 自带走一套算法，用简单移动平均算 RSI，
同一只 159941 在 2026-10-29 给出 34.3，而微信推送给出 48.6，差 14 个点，
还拿这个数去过滤金叉（RSI<65 才算买入信号），于是两边可以给出相反的结论。
双均线看着简单，各写一遍就必然分叉——现在只有一份实现，改推送就等于改这里。
"""

import html
import os
import sys
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

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
    layout="wide",
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
    .state-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 0.2rem 0 0.4rem; }
    .state-cell { border: 1px solid #334155; border-radius: 10px; padding: 0.6rem 0.8rem; background: #111827; color: #94a3b8 !important; }
    .state-cell .state-head { display: block; font-size: 0.76rem; color: #94a3b8 !important; margin-bottom: 0.1rem; }
    .state-cell b { font-size: 0.95rem; color: #cbd5e1 !important; }
    .state-cell.on-up { border-color: #34d399; background: #052e26; }
    .state-cell.on-up .state-head, .state-cell.on-up b { color: #ecfdf5 !important; }
    .state-cell.on-warn { border-color: #f59e0b; background: #3a2a05; }
    .state-cell.on-warn .state-head, .state-cell.on-warn b { color: #fffbeb !important; }
    .state-cell.on-down { border-color: #f87171; background: #3f1212; }
    .state-cell.on-down .state-head, .state-cell.on-down b { color: #fef2f2 !important; }
    .state-note { color: #94a3b8 !important; font-size: 0.8rem; margin: 0 0 1rem; }
    .block-container { max-width: 1280px; padding-top: 2rem; }
    .wl-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0.3rem 0 0.9rem; }
    .wl-chip { background: #111827; border: 1px solid #334155; border-radius: 999px; padding: 0.28rem 0.72rem; font-size: 0.85rem; color: #e2e8f0 !important; }
    .wl-chip em { font-style: normal; color: #94a3b8 !important; font-size: 0.78rem; margin-left: 0.35rem; }
    .ov-wrap { overflow-x: auto; margin: 0.4rem 0 0.6rem; }
    .ov-table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
    .ov-table th { text-align: left; color: #94a3b8 !important; font-weight: 600; padding: 0.45rem 0.6rem; border-bottom: 1px solid #334155; white-space: nowrap; }
    .ov-table td { padding: 0.5rem 0.6rem; border-bottom: 1px solid #1e293b; color: #e2e8f0 !important; white-space: nowrap; }
    .ov-table tr.on td { background: #172033; }
    .ov-table .ov-name { color: #94a3b8 !important; margin-left: 0.4rem; font-size: 0.8rem; }
    .sig-up { color: #34d399 !important; }
    .sig-down { color: #f87171 !important; }
    .sig-flat { color: #93c5fd !important; }
    .sig-fail { color: #f87171 !important; }
    .long-up { color: #34d399 !important; }
    .long-down { color: #f87171 !important; }
    .prem-heavy { color: #fbbf24 !important; }
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
    # app 是长驻进程，熔断状态不能跟上一轮复用：早上一次误熔断就会永远只走腾讯，
    # 而腾讯价格只有 3 位小数。每轮取数开头归零。
    dp.reset_em_breaker()
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
    # 两种留空要分开说：场外基金是「不参与判定」，不是「K 线不够」。
    # 270044 有 3000 多根净值却一直显示「K 线不足 200 根」，读到的人会以为数据坏了。
    hist = ev.get("df")
    if hist is None or len(hist) < dp.MA_LONG:
        return "K 线不足 200 根，MA200 还没暖机，长期状态这一项留空。"
    return "该标的未参与长期趋势判定，这一项留空。"


def premium_text(code, ev, base=None):
    """溢价片段：直接调推送的同一条展示规则

    1 位小数是为了和微信推送逐位对齐：推送 14.9%、这里 14.93%，
    对照着看会以为两边算的是两个东西（溢价本身是净值日结算的，第 2 位没有信息量）。
    绝对值（成本）和 `偏±Xpp`（相对本品种历史的异常）分开给，理由见 dp.premium_display。
    """
    return dp.premium_display(
        ev.get("prem"), (base or {}).get(code), ev.get("prem_label") or ""
    )


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
    if not dp.is_onexchange(code):
        st.caption("场外基金不算溢价：溢价需要「同一净值日的收盘价」，"
                   "场外基金没有盘中价，用别的日期对齐只会算出一个假数。")
        return
    prem = ev.get("prem")
    if prem is None:
        st.caption("溢价：东财这次没返回净值数据，跳过。")
        return
    label = ev.get("prem_label") or "净值日"
    line = (f"溢价 = {label} 当日收盘 / 单位净值 − 1，价格用不复权口径。"
            "净值本身滞后 1–2 天（跨境 T+2），所以它不随盘中价变动——"
            "盘中看到的溢价属于那个净值日，不是此刻。")
    line += (f" 左边那个数是绝对成本：每份多付几个点，{dp.PREMIUM_WARN:g}% 标「高」、"
             f"{dp.PREMIUM_HEAVY:g}% 标「重」，它不需要跟谁比。")
    bench = (base or {}).get(code)
    if bench is None:
        line += f" 台账样本不足 {dp.LEDGER_PREM_MIN} 天，暂不出相对偏离。"
    else:
        line += (f" `偏±Xpp` 才是相对本品种最近 {dp.LEDGER_PREM_WINDOW} 天中位数"
                 f"（{bench:.2f}%）的偏离，+{dp.PREMIUM_DEV_WARN:g}pp 显示、"
                 f"+{dp.PREMIUM_DEV_HEAVY:g}pp 加 ⚠——它只回答「今天反不反常」。")
    st.caption(line)


def state_matrix(ev):
    """把「长期方向 × 绝对溢价」并排放成四格，只高亮当下所处的那一格

    这不是评分：四格是两件已发生事实的组合，没有权重，也没有「多少分以上买」。
    不做加权总分的理由——金叉和趋势方向在回测里都不预测涨跌（30 只、22260 个观测），
    有依据的只有买入成本，以及把长期↓ 当排除器；把三项压成一个数字，
    等于用没被验证过的权重替读者下结论。
    """
    long_s, prem = ev.get("long"), ev.get("prem")
    if long_s is None or prem is None:
        return
    heavy = prem >= dp.PREMIUM_HEAVY
    # 文案取推送那份 MATRIX_CELLS，这里只决定配色；两边各写一遍四格措辞早晚分叉
    cells = [
        (1, False, "on-up"),
        (1, True, "on-warn"),
        (0, False, "on-warn"),
        (0, True, "on-down"),
    ]
    html = ['<div class="state-grid">']
    for ls, hi, tone in cells:
        text = dp.MATRIX_CELLS[(ls, hi)]
        active = f" {tone}" if (ls == long_s and hi == heavy) else ""
        html.append(
            f'<div class="state-cell{active}">'
            f'<span class="state-head">长期{"↑" if ls == 1 else "↓"} · '
            f'{"溢价重" if hi else "溢价正常"}</span><b>{text}</b></div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)
    st.markdown(
        '<div class="state-note">四格是当下事实的组合，不加权打分：'
        '长期↓ 只当排除器（不负责赚钱），溢价重是每份多付的成本；'
        '两者都不预测明天涨跌。</div>',
        unsafe_allow_html=True,
    )


def build_figure(code, ev, span, ledger):
    """价格/长期状态 + RSI + 溢价三栏图，序列全部来自 daily_etf_push"""
    df = ev["df"]
    win = df.tail(span).copy()
    win["x"] = pd.to_datetime(win["日期"].astype(str).str[:10])
    price_label = "收盘" if dp.is_onexchange(code) else "累计净值"

    hist_items = sorted(dp.prem_history(ledger, code).items())
    rows = 3 if (hist_items or ev.get("prem") is not None) else 2
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        row_heights=[0.60, 0.16, 0.24] if rows == 3 else [0.74, 0.26],
        vertical_spacing=0.06,
    )

    # 长期状态必须在全序列上递推再切窗口：在窗口内重新递推会让 holding 从 0 起步，
    # 图上凭空多出一段「长期↓」。这是带缓冲带的趋势状态最容易被画错的地方。
    # ev["long"] 为 None 时（场外基金不参与长期判定）整块不着色：模型没给的状态，
    # 图上不能自己算一个出来，否则看图的人会以为它和推送用的是同一个结论。
    runs = []
    if ev.get("long") is not None:
        state = dp.long_state_series(df).loc[win.index]
        run_start, prev = None, "init"
        for x, s in zip(win["x"], state):
            k = None if pd.isna(s) else int(s)
            if k != prev:
                if prev in (0, 1) and run_start is not None:
                    runs.append((run_start, x, prev))
                run_start = x if k in (0, 1) else None
                prev = k
        if prev in (0, 1) and run_start is not None:
            runs.append((run_start, win["x"].iloc[-1], prev))
    for x0, x1, s in runs:
        # exclude_empty_subplots 必须关掉：默认值会跳过「还没画任何 trace 的子图」，
        # 而这里着色先于曲线添加，保持默认的话一个矩形都不会出现（plotly 7.x 实测）。
        fig.add_vrect(
            x0=x0, x1=x1, row=1, col=1, line_width=0, layer="below",
            exclude_empty_subplots=False,
            fillcolor="rgba(16,185,129,0.13)" if s == 1 else "rgba(239,68,68,0.13)",
        )

    fig.add_trace(go.Scatter(x=win["x"], y=win["收盘"], name=price_label,
                             line=dict(color="#e2e8f0", width=1.6)), row=1, col=1)
    for col, color, dash in (("MA20", "#38bdf8", None),
                             ("MA60", "#a78bfa", None),
                             ("MA200", "#f59e0b", "dot")):
        if col in win.columns:
            fig.add_trace(go.Scatter(x=win["x"], y=win[col], name=col,
                                     line=dict(color=color, width=1.1, dash=dash)),
                          row=1, col=1)

    price_by_date = {d: float(c) for d, c in
                     zip(win["日期"].astype(str).str[:10], win["收盘"])}
    for event, name, symbol, color in (("golden", "金叉", "triangle-up", "#34d399"),
                                       ("death", "死叉", "triangle-down", "#f87171")):
        pts = [(d, price_by_date[d]) for d, e in dp.cross_points(df)
               if e == event and d in price_by_date]
        if pts:
            fig.add_trace(go.Scatter(
                x=[pd.Timestamp(d) for d, _ in pts], y=[p for _, p in pts],
                mode="markers", name=name,
                marker=dict(symbol=symbol, size=9, color=color,
                            line=dict(color="#0b0f19", width=1)),
                hovertemplate="%{x|%Y-%m-%d}<br>" + name + "<extra></extra>",
            ), row=1, col=1)

    if "盘中" in (dp.bar_suffix(ev["bar_date"], dp.beijing_now()) or ""):
        fig.add_annotation(x=win["x"].iloc[-1], y=win["收盘"].iloc[-1], text="盘中",
                           showarrow=True, arrowhead=0, ax=0, ay=-24,
                           font=dict(color="#fbbf24", size=11), row=1, col=1)

    if "RSI" in win.columns:
        fig.add_trace(go.Scatter(x=win["x"], y=win["RSI"], name="RSI(14)",
                                 line=dict(color="#94a3b8", width=1),
                                 showlegend=False), row=2, col=1)
        for y in (30, 70):
            fig.add_hline(y=y, line=dict(color="#475569", width=1, dash="dot"),
                          row=2, col=1)
        fig.update_yaxes(range=[0, 100], row=2, col=1)

    if rows == 3:
        r = 3
        if hist_items:
            hist = pd.Series([v for _, v in hist_items],
                             index=pd.to_datetime([d for d, _ in hist_items]))
            med = hist.rolling(dp.LEDGER_PREM_WINDOW,
                               min_periods=dp.LEDGER_PREM_MIN).median()
            fig.add_trace(go.Scatter(x=hist.index, y=hist.values, name="溢价",
                                     line=dict(color="#cbd5e1", width=1.2)), row=r, col=1)
            fig.add_trace(go.Scatter(x=med.index, y=med.values,
                                     name=f"{dp.LEDGER_PREM_WINDOW} 日中位",
                                     line=dict(color="#38bdf8", width=1.2, dash="dash")),
                          row=r, col=1)
            for k, color in ((dp.PREMIUM_DEV_WARN, "#f59e0b"),
                             (dp.PREMIUM_DEV_HEAVY, "#f87171")):
                fig.add_trace(go.Scatter(x=med.index, y=med.values + k,
                                         name=f"中位+{k:g}pp", showlegend=False,
                                         line=dict(color=color, width=1, dash="dot")),
                              row=r, col=1)
        if ev.get("prem") is not None:
            nav_d = ev.get("prem_nav_date") or ev["bar_date"]
            fig.add_trace(go.Scatter(
                x=[pd.Timestamp(nav_d)], y=[ev["prem"]], mode="markers", name="当前",
                marker=dict(color="#fbbf24", size=9,
                            line=dict(color="#0b0f19", width=1)),
                hovertemplate=f"{nav_d}<br>溢价 %{{y:.1f}}%<extra></extra>",
                showlegend=False,
            ), row=r, col=1)
        fig.add_hline(y=0, line=dict(color="#475569", width=1), row=r, col=1)

    fig.update_layout(
        height=700 if rows == 3 else 540,
        margin=dict(l=8, r=8, t=54, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cbd5e1", size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        hovermode="closest",
    )
    fig.update_xaxes(gridcolor="#1e293b", zerolinecolor="#334155", showspikes=False)
    # automargin：窄屏下不设它，y 轴刻度（1.24）和轴标题会被裁成最后一个字符
    fig.update_yaxes(gridcolor="#1e293b", zerolinecolor="#334155", automargin=True)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    if "RSI" in win.columns:
        fig.update_yaxes(title_text="RSI 仅参考", row=2, col=1)
    if rows == 3:
        fig.update_yaxes(title_text="溢价 %", row=3, col=1)
    return fig


def render_chart(code, ev, span, ledger, key_prefix="detail"):
    st.plotly_chart(build_figure(code, ev, span, ledger), theme=None,
                    config={"displayModeBar": False},
                    key=f"chart_{key_prefix}_{code}_{span}")
    note = (f"最后一根：{dp.bar_suffix(ev['bar_date'], dp.beijing_now()) or ev['bar_date']}。"
            "▲/▼ 是已经发生的 MA20/MA60 交叉，是标记，不是买卖点。")
    if ev.get("long") is None:
        note += "该标的的长期状态不参与判定，主图不做红绿着色。"
    else:
        note += "绿/红底色是长期↑/长期↓（MA200 缓冲带逐日递推），无底色段是 MA200 未暖机。"
    if ev.get("prem") is not None:
        note += ("溢价虚线是最近 60 个观测的中位数；点线为 +4pp / +7pp 偏离线，"
                 "只回答「今天反不反常」，不代表贵贱。")
    st.caption(note)


def render_detail(code, ev, base, key_prefix="detail"):
    signal = ev["signal"]
    st.markdown(
        f'<div class="{card_class(signal)}">{dp.signal_icon(signal)} {signal}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="reason-text">{plain_reason(ev)}</div>', unsafe_allow_html=True)
    state_matrix(ev)

    st.markdown("#### 📊 技术指标")
    m1, m2, m3, m4 = st.columns(4)
    # 场外基金现在用累计净值（原因见 daily_etf_push.fetch_data 里那段分红台阶的说明）。
    # 标签必须跟着口径变：写「收盘价」会让人拿它去对天天基金上的单位净值，
    # 270044 一个是 1.7158、一个是 1.2140，一对不上就以为算错了。
    m1.metric("收盘价" if dp.is_onexchange(code) else "累计净值", f"{ev['close']:g}")
    m2.metric("RSI(14)", "—" if ev["rsi"] is None else f"{ev['rsi']}")
    m3.metric("MA20", f"{ev['ma20']:g}")
    m4.metric("MA60", f"{ev['ma60']:g}")

    c1, c2, c3 = st.columns(3)
    c1.metric("数据基准", dp.bar_suffix(ev["bar_date"], dp.beijing_now()) or ev["bar_date"])
    c2.metric("长期趋势", long_text(ev["long"]))
    c3.metric("溢价", "—" if ev["prem"] is None else f"{ev['prem']:.1f}%")

    # 「逆风 + 贵」的组合约束：措辞直接调推送那边同一个函数，两边不能各写一套
    # 文案只讲已发生的事实（既贵又逆风），不写「风险较高/建议观望」这类会被二次解读的词
    if dp.gate_note(ev.get("long"), ev["prem"]):
        st.warning(f"{dp.GATE_LABEL}：当期既贵又逆风——同时满足「长期↓」"
                   f"和「绝对溢价 ≥ {dp.PREMIUM_HEAVY:g}%」。这是成本约束，不是预测它会跌。")

    if not dp.is_onexchange(code):
        st.caption("场外基金只有净值、没有盘中价：净值 T+1 公布（QDII 常滞后 2 天）。"
                   "上面四个数字全部是**累计净值**口径：单位净值每遇分红会凭空掉一笔台阶"
                   "（270044 每季度一次，约占净值 0.85%），而它平时的日波动只有万分之几，"
                   "一根假阴线就能把 RSI 砸低几十个点。你账户里每份值多少钱，请查每日"
                   "公布的单位净值。")

    st.caption(long_hint(ev))
    if ev.get("patched"):
        st.caption("今日这根 K 线是用实时价补的：历史日线接口还没收录当日，补价前已做跳变保护。")
    prem_note(code, ev, base)

    st.markdown("#### 📈 价格、长期状态与事件")
    span = st.radio("显示区间", (120, 250, 400), index=1, horizontal=True,
                    format_func=lambda n: f"{n} 个交易日",
                    key=f"span_{key_prefix}_{code}", label_visibility="collapsed")
    render_chart(code, ev, span, dp.load_ledger(), key_prefix)


# ==================== 自选列表 ====================
# 默认带出推送里的全部标的：14 只 ETF 池 + 债基 270044 + 其他基金 270023，
# 不是只看「⭐ 自选ETF」那 6 只。不想看某只再手动删；
# 增删仍只保存在当前浏览器会话里。
_DEFAULT_WATCHLIST = list(dp.ETF_LIST) + [dp.BOND_CODE, dp.OTHER_FUND]
# 旧版本默认只有 6 只 / 14 只；版本号变化时重新播种，免得已经开着的页面还停在旧清单。
_WATCHLIST_VERSION = 3
if ("watchlist" not in st.session_state
        or st.session_state.get("watchlist_version") != _WATCHLIST_VERSION):
    st.session_state.watchlist = list(_DEFAULT_WATCHLIST)
    st.session_state["watchlist_version"] = _WATCHLIST_VERSION


def _valid_code(code):
    return len(code) == 6 and code.isdigit()


def add_to_watchlist(code):
    code = code.strip()
    if not code:
        st.warning("请输入代码")
        return
    if not _valid_code(code):
        st.error("ETF / 基金代码应为 6 位数字")
        return
    if code in st.session_state.watchlist:
        st.info(f"{code} 已在自选中")
        return
    st.session_state.watchlist.append(code)
    st.session_state.pop("batch", None)
    st.session_state.pop("view_code", None)
    st.toast(f"已将 {code} 加入自选")
    st.rerun()


def remove_from_watchlist(code):
    if code in st.session_state.watchlist:
        st.session_state.watchlist.remove(code)
        st.session_state.pop("batch", None)
        st.session_state.pop("view_code", None)
        st.toast(f"已移除 {code}")
        st.rerun()


def render_watchlist_chips(codes):
    chips = []
    for c in codes:
        name = dp.FUND_NAMES.get(c, "")
        label = html.escape(c)
        if name:
            label += f"<em>{html.escape(name)}</em>"
        chips.append(f'<span class="wl-chip">{label}</span>')
    st.markdown(f'<div class="wl-chips">{"".join(chips)}</div>',
                unsafe_allow_html=True)


def render_batch(batch):
    """一次取完自选后的总览：指标条 + 对比表 + 单只详情"""
    codes = list(batch["codes"])
    items, base, now = batch["items"], batch["base"], batch["now"]
    ok_codes = [c for c in codes if (items.get(c) or {}).get("ok")]
    ups = sum(1 for c in codes if (items.get(c) or {}).get("long") == 1)
    downs = sum(1 for c in codes if (items.get(c) or {}).get("long") == 0)
    heavy = sum(1 for c in ok_codes
                if items[c].get("prem") is not None
                and items[c]["prem"] >= dp.PREMIUM_HEAVY)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("自选", f"{len(codes)} 只")
    k2.metric("长期↑", f"{ups} 只")
    k3.metric("长期↓", f"{downs} 只")
    k4.metric("绝对溢价重", f"{heavy} 只")
    k5.metric("分析失败", f"{len(codes) - len(ok_codes)} 只")

    if st.session_state.get("view_code") not in ok_codes:
        st.session_state["view_code"] = ok_codes[0] if ok_codes else None
    selected = st.session_state.get("view_code")

    rows = []
    for c in codes:
        ev = items.get(c) or {}
        name = html.escape(dp.FUND_NAMES.get(c, ""))
        name_html = f'<span class="ov-name">{name}</span>' if name else ""
        if not ev.get("ok"):
            reason = html.escape(str(ev.get("reason", "分析失败")))
            rows.append(
                f'<tr><td><b>{html.escape(c)}</b>{name_html}</td>'
                f'<td class="sig-fail">❌ {reason}</td>'
                f'<td>—</td><td>—</td><td>—</td><td>—</td></tr>'
            )
            continue
        sig = ev["signal"]
        if sig.startswith("金叉"):
            sig_cls = "sig-up"
        elif sig.startswith("死叉"):
            sig_cls = "sig-down"
        else:
            sig_cls = "sig-flat"
        long_cell = (f'<span class="long-up">长期↑</span>' if ev["long"] == 1 else
                     f'<span class="long-down">长期↓</span>' if ev["long"] == 0 else "—")
        prem = premium_text(c, ev, base) or "—"
        prem_cls = (' class="prem-heavy"' if ev.get("prem") is not None
                    and ev["prem"] >= dp.PREMIUM_HEAVY else "")
        when = dp.bar_suffix(ev["bar_date"], now) or ev["bar_date"]
        row_cls = ' class="on"' if c == selected else ""
        rows.append(
            f'<tr{row_cls}><td><b>{html.escape(c)}</b>{name_html}</td>'
            f'<td class="{sig_cls}">{dp.signal_icon(sig)} {html.escape(sig)}</td>'
            f'<td>{"—" if ev["rsi"] is None else ev["rsi"]}</td>'
            f'<td>{long_cell}</td><td{prem_cls}>{html.escape(prem)}</td>'
            f'<td>{html.escape(str(when))}</td></tr>'
        )

    st.markdown(
        '<div class="ov-wrap"><table class="ov-table">'
        '<thead><tr><th>代码 / 名称</th><th>信号</th><th>RSI</th>'
        '<th>长期</th><th>溢价（绝对 · 偏）</th><th>数据基准</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>',
        unsafe_allow_html=True,
    )
    st.caption(f"取数时间 {now.strftime('%Y-%m-%d %H:%M')} · "
               "溢价列左侧是绝对成本，`偏±Xpp` 是相对本品种台账中位的异常；"
               "场外基金没有盘中价，长期与溢价留空。")

    if not ok_codes:
        st.error("本轮没有成功分析的标的：接口可能暂时受限，请过 1–2 分钟重试。")
        return

    st.markdown("#### 🔎 单只详情")
    view = st.selectbox(
        "选择标的", ok_codes, key="view_code", label_visibility="collapsed",
        format_func=lambda c: (f"{dp.signal_icon(items[c]['signal'])} {c} "
                               f"{dp.FUND_NAMES.get(c, '')} · {items[c]['signal']}"),
    )
    render_detail(view, items[view], base, key_prefix="batch")


# ==================== 主界面 ====================
st.markdown('<div class="main-header">📈 ETF 信号助手</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">双均线 + Wilder RSI + MA200 缓冲带 · '
            '与每日微信推送同一套计算</div>', unsafe_allow_html=True)

st.markdown("#### ⭐ 我的自选")
render_watchlist_chips(st.session_state.watchlist)

ac1, ac2 = st.columns([2, 3])
with ac1:
    analyze_all = st.button(
        f"🔍 一键分析全部自选（{len(st.session_state.watchlist)} 只）",
        type="primary", use_container_width=True, key="analyze_all",
        disabled=not st.session_state.watchlist,
    )
with ac2:
    st.caption("默认是推送里的全部标的：14 只 ETF + 债基 + 其他基金；一次取完，"
               "结果按对比表列出，图表只画下拉选中的那一只，避免多张图同时渲染。"
               "自选保存在当前浏览器会话里。")

with st.expander("⚙️ 管理自选（添加 / 删除）"):
    mc1, mc2 = st.columns(2)
    with mc1:
        new_code = st.text_input("添加代码", key="new_watch_code",
                                 placeholder="6 位数字，例如 510300").strip()
        if st.button("加入自选", key="wl_add"):
            add_to_watchlist(new_code)
    with mc2:
        if st.session_state.watchlist:
            del_code = st.selectbox(
                "选择要删除的代码", st.session_state.watchlist, key="del_watch_code",
                format_func=lambda c: f"{c} {dp.FUND_NAMES.get(c, '')}".strip(),
            )
            if st.button("从自选移除", key="wl_del"):
                remove_from_watchlist(del_code)
        else:
            st.caption("自选为空")

st.markdown("---")

if analyze_all:
    codes = tuple(st.session_state.watchlist)
    if not codes:
        st.error("自选为空，请先添加代码")
    else:
        with st.spinner(f"正在取 {len(codes)} 只基金的行情…首次约 10–30 秒"):
            items, base, now = load_signals(codes)
        st.session_state["batch"] = {
            "codes": codes, "items": items, "base": base, "now": now,
        }
        ok_codes = [c for c in codes if (items.get(c) or {}).get("ok")]
        st.session_state["view_code"] = ok_codes[0] if ok_codes else None

batch = st.session_state.get("batch")
if batch:
    render_batch(batch)
elif st.session_state.watchlist:
    st.caption("还没有批量结果。点击上方的「一键分析全部自选」开始。")

st.markdown("---")

# ==================== 单个代码（临时分析，不影响自选） ====================
with st.expander("🔎 分析单个代码（临时，不加入自选）"):
    sc1, sc2 = st.columns([3, 1])
    with sc1:
        symbol = st.text_input("输入ETF/基金代码", key="single_code",
                               placeholder="例如 513120、510300、159915").strip()
    with sc2:
        st.write("")
        st.write("")
        run_btn = st.button("开始分析", use_container_width=True, key="single_run")
    if run_btn:
        if not symbol:
            st.error("请输入代码")
        elif not _valid_code(symbol):
            st.error("ETF / 基金代码应为 6 位数字")
        else:
            with st.spinner(f"正在取 {symbol} 的行情…"):
                items, base, _now = load_signals((symbol,))
            ev = items.get(symbol)
            if not ev or not ev.get("ok"):
                st.session_state.pop("manual", None)
                st.error(f"{symbol}：{(ev or {}).get('reason', '分析失败')}")
                st.warning("行情接口可能暂时受限，请过 1-2 分钟重试，"
                           "或换个代码试试（如 510300）。")
            else:
                st.session_state["manual"] = (symbol, ev, base)

manual = st.session_state.get("manual")
if manual:
    m_code, m_ev, m_base = manual
    st.markdown(f"#### 📄 临时分析 · {html.escape(m_code)}")
    render_detail(m_code, m_ev, m_base, key_prefix="manual")
    if m_code not in st.session_state.watchlist:
        if st.button(f"⭐ 将 {m_code} 加入自选", key="add_manual"):
            add_to_watchlist(m_code)

st.markdown("---")

# ==================== 全池速览 ====================
with st.expander("📋 全部标的速览（与推送同顺序）"):
    st.caption("和微信推送一样按分组列一遍，第一次拉取约需 30–60 秒。")
    if st.button("拉取全部", key="all_pool_btn"):
        st.session_state["all_pool"] = True

    # 不能复用 "batch"：那里存的是自选批量结果（dict），dict 为真会让
    # 每次一键分析都顺带拉一遍全池，页面白白多等 30–60 秒。
    if st.session_state.get("all_pool"):
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
- **图上的颜色、箭头和四格不是建议。** 绿/红底色是 MA200 缓冲带的逐日状态，▲/▼ 是已经发生的交叉，四格只是把「长期方向 + 绝对溢价」两个事实并排放——没有加权分，也没有「多少分以上买」。
- **长期↓ 是排除器，不是做空信号。** 加入 MA200 缓冲带的回测把最大回撤从 19.1% 压到 18.4%，但上涨捕获率恒等于持仓率——它只帮你别在长期下跌里硬扛，不负责赚钱。
- **溢价是你为「买得到」多付的钱。** 跨境 ETF 常年溢价，溢价 12% 意味着净值要先涨 12% 你才回本。这里的溢价 =「净值日当天收盘 / 单位净值 − 1」，而净值本身滞后 1–2 天（跨境 T+2），价格却一直在动——所以括号里那个日期是它的口径，它不是你此刻看到的溢价。
- **溢价有两个数，别混着读。** `溢价重 18.1%` 是绝对值：每份多付 18.1%，净值要先涨 18.1% 你才回本——这是成本，不预测方向（额度受限的跨境 ETF 可以长期维持高溢价，所以它几乎天天带「重」）。`偏+6.5pp` 才是相对本品种最近 {dp.LEDGER_PREM_WINDOW} 天中位数的偏离，只用来判断「今天是否反常」，+{dp.PREMIUM_DEV_WARN:g}pp 显示、+{dp.PREMIUM_DEV_HEAVY:g}pp 加 ⚠。
- **「（N 日前）」是回看窗口。** 交叉当天没看到、隔一天再看，信号仍然显示，同时标明它距今天几个交易日；超过 5 个交易日的旧交叉不再提示——那已经是历史，不是状态变化。
- **仓位和止损由你自己定。** 这里不给任何固定比例——没有证据支持某条比例更好，而单只 ETF 能承受多大回撤，只有你自己知道。
- 自选列表保存在当前浏览器会话里，关掉就没了。
""")

st.caption("数据来源：东方财富 / 腾讯（akshare） · "
           "指标口径与 daily_etf_push.py 完全一致 · 仅供学习参考，不构成投资建议")

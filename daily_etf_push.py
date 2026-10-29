#!/usr/bin/env python3
"""
每日ETF信号推送脚本（配合 GitHub Actions + Server酱）

时区说明：GitHub Actions 的 runner 是 UTC，本脚本所有与"现在几点"有关的计算统一换算成北京时间（UTC+8），
避免消息里显示 UTC 时间、时段标签判错。

用法：
    python daily_etf_push.py --detect-slot   只判定当前北京时间落在哪个推送窗口，输出给 Actions
    python daily_etf_push.py --slot close    按指定时段（pre / mid / close / manual）推送
    python daily_etf_push.py --slot mid --preview   只生成并打印消息，不发送（不占每日额度）
    python daily_etf_push.py --slot close --record  强制写入当日台账（本地补录用）
    python daily_etf_push.py                 按当前北京时间自动判定时段后推送

台账：signals.json 记每天每只的信号/长期趋势/溢价率，用来算「从第一天推送开始」的实盘胜率。
     Actions 每次是全新容器，所以这个文件必须提交回仓库才有意义（见 workflow）。

额度提醒：Server酱免费版每天只有 5 条，正常自动推送是一天 3 条，调试时优先用 --preview。
"""

import os
import sys
import time
import math
import json
import re
import statistics
import argparse
from datetime import datetime, timedelta, timezone

# 防御：部分环境 stdout 不是 UTF-8（比如 Windows 下是 GBK），消息里的 emoji 会让 print 直接抛异常。
# 在 Actions 里这个异常会让 job 失败，而 workflow 是先占坑再推送，失败后本时段不会重试，所以强制转 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ==================== 配置 ====================
# 从环境变量读取（GitHub Secrets）
SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")

# 推送池：按大类分组，正文也按这个顺序分小节排
# 2026-10-29 从 6 只扩到 14 只：原来 6 只全是港股/美股中概加一只沪深300，
# A 股行业与商品一个都没有，所以「只剩 1 只长期↑」其实是池子太窄造成的，
# 不是市场结论。扩池后的定位要写清楚：这是行情摘要（看哪行热），
# 不是买入信号——「全都↑可以进场」这件事已经用 25 只/十年样本外证伪了
# （39 个非重叠块均值 +0.31%，随机打乱反而有 +0.59%，p=0.617）。
# 新增的 8 只均上市于 2016 年前，HISTORY_DAYS=600 足够 MA200 暖机。
# ETF_LIST 由这里派生，不单独维护——两份清单迟早会改漏一份。
#
# 2026-10-29 把原来的「🪙 商品」拆成黄金与油气两节。拆的依据不是常识，是相关系数：
#   用 25 只跨十年的日收益算，518880 与 162411 的 r 只有 +0.04，
#   而同一池里 A 股行业+宽基两两中位相关是 +0.57——同类资产应该是那个样子。
#   并且它俩恰好是全池最孤立的两只（对池内平均相关 +0.07 / +0.17）。
#   合在一节里写「本节 1/2 只↑」，读出来的是一个不存在的「商品板块」。
#   驱动也确实不同：518880 是人民币计价的 Au99.99（金价×汇率两层），
#   162411 是一篮子能源企业的股权。
ETF_GROUPS = {
    "⭐ 自选ETF": ["159545", "159941", "513120", "159330", "159699", "513330"],
    "🧭 宽基指数": ["159915", "510500"],
    "🏭 行业": ["512010", "512880", "515220"],
    "🪙 黄金": ["518880"],
    "🛢 油气": ["162411"],
    "🌏 海外": ["513500"],
}

# 自选ETF列表（由分组拉平，保持顺序：自选在前）
ETF_LIST = [c for codes in ETF_GROUPS.values() for c in codes]

# 债基
BOND_CODE = "270044"

# 单独基金
OTHER_FUND = "270023"

# 产品简称（官方口径）：推送里跟在代码后面。
# 只给一串数字根本认不出买的是什么，尤其场外那两只平时看不到行情页。
# 写死而不是每次从接口取：东财的 fund_etf_fund_daily_em 在境外 runner 上失败是常态，
# 名称没必要跟着一起丢。新增标的时记得在这里补一行；漏了只是不显示名称，不影响信号。
FUND_NAMES = {
    "159545": "港股通红利低波ETF",
    "159941": "纳指ETF广发",
    "513120": "港股创新药ETF广发",
    "159330": "沪深300ETF东财",
    "159699": "恒生消费ETF广发",
    "513330": "恒生互联网ETF华夏",
    # 以下为 2026-10-29 扩池新增，名称取腾讯行情接口的官方简称
    "159915": "创业板ETF易方达",
    "510500": "中证500ETF南方",
    "512010": "医药ETF易方达",
    "512880": "证券ETF国泰",
    "515220": "煤炭ETF国泰",
    "518880": "黄金ETF华安",
    "162411": "华宝油气LOF",
    "513500": "标普500ETF博时",
    "270044": "广发双债添利债券A",
    "270023": "广发全球精选股票(QDII)人民币A",
}

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

# 取多少天历史
# 从 400 提到 600：MA200 要 200 根预热，400 个日历天只有约 270 个交易日，
# 预热完只剩 70 根，长期趋势的滞回状态来不及收敛，判定会偏。
HISTORY_DAYS = 600

# 少于这么多根K线就不出信号
# 必须 > 60：判定金叉要比较「前一根」和「当前根」的 MA60，
# 只有 60 根时前一根的 MA60 还是 NaN，比较恒为 False，会静默漏掉金叉
MIN_BARS = 65

# 长期趋势 = 收盘 vs MA200，外加 0.5 倍日均波动的缓冲带（滞回）
# 2026-10-29 验证（6 只、742 个交易日、两段样本）：
#   贴着均线就进出会被日常波动反复震出，38 段持仓里 29 段是亏的（段胜率 24%）；
#   加缓冲后段数降到 23、胜率 35%，而累计收益持平（+28.1%→+28.6%）、回撤改善（-19.1%→-18.4%）。
#   期B / 期A 两段各自都成立，不是单段拟合。
# 注意：它衡量的是「有没有跌破长期趋势」，不是「会不会涨」——
# 回测显示它抓涨能力为零（上涨捕获率恒等于持仓率），策略层面的意义只有控回撤。
MA_LONG = 200
BAND_K = 0.5

# 溢价率提示线（同日收盘 / 单位净值 - 1，正数表示溢价）
#
# 固定阈值注定会失效，因为溢价水平本身在漂移。159941 实测年度中位数：
#   2018 +0.09%  2019 -0.82%  2020 +1.06%  2021 -0.21%  2022 +0.58%
#   2023 +0.53%  2024 +1.88%  2025 +2.55%  2026 +6.80%（最高 18.85%）
# 同样一个 8%，在 2019 年是百年一遇，在 2026 年却是家常便饭。
#
# 所以分两档：台账里该品种的溢价记录不足 LEDGER_PREM_MIN 天时，用绝对阈值兜底；
# 攒够了就改用它自己的中位数 + 偏离量，自动适应漂移。
PREMIUM_WARN = 8.0
PREMIUM_HEAVY = 12.0
PREMIUM_DEV_WARN = 4.0
PREMIUM_DEV_HEAVY = 7.0
LEDGER_PREM_MIN = 20

# 信号台账（跨天累计，用来统计实盘胜率）
# Actions 每次跑都是全新容器，状态必须落到仓库文件里，由 workflow 结束后提交回去。
LEDGER_PATH = os.getenv("ETF_LEDGER", "signals.json")
LEDGER_FORWARD = (5, 10, 20)   # 信号发出后第几个交易日结算一次
LEDGER_KEEP_DAYS = 260         # 台账只留最近这么多天，防止无限增长

# RSI 分区线：只给 backtest.py 做分组对比，线上推送不做任何 RSI 过滤
# 2026-10-29 回测（1932 个观测、400 天前复权日线）：
#   金叉后 RSI>=65 的那 4 次，20 日平均 +6.37%（胜率75%）；RSI<65 的 16 次只有 -2.14%。
#   「RSI 高就别追」在这个样本上方向是反的，所以过滤器不参与判定，RSI 只当参考数字一起发出去。
# 常量保留，方便以后想复验时改阈值重跑 backtest.py。
RSI_CHASE_LIMIT = 65
RSI_PANIC_LIMIT = 30

# 实时行情数据源：腾讯优先（一次请求带多只、境外可达），东财兜底
# （实测 GitHub runner 访问 push2delay.eastmoney.com 会拿到 502）
SPOT_CODE_KEYS = ("代码", "证券代码")
SPOT_PRICE_KEYS = ("最新价", "现价", "收盘价")
SPOT_RETRIES = 2          # 每个源的重试次数：盘后只靠这一条消息，多一次机会很便宜
SPOT_RETRY_WAIT = 5
# 补价保护：实时价相对历史最后一根变动超过这个比例就不补。
# 场内ETF涨跌停限制 10%，留 5 个点余量；主要拦份额折算/除权：
# 历史用的是前复权价（adjust="qfq"），实时价是未复权真实价，折算日两者口径不一致，
# 直接补进去等于凭空造一根 -50% 的假暴跌K线，会把 MA20 砸穿 MA60 误报死叉。
SPOT_JUMP_LIMIT = 0.15
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 消息正文按 Markdown 渲染（Server酱会把 desp 当 Markdown 转 HTML）。
# 关键：单个换行在 Markdown 里等于空格，所以必须用列表项 / 空行才能真分行，
# 改成纯文本拼 \n 会全部挤成一堆。以后改这里请继续用 "- " 列表。
SIGNAL_ICONS = {
    "买入信号": "🟢",
    "卖出/减仓": "🔴",
    "偏空观望": "🟡",
    "持有/观望": "🔵",
}


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
def should_apply_spot(slot, now):
    """是否用实时价补当日K线

    盘前（未开盘）绝对不能补：那时实时接口的「最新价」就是昨收，
    补进去等于凭空多插一根平盘K线，会把 MA/RSI 算歪。
    盘中、盘后补：此时最新价是当日真实价格，15:20 那条才能算出当日信号。
    """
    if slot == "pre":
        return False
    return now.hour * 60 + now.minute >= 9 * 60 + 30


def name_list(codes):
    """把代码列表转成「代码 名称」的顿号串，给提示文案用；没登记名称的只留代码"""
    return "、".join(
        f"{c} {FUND_NAMES[c]}" if c in FUND_NAMES else c for c in codes
    )


def tencent_code(symbol):
    """转腾讯行情代码：5/6 开头在上交所，其余（15x/16x）在深交所"""
    return ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol


def is_onexchange(symbol):
    """是否场内基金（有盘中行情）

    沪市 50/51/52/53/56/58 开头，深市 15/16/18 开头；其余按场外基金处理（只有净值）
    """
    return symbol.startswith(("50", "51", "52", "53", "56", "58", "15", "16", "18"))


def fetch_spot_tencent(symbols):
    """腾讯行情 https://qt.gtimg.cn/q=sz159545,sh513120

    返回串以 ~ 分隔：[2]=代码 [3]=最新价 [30]=行情时间 yyyyMMddHHmmss
    实测可用；失败时返回 {}，不影响主流程
    """
    try:
        import requests
    except ImportError as e:
        print(f"[提示] requests 不可用，跳过腾讯实时行情: {e}")
        return {}

    codes = ",".join(tencent_code(s) for s in symbols)
    wanted = set(symbols)
    today = datetime.now(BEIJING_TZ).strftime("%Y%m%d")
    last_error = None

    for attempt in range(1, SPOT_RETRIES + 1):
        try:
            resp = requests.get(f"https://qt.gtimg.cn/q={codes}", timeout=20,
                                headers={"User-Agent": UA})
            resp.raise_for_status()
            # 返回为 GBK；显式解码，避免编码错位把 ~ 当成多字节字符的一部分
            text = resp.content.decode("gbk", errors="ignore")
            prices = {}
            for line in text.split(";"):
                if '="' not in line:
                    continue
                field = line.split('="', 1)[1].strip('"').split("~")
                if len(field) < 31:
                    continue
                code = field[2].strip()
                if code not in wanted:
                    continue
                try:
                    price = float(field[3])
                except ValueError:
                    continue
                if math.isnan(price) or price <= 0:
                    continue
                # 行情时间不是今日（未开盘、数据陈旧）就不补，宁缺勿错
                stamp = field[30].strip()
                if len(stamp) >= 8 and stamp[:8] != today:
                    print(f"[提示] {code} 实时行情时间为 {stamp[:8]}，不是今日，不补价")
                    continue
                prices[code] = price
            return prices
        except Exception as e:
            last_error = e
            if attempt < SPOT_RETRIES:
                time.sleep(SPOT_RETRY_WAIT)

    print(f"[提示] 腾讯实时行情获取失败: {last_error}")
    return {}


def fetch_spot_eastmoney(symbols):
    """东财全市场 ETF 实时行情（一次拉取），返回 {代码: 最新价}

    列名因 akshare 版本而异，按候选名取列；取不到就降级，
    宁可只显示上一交易日信号，也不要让整条推送彻底失败
    """
    try:
        import akshare as ak
    except ImportError as e:
        print(f"[提示] akshare 不可用，跳过东财实时行情: {e}")
        return {}

    spot = None
    last_error = None
    for attempt in range(1, SPOT_RETRIES + 1):
        try:
            spot = ak.fund_etf_spot_em()
            break
        except Exception as e:
            last_error = e
            if attempt < SPOT_RETRIES:
                time.sleep(SPOT_RETRY_WAIT)

    if spot is None:
        print(f"[提示] 东财实时行情获取失败（已重试{SPOT_RETRIES}次）: {last_error}")
        return {}
    if spot.empty:
        print("[提示] 东财实时行情返回空数据")
        return {}

    code_col = next((c for c in SPOT_CODE_KEYS if c in spot.columns), None)
    price_col = next((c for c in SPOT_PRICE_KEYS if c in spot.columns), None)
    if not code_col or not price_col:
        print(f"[提示] 东财实时行情列名与预期不符，跳过；实际列: {list(spot.columns)[:12]}")
        return {}

    wanted = set(symbols)
    prices = {}
    for _, row in spot.iterrows():
        code = str(row[code_col]).strip()
        if code in wanted:
            try:
                price = float(row[price_col])
            except (TypeError, ValueError):
                continue
            # NaN 或 <=0 视为停牌/无效价，不拿去补 K 线
            if not math.isnan(price) and price > 0:
                prices[code] = price
    return prices


def get_spot_prices(symbols):
    """当日实时价：腾讯优先，没取到的再用东财补位；两个都失败则返回 {}"""
    prices = fetch_spot_tencent(symbols)
    missing = [s for s in symbols if s not in prices]
    if missing:
        prices.update(fetch_spot_eastmoney(missing))
    return prices


def fetch_kline_tencent(symbol, count=HISTORY_DAYS, adjust=True):
    """腾讯历史日线，返回含「日期 / 收盘」的 DataFrame，失败返回 None

    口径与 akshare 的 fund_etf_hist_em(adjust="qfq") 一致，可作场内 ETF 行情兜底

    adjust=True 取前复权（算 MA/RSI 用，除权不会造成假跳空）；
    adjust=False 取不复权原价——算溢价率必须用它，因为基金净值是不复权的，
    拿前复权价比出来的溢价是假的（分红会被前复权抹掉，溢价凭空变大）。

    已知偏差：请求根数较大时，当日未收盘那根的价格会被截到 2 位小数
    （实测 1.708 变 1.71、1.236 变 1.24），误差 0.3% 以内，不影响指标判定
    """
    try:
        import requests
        import pandas as pd
    except ImportError:
        return None

    scode = tencent_code(symbol)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={scode},day,,,{count},{'qfq' if adjust else ''}")
    try:
        data = requests.get(url, timeout=20, headers={"User-Agent": UA}).json()
        rows = (data.get("data") or {}).get(scode) or {}
        rows = rows.get("qfqday") or rows.get("day") or []
    except Exception as e:
        print(f"[提示] {symbol} 腾讯历史日线获取失败: {e}")
        return None

    records = []
    for row in rows:
        # 每行形如 ["2026-10-28", 开盘, 收盘, 最高, 最低, 成交量, ...]
        if len(row) < 3:
            continue
        try:
            records.append({"日期": str(row[0])[:10], "收盘": float(row[2])})
        except (TypeError, ValueError):
            continue
    if not records:
        print(f"[提示] {symbol} 腾讯历史日线返回空数据")
        return None
    return pd.DataFrame(records)


def fetch_close_on(symbol, date_str):
    """取某一天的不复权收盘价，用于和净值对齐算溢价"""
    df = fetch_kline_tencent(symbol, count=30, adjust=False)
    if df is None or df.empty:
        return None
    hit = df[df["日期"] == date_str]
    if hit.empty:
        return None
    return float(hit.iloc[-1]["收盘"])


def fetch_fund_snapshot(symbols):
    """返回 {代码: (溢价率%, 净值日期)}，取不到的标的直接不进结果

    溢价率 = 同日收盘 / 单位净值 - 1，正数表示溢价。

    这里踩过一个坑必须留在注释里：最初图省事，直接拿 fund_etf_fund_daily_em 的
    「市价」列除以「净值」列，159941 算出 15.1%。但那个接口的两列并不同期——
    市价给的是 10-20 的 1.708，净值给的是 10-27 的 1.4837，隔了 5 个交易日。
    按同日数据重算，10-27 实际是 11.95%（收盘 1.661 / 净值 1.4837），错了 3 个点。
    所以现在先用净值日期，再去查那一天的收盘价，两边口径必须严格对齐。

    价格还得用不复权（adjust=False）：基金净值是不复权的，拿前复权价比，
    分红会被抹掉，溢价会凭空变大。

    为什么不用 fund_etf_spot_em：它在境外 runner 上实测 502（push2delay 域名）。
    失败不响主流程：只是少一个提示，信号照常算。
    """
    try:
        import akshare as ak
    except ImportError as e:
        print(f"[提示] akshare 不可用，跳过溢价率: {e}")
        return {}

    try:
        df = ak.fund_etf_fund_daily_em()
    except Exception as e:
        print(f"[提示] 溢价率数据获取失败: {e}")
        return {}
    if df is None or df.empty:
        return {}

    code_col = next((c for c in ("基金代码", "代码") if c in df.columns), None)
    if not code_col:
        print(f"[提示] 溢价率数据列名与预期不符，跳过；实际列: {list(df.columns)[:10]}")
        return {}

    # 列名形如 "2026-10-28-单位净值"，按日期倒序，取最近一个有值的
    nav_cols = sorted(
        [c for c in df.columns if re.match(r"^\d{4}-\d{2}-\d{2}-单位净值$", str(c))],
        reverse=True,
    )
    if not nav_cols:
        print("[提示] 未找到单位净值列，跳过溢价率")
        return {}

    wanted = set(symbols)
    out = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        if code not in wanted:
            continue
        nav, nav_date = None, ""
        for col in nav_cols:
            try:
                value = float(row[col])
            except (TypeError, ValueError):
                continue
            if not math.isnan(value) and value > 0:
                nav, nav_date = value, str(col)[:10]
                break
        if nav is None:
            continue
        close = fetch_close_on(code, nav_date)
        if not close:
            continue
        out[code] = (round((close / nav - 1) * 100, 2), nav_date)
    return out


def calc_long_trend(df):
    """长期趋势状态：1 = 长期↑，0 = 长期↓，None = 数据不足

    用滞回带判定：站上 MA200 且高出 0.5 倍日均波动才算转强，
    跌破 MA200 且低出 0.5 倍日均波动才算转弱，中间地带维持原状态。

    必须从序列开头逐根递推：当前状态取决于上一次是从哪个方向进来的，
    只看最后一根算不出滞回结果——加缓冲带的全部意义就在这里。
    """
    import pandas as pd
    close = df["收盘"]
    ma = close.rolling(MA_LONG).mean()
    if ma.iloc[-1] != ma.iloc[-1]:
        return None
    vol = close.pct_change().rolling(20).std()
    up_band = ma * (1 + BAND_K * vol)
    dn_band = ma * (1 - BAND_K * vol)

    holding = 0
    for i in range(len(close)):
        u, d = up_band.iloc[i], dn_band.iloc[i]
        if pd.isna(u) or pd.isna(d):
            continue
        if holding == 0 and close.iloc[i] > u:
            holding = 1
        elif holding == 1 and close.iloc[i] < d:
            holding = 0
    return holding


def calculate_rsi(series, period=14):
    """RSI（Wilder 平滑），与通达信/东财等主流行情软件口径一致

    不能用 rolling(period).mean()：那是简单移动平均，算出来是另一个指标。
    实测同一份行情差异可达 14 个点（159941 2026-10-29：SMA 34.3 vs Wilder 48.6），
    而金叉过滤用的正是 RSI<65 这条线，400 天历史里约 15% 的交易日两种算法判定相反。

    Wilder 平滑等价于 alpha=1/period 的指数平滑；用 adjust=False 从序列头部递推，
    历史足够长时与「首个均值用 SMA 播种」的标准实现收敛到同一个值。
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    # 连续横盘时两者都为 0，0/0 得 NaN；这种行情本身没有超买超卖可言，留 NaN 交给调用方处理
    return 100 - (100 / (1 + avg_gain / avg_loss))


def fetch_data(symbol, max_retries=2):
    """取历史日线，返回含「日期 / 收盘」的 DataFrame

    场内 ETF 只用市价接口（东财 → 腾讯兜底），绝不退到场外净值接口：
    净值和市价是两套价格，跨境ETF长期高溢价，
    混进同一条序列算出的 MA/RSI 是错的（实测 159941 净值 1.4647 vs 市价 1.661）
    """
    import akshare as ak
    now = beijing_now()
    end_date = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    last_error = None

    if is_onexchange(symbol):
        attempts = [
            lambda: ak.fund_etf_hist_em(symbol=symbol, period="daily",
                                        start_date=start_date, end_date=end_date, adjust="qfq"),
            lambda: ak.fund_etf_hist_em(symbol=symbol, period="daily", adjust="qfq"),
        ]
    else:
        attempts = [
            lambda: ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势"),
        ]

    for call in attempts:
        for _attempt in range(max_retries):
            try:
                df = call()
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                last_error = e
            time.sleep(1.0)

    # 场内 ETF 兜底：腾讯历史日线，同样是市价 + 前复权，口径与东财一致
    if is_onexchange(symbol):
        df = fetch_kline_tencent(symbol)
        if df is not None:
            print(f"[提示] {symbol} 东财未返回行情，改用腾讯历史日线")
            return df

    print(f"[警告] {symbol} 获取失败: {last_error}")
    return None


def analyze_one(symbol, now, spot_prices, use_spot=True, fund_snap=None,
                show_long=True, prem_base=None):
    """分析单只，返回 (展示文本, 数据基准日, 是否用了当日实时价, 结构化数据)

    数据基准日形如 '10-28'，用来暴露"东财历史日线当天还没收录当日K线"这种情况；
    此时若 use_spot 为真，会用实时价补一根当日K线，基准日后标“当日”。
    返回的展示文本是 Markdown 列表项，供 Server酱 正文直接渲染。

    第 4 项给台账用（带了日期和收盘序列），展示层不需要它，失败时为 None。
    show_long=False 时不输出长期趋势：场外债基净值太平滑，MA200 信号几乎不动，
    印上去只是噪音，还会让人误以为它和 ETF 用同一个阈值。
    prem_base 是该品种自己的溢价中位数（台账攒够了才有），
    有它就用「中位数 + 偏离量」判定，没它就用绝对阈值兜底。
    """
    try:
        import pandas as pd
        df = fetch_data(symbol)
        if df is None or len(df) < MIN_BARS:
            return f"- ⚪ **{symbol}** 数据不足", "", False, None

        # 统一列名处理
        if "收盘" not in df.columns and "单位净值" in df.columns:
            df = df.rename(columns={"单位净值": "收盘"})
        if "日期" not in df.columns and "净值日期" in df.columns:
            df = df.rename(columns={"净值日期": "日期"})

        df = df[["日期", "收盘"]].sort_values("日期").reset_index(drop=True)

        today = now.strftime("%Y-%m-%d")
        patched = False
        if use_spot and str(df.iloc[-1]["日期"])[:10] < today and symbol in spot_prices:
            last_close = float(df.iloc[-1]["收盘"])
            spot_price = spot_prices[symbol]
            if last_close > 0 and abs(spot_price / last_close - 1) > SPOT_JUMP_LIMIT:
                # 宁可少一根当日K线（退回上一交易日信号），也不要拿口径不一致的价算出假买卖点
                print(f"[提示] {symbol} 实时价 {spot_price} 相对上一根 {last_close} "
                      f"变动超过 {SPOT_JUMP_LIMIT:.0%}，疑似折算/除权，不补价")
            else:
                df = pd.concat(
                    [df, pd.DataFrame([{"日期": today, "收盘": spot_price}])],
                    ignore_index=True,
                )
                patched = True

        df["MA20"] = df["收盘"].rolling(20).mean()
        df["MA60"] = df["收盘"].rolling(60).mean()
        df["RSI"] = calculate_rsi(df["收盘"], 14)

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        bar_date = str(latest["日期"])[:10]

        golden = (prev["MA20"] <= prev["MA60"]) and (latest["MA20"] > latest["MA60"])
        death = (prev["MA20"] >= prev["MA60"]) and (latest["MA20"] < latest["MA60"])
        ma20_above = latest["MA20"] > latest["MA60"]

        # 连续横盘会让 avg_gain / avg_loss 都为 0，得 NaN（债基净值很常见）。
        # NaN 无法参与大小比较，显式转成 None，展示时印成 "--" 而不是 "nan"
        raw_rsi = float(latest["RSI"])
        rsi = round(raw_rsi, 1) if raw_rsi == raw_rsi else None

        # 交叉是「事件」，均线排列是「状态」，两者分开判定，避免标签自相矛盾。
        # 这里刻意不加 RSI 过滤：回测显示按 RSI 高低拦信号会让结果更差，
        # 而且一旦「过滤了但仍印金叉」，就会出现「持有/观望 · 金叉」这种自相矛盾的消息。
        # 现在信号和标记永远一致，RSI 只当参考数字一起发出去。
        extra = ""
        event = ""
        if golden:
            signal, extra, event = "买入信号", "金叉", "golden"
        elif death:
            signal, extra, event = "卖出/减仓", "死叉", "death"
        elif ma20_above:
            signal = "持有/观望"
        else:
            signal = "偏空观望"

        # 长期趋势（MA200 + 缓冲带）：给「历史回撤大」的品种做预警，不参与买卖判定
        long_state = calc_long_trend(df) if show_long else None

        # 溢价率只在明显时才印：常规 ETF 折溢价 1% 以内是噪音；
        # QDII 常态就有 6~7%（额度限制），所以绝对阈值抬到 8%。
        # 台账攒够 LEDGER_PREM_MIN 天后改用它自己的中位数：溢价水平年年在漂，
        # 固定阈值必然不准（159941 的年度中位数从 2019 年的 -0.8% 涨到 2026 年的 6.8%）。
        # 净值日期必须能看出来：QDII 净值 T+2，今天看到的溢价对的是两三天前的净值，
        # 不标出来容易被当成实时值（最初那个 15.1% 就是错配日期算出来的）
        prem = None
        prem_text = ""
        if fund_snap and symbol in fund_snap:
            prem, nav_date = fund_snap[symbol]
            base = (prem_base or {}).get(symbol)
            if base is None:
                warn, heavy = PREMIUM_WARN, PREMIUM_HEAVY
            else:
                warn = base + PREMIUM_DEV_WARN
                heavy = base + PREMIUM_DEV_HEAVY
            if prem >= warn:
                prem_text = f"溢价 {prem:.1f}%"
                if nav_date and nav_date != now.strftime("%Y-%m-%d"):
                    prem_text += f"({nav_date[5:]})"
                if prem >= heavy:
                    prem_text += "⚠"

        # Markdown 列表项：- 图标 **代码** 产品名 · 信号 · RSI · 长期 · 溢价 · 基准日
        # 名称紧跟代码、用空格隔开（不拿 · 隔，否则一行被切成太多块反而难读）
        # 当日标记直接用文字：全角＊在手机上渲染得太小看不清，半角 * 又会被 Markdown 当斜体
        rsi_text = f"RSI {rsi}" if rsi is not None else "RSI --"
        head = f"- {SIGNAL_ICONS.get(signal, '⚪')} **{symbol}**"
        name = FUND_NAMES.get(symbol, "")
        if name:
            head += f" {name}"
        parts = [head, signal, rsi_text]
        if extra:
            parts.append(f"**{extra}**")
        if long_state == 1:
            parts.append("长期↑")
        elif long_state == 0:
            # 不加图标、不单独分组：上几轮试过置顶风险区，实际是噪音，这里只加粗
            parts.append("**长期↓**")
        if prem_text:
            parts.append(prem_text)
        if bar_date:
            parts.append(f"{bar_date[5:]} 当日" if patched else bar_date[5:])

        meta = {
            "code": symbol,
            "signal": signal,
            "event": event,
            "long": long_state,
            "close": float(latest["收盘"]),
            "prem": prem,
            "bar_date": bar_date,
            "dates": [str(d)[:10] for d in df["日期"]],
            "closes": [float(v) for v in df["收盘"]],
        }
        return " · ".join(parts), bar_date[5:], patched, meta
    except Exception as e:
        print(f"[警告] {symbol} 分析失败: {e}")
        label = f"{symbol} {FUND_NAMES[symbol]}" if symbol in FUND_NAMES else symbol
        return f"- ❌ **{label}** 分析失败", "", False, None


# ==================== 信号台账（实盘胜率） ====================
# 为什么需要落盘：Actions 每次跑都是全新容器，不存就永远是「第一次」
# 台账结构（紧凑键名，控制文件体积）：
#   {"version":1, "start":"摘要日", "days": {"2026-10-30": {"159941": {"c":价, "sig":事件, "long":状态, "prem":溢价}}}}

def load_ledger():
    """读台账；文件不存在或损坏都当空台账——宁可丢胜率，不能让推送发不出去"""
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
        print("[提示] 台账结构不对，按空台账处理")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"[提示] 台账读取失败，按空台账处理: {e}")
    return {"version": 1, "start": "", "days": {}}


def save_ledger(ledger):
    """原子写：先落临时文件再替换，避免进程被杀时留下半截 JSON 把后续几天全弄坏"""
    days = ledger.get("days") or {}
    if len(days) > LEDGER_KEEP_DAYS:
        ledger["days"] = {k: days[k] for k in sorted(days)[-LEDGER_KEEP_DAYS:]}
        print(f"[台账] 超过 {LEDGER_KEEP_DAYS} 天，已裁掉最早的记录")
    tmp = LEDGER_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
        print(f"[台账] 已写入 {LEDGER_PATH}（{len(ledger['days'])} 个交易日）")
        return True
    except OSError as e:
        print(f"[警告] 台账写入失败: {e}")
        return False


def calc_prem_baseline(ledger):
    """从台账里取每只的溢价率中位数，样本不够 LEDGER_PREM_MIN 天的不给基准

    为什么不信固定阈值：159941 的年度溢价中位数 2019 年是 -0.82%，2026 年是 +6.80%，
    差出七个多点。拿一个写死的数当尺子，过两年必然失效。
    等台账自己攒出几十天，用该品种当时的中位数当基准才跟得上漂移。
    """
    hist = {}
    for rec in (ledger.get("days") or {}).values():
        for code, item in rec.items():
            value = item.get("prem")
            if value is None:
                continue
            hist.setdefault(code, []).append(float(value))
    return {
        code: statistics.median(vals)
        for code, vals in hist.items()
        if len(vals) >= LEDGER_PREM_MIN
    }


def record_ledger(ledger, bar_date, metas):
    """把当日快照写进台账

    只记 ETF 自选列表。事件型（金叉/死叉）入 sig，状态型空着——
    「持有/观望」每天都成立，存下来没有验证价值，只会把文件撑大。
    长期趋势和溢价率每天存一份，几个月后就能拿它们做时间序列分析，
    这正是这次「先观测几个月」的数据基础。
    """
    if not bar_date:
        return 0
    day = ledger["days"].setdefault(bar_date, {})
    for meta in metas:
        if not meta:
            continue
        day[meta["code"]] = {
            "c": round(meta["close"], 4),
            "sig": meta["event"],
            "long": meta["long"],
            "prem": meta["prem"],
        }
    if not ledger.get("start"):
        ledger["start"] = bar_date
    return len([m for m in metas if m])


# 信号方向：金叉后面要涨才算对，死叉后面要跌才算对
LEDGER_DIRECTION = {"golden": ("金叉", 1), "death": ("死叉", -1)}


def verify_ledger(ledger, metas_by_code):
    """用台账里的信号日期 + 今天的真实行情，算信号后第 N 个交易日的涨跌

    为什么不用台账里记的收盘价：前复权价会随分红除权整体重算，
    拿当年记下的价格和今天查到的同一天价格比，会凭空多出涨跌幅。
    所以只在同一条序列内部比（信号日收盘 vs N 个交易日后的收盘）。

    返回 {信号类型: {N: [每笔涨跌]}}，未满 N 日的不计入。
    """
    out = {}
    for code, meta in metas_by_code.items():
        if not meta:
            continue
        idx = {d: i for i, d in enumerate(meta["dates"])}
        closes = meta["closes"]
        for day_str, rec in (ledger.get("days") or {}).items():
            item = rec.get(code)
            if not item or not item.get("sig"):
                continue
            i = idx.get(day_str)
            if i is None:
                continue
            for n in LEDGER_FORWARD:
                j = i + n
                if j >= len(closes) or closes[i] <= 0:
                    continue
                out.setdefault(item["sig"], {}).setdefault(n, []).append(
                    closes[j] / closes[i] - 1)
    return out


def build_ledger_lines(ledger, stats):
    """拼实盘胜率段落；样本不足时明确标出来，不让它看起来像个结论"""
    days = ledger.get("days") or {}
    lines = ["### 📈 实盘验证", ""]
    if not days:
        lines.append("- 台账为空，盘后推送后开始累计（首日没有胜率可算）")
        lines.append("")
        return lines

    start = ledger.get("start") or min(days)
    lines.append(f"- 自 {start[5:]} 起累计 {len(days)} 个交易日")

    has_stat = False
    for sig, (name, direction) in LEDGER_DIRECTION.items():
        total = sum(1 for rec in days.values()
                    for code, item in rec.items() if item.get("sig") == sig)
        if not total:
            continue
        chunks = []
        for n in LEDGER_FORWARD:
            vals = (stats.get(sig) or {}).get(n) or []
            if not vals:
                chunks.append(f"{n}日 待满")
                continue
            has_stat = True
            win = sum(1 for v in vals if v * direction > 0)
            mean = sum(vals) / len(vals) * 100
            chunks.append(f"{n}日 {win}/{len(vals)} 胜 均{mean:+.1f}%")
        lines.append(f"- **{name}** 发出 {total} 次 · " + " · ".join(chunks))

    if not has_stat:
        lines.append("- 暂无已结算样本，满 5 个交易日后出现胜率")
    else:
        lines.append("- 分子是已满 N 日的笔数；均值是实际涨跌，金叉后跌/死叉后涨都算输")
    lines.append("")
    return lines


# ==================== 消息拼装与推送 ====================
def build_message(now, slot, record=False):
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%m-%d")
    today_full = now.strftime("%Y-%m-%d")

    # 只拉一次全市场实时行情，所有 ETF 复用，避免逐只重复请求
    use_spot = should_apply_spot(slot, now)
    spot_prices = get_spot_prices(ETF_LIST) if use_spot else {}
    # 溢价率同理：一次拉全市场 1600+ 只，这里只取池子里那 14 只
    fund_snap = fetch_fund_snapshot(ETF_LIST)

    # 台账先读出来：溢价基准要从里面的历史溢价中位数算，攒够了就不再信固定阈值
    ledger = load_ledger()
    prem_base = calc_prem_baseline(ledger)
    if prem_base:
        print("[溢价] 已改用台账中位数作基准: " + "  ".join(
            f"{k} {v:.2f}%" for k, v in sorted(prem_base.items())))

    # 推送标题栏已有“ETF信号助手·时段”，正文只留时间戳，不再重复一遍
    lines = [f"**{now_str} · {slot_label(slot)}**", ""]
    stale = []
    patched_count = 0
    fresh_count = 0
    metas = []

    # 按大类分小节，每节内保持完整详情（信号/RSI/长期/溢价/基准日一个不少）
    # 分组只是为了长正文能扫读：14 只不分节连成一片，找一只得逐行看
    # 统计量全部定义在循环外面 —— fresh_count/patched_count/stale/metas
    # 必须跨组累计，否则底部「N/14 只」的说明会只算到最后一组
    # 只有 g_ups/g_downs 是每组重新算的（它本来就只该覆盖本节），
    # 把这两个误当成跨组量、或把上面四个误当成组内量，都会算错
    for group_name, group_codes in ETF_GROUPS.items():
        lines.append(f"### {group_name}")
        lines.append("")
        g_ups = 0
        g_downs = 0
        for code in group_codes:
            text, bar_date, patched, meta = analyze_one(
                code, now, spot_prices, use_spot=use_spot,
                fund_snap=fund_snap, prem_base=prem_base)
            lines.append(text)
            if meta:
                metas.append(meta)
                if meta.get("long") == 1:
                    g_ups += 1
                elif meta.get("long") == 0:
                    g_downs += 1
            # 当日数据有两条来路：历史接口自己收录了，或用实时价补了一根，
            # 说明文案要能区分这两种，所以这里两个都统计
            if bar_date == today:
                fresh_count += 1
            # 只有「本该能拿到当日却没拿到」才算异常；盘前不补价，拿到上一交易日数据是正常的
            if use_spot and bar_date and bar_date != today and not patched:
                stale.append(code)
            if patched:
                patched_count += 1
            time.sleep(0.8)  # 避免请求过快
        # 本节尾行：哪个大类在走强，一眼能对比——这是你要的「看热门行业」那层
        # 分母跟池级宽度行同一口径：只算得出长期状态的只数，取不到数的不进分母。
        # 否则一个组两只只取到一只时会写成 1/2，跟下面的 1 只详情对不上。
        # 整节都算不出长期状态就不写，宁可缺这一行也不要拿 0/0 装成一个读数是 0 的组。
        # 组内只有一只也不写：单成员的「本节 0/1 只↑」只是把标题下面那个读数
        # 再念一遍，是冗余不是信息。黄金和油气各自成节之后就是这种情况。
        g_known = g_ups + g_downs
        if g_known and len(group_codes) > 1:
            tail = f"- ▲ 本节 **{g_ups}/{g_known} 只↑**"
            if g_downs:
                tail += f" · {g_downs} 只↓"
            lines.append(tail)
        lines.append("")

    # 长期趋势宽度：一句纯事实，不引申出任何预测
    # 为什么不写成「宽度极低后通常会反弹」，也不写成「全都↑可以进场」：
    #   2026-10-29 两轮证伪。第一轮在本池做，宽度=1 的 20 天分散在 4 个短时段，
    #   切非重叠块后块数为 0；拉长窗口后 p=0.60/0.55。第二轮换 25 只跨十年做样本外，
    #   “全都↑”这类高共振共 39 个非重叠块、落在 40 个独立时段、跨 11 个年份，
    #   均值 +0.31%，而随机打乱反而有 +0.59%，p=0.617——样本够多了，答案是没关系。
    # 所以这里只描述当下状态，历史上接下来发生什么根本不确定。
    ups = sum(1 for m in metas if m.get("long") == 1)
    downs = sum(1 for m in metas if m.get("long") == 0)
    known = ups + downs
    if known:
        width_text = f"- 📐 长期趋势宽度：**{ups}/{known} 只↑**"
        if downs:
            width_text += f" · {downs} 只↓"
        lines.append(width_text)
        lines.append("")

    # 债市晴雨表（场外基金，没有实时价，净值本身 T+1）
    lines.append("### 📊 债市晴雨表")
    lines.append("")
    text, _, _, _ = analyze_one(BOND_CODE, now, spot_prices, use_spot=False, show_long=False)
    lines.append(text)
    lines.append("")

    # 单独基金
    lines.append("### 📌 其他基金")
    lines.append("")
    text, _, _, _ = analyze_one(OTHER_FUND, now, spot_prices, use_spot=False, show_long=False)
    lines.append(text)
    lines.append("")

    # 台账：先把当日快照落盘，再算胜率，这样首日推送就能显示「累计 1 个交易日」
    # 只在拿到当日真实K线时记录，否则记下的就是上一交易日的价格，会把日期弄错一天
    bar_dates = {m["bar_date"] for m in metas if m.get("bar_date")}
    if record and bar_dates == {today_full}:
        record_ledger(ledger, today_full, metas)
        save_ledger(ledger)
    elif record:
        print(f"[台账] 当日K线未齐（{sorted(bar_dates) or '无'}），本次不记录")

    metas_by_code = {m["code"]: m for m in metas}
    lines.extend(build_ledger_lines(ledger, verify_ledger(ledger, metas_by_code)))


    # 数据来源说明，让消息里能看出算的是哪一天的价
    # 说明也用列表项，理由同上：纯文本换行在 Markdown 里不生效
    lines.append("---")
    lines.append("")
    notes = []
    total = len(ETF_LIST)
    if slot == "pre":
        notes.append("盘前时段，指标按上一交易日收盘计算")
    elif not fresh_count:
        # 历史接口和实时价都没给到今天的数据
        notes.append("⚠ 当日行情未取到，ETF 信号为上一交易日数据")
    else:
        # 判据是基准日是不是今天，不是有没有补价：
        # 历史接口（含腾讯兜底）本身就有当日K线时 patched_count=0，但数据是当天的，
        # 不能报“当日行情未取到”
        if patched_count:
            notes.append(f"标“当日”的是今日实时价（{patched_count}/{total} 只）")
        else:
            notes.append(f"ETF 信号已含当日行情（{fresh_count}/{total} 只）")
        if stale:
            # 不区分“接口没给价”和“触发折算保护主动不补”，两种情况都去日志里看具体原因
            notes.append(f"⚠ 当日价未补齐：{name_list(stale)}（原因见运行日志）")
    notes.append(f"场外基金（{BOND_CODE}/{OTHER_FUND}）净值为 T+1，通常滞后一天")
    lines.extend(f"- {note}" for note in notes)
    lines.append("")

    # 尾注只说证据支持的话。原来那句「有死叉或重大利空优先减仓」是模型
    # 还叫「信号模型」时留下的，而金叉/死叉判方向已经被实测推翻（30 只
    # 22260 个观测，超额≈0）——继续留着等于让一条已证伪的规则替读者做决定。
    # 以后改这三行只加实测支持的内容，不要再写成买卖指令。
    # 必须带 "- " 列表前缀：上面约 170 行写过这条规矩——两个 append 之间只有一
    # 个 \n，在 Markdown 里是软换行，微信会把三句挤成一段。凑成一段就没人读完。
    lines.append("- 💡 有证据的三件事：**长期↓** 当排除器用（↑ 内部排序≈噪声）、"
                 "**MA200 缓冲带** 主要作用是控回撤不是选方向、"
                 "**溢价率** 比的是该品种自己的历史中位")
    lines.append("- 💡 已证伪的一件事：🟢金叉/🔴死叉 判断方向"
                 "（30 只 22260 个观测，超额≈0）——它只是标记，不是买卖指令")
    lines.append("- 💡 本信息仅供行情回顾，不构成投资建议")

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
    parser.add_argument("--record", action="store_true",
                        help="强制把当日快照写入台账（默认只在盘后自动推送时写，预览不污染数据）")
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
    # 台账只在盘后写：盘中价随时在变，记下去会把当天信号定成当时那个快照
    record = args.record or (slot == "close" and not args.preview)
    content = build_message(now, slot, record=record)
    print(content)
    print("-" * 40)

    # 标题会显示在微信通知栏，带上时间便于区分一天里的三条
    title = f"ETF信号助手·{slot_label(slot)}·{now.strftime('%m-%d %H:%M')}"
    if not send_serverchan(title, content, preview=args.preview):
        # 退出码非 0，让 Actions 变红，避免"静默失败"
        return 1

    print("预览完成（未发送）" if args.preview else "推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())

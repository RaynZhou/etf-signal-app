#!/usr/bin/env python3
"""
每日ETF信号推送脚本（配合 GitHub Actions + Server酱）

时区说明：GitHub Actions 的 runner 是 UTC，本脚本所有与"现在几点"有关的计算统一换算成北京时间（UTC+8），
避免消息里显示 UTC 时间、时段标签判错。

用法：
    python daily_etf_push.py --detect-slot   只判定当前北京时间落在哪个推送窗口，输出给 Actions
    python daily_etf_push.py --slot close    按指定时段（mid / close / manual）推送
    python daily_etf_push.py --slot mid --preview   只生成并打印消息，不发送（不占每日额度）
    python daily_etf_push.py --slot close --record  强制写入当日台账（本地补录用）
    python daily_etf_push.py --backfill-prem 用历史净值回填溢价历史，让动态基准立刻生效
    python daily_etf_push.py                 按当前北京时间自动判定时段后推送

台账：signals.json 记每天每只的信号/长期趋势/溢价率，用来算「从第一天推送开始」的实盘胜率。
     Actions 每次是全新容器，所以这个文件必须提交回仓库才有意义（见 workflow）。

# 额度提醒：Server酱免费版每天只有 5 条，正常自动推送是一天 2 条（盘中+盘后），
# 调试时优先用 --preview。2026-10-30 砍掉了盘前那条：它取不到当日K线，
# 内容与前一天盘后那条逐字相同，只是把同样的均线再发一遍（详见 SLOT_WINDOWS 注释）。
"""

import os
import sys
import time
import math
import json
import re
import statistics
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
#
# 2026-10-30 砍掉「盘前」窗口（原 08:50-09:20）。理由不是额度不够，是它没有信息量：
# 盘前拿不到当日K线（should_apply_spot 在盘前直接返回 False），所以盘前那条的
# MA/RSI/长期状态与昨天盘后那条**逐字相同**，唯一差别是「盘前时段」四个字。
# 等于每天 3 条里烧掉 1 条去复读上一条，占免费版 5 条的 20%。
# 要恢复盘前推送，前提是先接一个真正有增量的数据源（隔夜美股、港股夜期、外盘期货），
# 而不是把昨天的均线再发一遍。在那之前这里不保留 pre。
SLOT_WINDOWS = {
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
# 中位数只取最近 LEDGER_PREM_WINDOW 天：全历史中位数会被旧样本拽住，在单边漂移下
# 反而比固定阈值更误导（推导见 calc_prem_baseline 的 docstring）。
PREMIUM_WARN = 8.0
PREMIUM_HEAVY = 12.0
PREMIUM_DEV_WARN = 4.0
PREMIUM_DEV_HEAVY = 7.0
# 启用门槛从 20 提到 40：台账每天只贡献 1 个样本，20 个样本的中位数极不稳定
# （插进两三个极端值就能拽走两三个点），40 个才勉强能当尺子用。窗口仍是 60，
# 所以第 40-60 天是稳定区，第 20-40 天那种「已启用但样本不足」的模糊地带被绕开了。
LEDGER_PREM_MIN = 40
LEDGER_PREM_WINDOW = 60      # 溢价基准只回看最近这么多天，避免被多年前的水平拽住
# 回填的溢价历史保留天数：比窗口多留 30 天，方便以后换窗口时不必重拉
PREM_HIST_KEEP = LEDGER_PREM_WINDOW + 30

# 信号台账（跨天累计，用来统计实盘胜率）
# Actions 每次跑都是全新容器，状态必须落到仓库文件里，由 workflow 结束后提交回去。
LEDGER_PATH = os.getenv("ETF_LEDGER", "signals.json")
LEDGER_FORWARD = (5, 10, 20)   # 信号发出后第几个交易日结算一次
LEDGER_MIN_SAMPLE = 10         # 结算样本少于此数只报笔数、不出胜率
LEDGER_STALE_BARS = 2          # 台账落后超过这么多个交易日就告警（漏一天算常态，连续漏才说明回写挂了）
LEDGER_KEEP_DAYS = 260         # 台账只留最近这么多天，防止无限增长

# 事件回看窗口：金叉/死叉原先只比对「最后两根」，跨日的事件就会凭空消失。
# 2026-10-30 实测：513120 在 3 个交易日前交叉、515220 在 6 个交易日前交叉，
# 而当天推送 event 为空，读者看到「偏空观望」，完全不知道刚发生过什么。
# 更隐蔽的后果在台账：record 只记当天的 event，所以推送漏发那天（占坑失败、网络失败、
# Actions 挂掉）的交叉永久消失——「实盘胜率」的样本天然只含推送成功那天的交叉，这是选择偏差。
# 回看 5 个交易日：覆盖一个完整交易周，又不至于把早已走完的交叉翻出来当新闻。
EVENT_LOOKBACK = 5

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
    "金叉": "🟢",
    "死叉": "🔴",
    "偏空观望": "🟡",
    "持有/观望": "🔵",
}


def signal_icon(signal):
    """按前缀取图标

    加了回看窗口之后，signal 会出现「金叉（3日前）」这种带后缀的形态。
    用 SIGNAL_ICONS.get(signal) 精确匹配会掉成 ⚪——图标说「无信号」而文字说「金叉」，
    比没有图标更糟。所以统一走前缀匹配。
    """
    for key, icon in SIGNAL_ICONS.items():
        if signal.startswith(key):
            return icon
    return "⚪"


# 事件 → 展示文案。30 只 22260 个观测的回测说金叉/死叉对后续涨跌没有稳定预测力
# （超额≈0），所以这里印「金叉」而不是「买入信号」：后者等于替它承诺一件证据
# 不支持的事，还和尾注「它只是标记，不是买卖指令」正面打架。
# 台账里的 sig 仍存 golden/death，结算口径与展示文案无关。
EVENT_LABELS = {"golden": "金叉", "death": "死叉"}


def signal_label(event, ma20_above, days_ago=0):
    """事件优先于状态：回看窗口内有交叉就印交叉，否则按均线排列给状态

    两者分开判定，避免「持有/观望 · 金叉」这种自相矛盾的组合。
    RSI 刻意不参与：回测显示按 RSI 高低拦信号会让结果更差，而且一旦
    「过滤了但仍印金叉」，标签又会矛盾。RSI 只当参考数字一起发。

    days_ago 是交叉距今天几个交易日（0 = 今天）。非 0 时必须标出来：
    直接印「金叉」会让人以为是今天发生的，而它可能是 4 个交易日前那根，
    中间的价格早就走完了——这正是金叉证伪结论里说的「滞后性」。

    抽成独立函数是为了能被测到：之前这段直接写在 analyze_one 里，
    测试只能拿 stub 自造的字符串去断言，改回「买入信号」竟然测不出来。
    """
    if event in EVENT_LABELS:
        label = EVENT_LABELS[event]
        return label if not days_ago else f"{label}（{days_ago}日前）"
    return "持有/观望" if ma20_above else "偏空观望"


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


# A股 15:00 收盘：这之后当日的K线才定格，之前所有「当日」数字都是盘中瞬时值
CLOSE_MINUTE = 15 * 60


def bar_is_final(now):
    """当日K线是否已定格（收盘后）

    为什么不按 slot 判断：手动触发（manual）不在任何窗口里，但 18:07 手动跑的
    当然是收盘数据、10:13 跑的不是。按钟点判对两种触发方式都成立。
    """
    return now.hour * 60 + now.minute >= CLOSE_MINUTE


def bar_suffix(bar_date, now):
    """日期列里该跟什么后缀：今天这根K线在收盘前要标「盘中」

    上一版用「当日」表示「这个价是我用实时价补的」，于是同一个盘中快照会同时
    出现两副样子：走腾讯兜底的 1 只带「当日」，走东财的 13 只不带——东财盘中
    已经返回了当日那根未收盘K线，根本进不了补价分支。底部那句「标当日的是今日
    实时价（1/14 只）」于是被读成「另外 13 只是昨天的数据」，而它们其实是今天
    的半根。标记该回答「这根K线完不完整」，不是「它从哪个接口来」。
    """
    if not bar_date:
        return ""
    day = bar_date[5:]
    if bar_date != now.strftime("%Y-%m-%d") or bar_is_final(now):
        return day
    return f"{day} 盘中"


# ==================== 数据与指标 ====================
def should_apply_spot(slot, now):
    """是否用实时价补当日K线

    开盘前（未到 09:30）绝对不能补：那时实时接口的「最新价」就是昨收，
    补进去等于凭空多插一根平盘K线，会把 MA/RSI 算歪。
    盘中、盘后补：此时最新价是当日真实价格，15:20 那条才能算出当日信号。

    盘前推送窗口已删，但这里不能跟着删：手动跑 --slot manual、或外部定时器
    在 09:30 前误触发时，这里就是唯一一道拦下来不让实时价渗进来的门。
    """
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


def fetch_close_on(symbol, date_str, count=90):
    """取某一天的不复权收盘价，用于和净值对齐算溢价

    count 从 30 提到 90：30 根只覆盖约 42 个日历日，QDII 净值滞后 2 天，
    再遇上长假（国庆/春节休市 8 天）可能落到窗口外，那时溢价会静默消失而不是报错。
    90 根约 130 个日历日，足够跨过任何一次长假。
    """
    df = fetch_kline_tencent(symbol, count=count, adjust=False)
    if df is None or df.empty:
        return None
    hit = df[df["日期"] == date_str]
    if hit.empty:
        return None
    return float(hit.iloc[-1]["收盘"])


def fetch_nav_history(code, days=90):
    """单只基金最近 days 天的历史单位净值，返回 {日期: 净值}（日期形如 2026-10-28）

    回填专用。为什么必须单独走这条路：fund_etf_fund_daily_em 只返回最近 2 列净值，
    做得了「今天的溢价」但做不了「过去 60 天的溢价」；而 fund_open_fund_info_em
    返回完整历史（实测 159941 有 2739 行、162411 有 3661 行），ETF 与 LOF 都覆盖。
    这是让「动态溢价基准」不必空等 40 个交易日的唯一路径。
    """
    try:
        import akshare as ak
    except ImportError as e:
        print(f"[提示] akshare 不可用，无法回填净值历史: {e}")
        return {}

    try:
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    except Exception as e:
        print(f"[提示] {code} 净值历史获取失败: {e}")
        return {}
    if df is None or df.empty:
        return {}

    date_col = next((c for c in ("净值日期", "日期") if c in df.columns), None)
    nav_col = next((c for c in ("单位净值", "净值") if c in df.columns), None)
    if not date_col or not nav_col:
        print(f"[提示] {code} 净值历史列名不符，跳过；实际列: {list(df.columns)[:6]}")
        return {}

    out = {}
    for _, row in df.tail(days + 40).iterrows():        # 多取一些再去掉非交易日
        try:
            d = str(row[date_col])[:10]
            v = float(row[nav_col])
        except (TypeError, ValueError):
            continue
        if v != v or v <= 0:
            continue
        out[d] = v
    return dict(sorted(out.items())[-days:])


def fetch_nav_batch(symbols):
    """批量取「最近一个可用单位净值」，返回 {代码: (nav, 'YYYY-MM-DD')}

    两个源，按「快但窄 → 慢但全」的顺序用：
      ① fund_etf_fund_daily_em：一次拉全市场 ETF，但列名是日期透出的宽表，
         只回最近两列净值。快，但不覆盖 LOF。
      ② fund_open_fund_info_em：一次一只，返回完整历史，ETF 与 LOF 都覆盖。

    为什么必须补上②：162411 华宝油气是 LOF，在①里根本不存在，
    而它恰恰是全池最该看溢价的标的——LOF 折溢价本身就是套利核心。
    2026-10-30 实测：14 只里①命中 13 只，唯一缺的就是 162411，
    而旧代码只是不印溢价、不作任何提示，读者会以为它没有溢价问题。
    """
    wanted = list(dict.fromkeys(symbols))
    out = {}

    try:
        import akshare as ak
    except ImportError as e:
        print(f"[提示] akshare 不可用，跳过溢价率: {e}")
        return out

    # ① 批量宽表
    try:
        wide = ak.fund_etf_fund_daily_em()
    except Exception as e:
        print(f"[提示] 溢价率批量源获取失败，改走逐只净值: {e}")
        wide = None

    if wide is not None and not wide.empty:
        code_col = next((c for c in ("基金代码", "代码") if c in wide.columns), None)
        nav_cols = sorted(
            [c for c in wide.columns if re.match(r"^\d{4}-\d{2}-\d{2}-单位净值$", str(c))],
            reverse=True,
        )
        if code_col and nav_cols:
            wanted_set = set(wanted)
            for _, row in wide.iterrows():
                code = str(row[code_col]).strip()
                if code not in wanted_set or code in out:
                    continue
                for col in nav_cols:
                    try:
                        value = float(row[col])
                    except (TypeError, ValueError):
                        continue
                    if value != value or value <= 0:
                        continue
                    out[code] = (value, str(col)[:10])
                    break
        else:
            print(f"[提示] 溢价率宽表列名与预期不符，改走逐只净值；实际列: {list(wide.columns)[:10]}")

    # ② 逐只兜底（主要是 LOF）
    missing = [c for c in wanted if c not in out]
    for code in missing:
        hist = fetch_nav_history(code, days=10)
        if hist:
            last_date = max(hist)
            out[code] = (hist[last_date], last_date)
            print(f"[溢价] {code} 不在 ETF 净值宽表里，已用单只净值兜底（{last_date}）")

    return out


def fetch_fund_snapshot(symbols, max_workers=6):
    """返回 {代码: {"prem": 溢价率%, "label": 净值日 MM-DD, "nav_date": 完整日期}}

    溢价率 = 同日收盘 / 单位净值 - 1，正数表示溢价。取不到的标的直接不进结果。

    这里踩过一个坑必须留在注释里：最初图省事，直接拿 fund_etf_fund_daily_em 的
    「市价」列除以「净值」列，159941 算出 15.1%。但那个接口的两列并不同期——
    市价给的是 10-20 的 1.708，净值给的是 10-27 的 1.4837，隔了 5 个交易日。
    按同日数据重算，10-27 实际是 11.95%（收盘 1.661 / 净值 1.4837），错了 3 个点。
    所以现在先用净值日期，再去查那一天的收盘价，两边口径必须严格对齐。

    价格还得用不复权（adjust=False）：基金净值是不复权的，拿前复权价比，
    分红会被抹掉，溢价会凭空变大。

    为什么不用 fund_etf_spot_em 里的 IOPV 做「实时溢价」：
    2026-10-30 实测该接口在国内网络下拉 16 页分页，第 6 页就 ProxyError；
    GitHub runner 上更是长期 502。把一个「大部分时候取不到」的源塞进主链路，
    只会让溢价时有时无、口径时实时时净值，比老实的 T-1 更误导。
    所以这里明确只给净值口径，并让调用方把「这是哪天」印到消息里。

    并发取收盘价：14 只串行要 23.5 秒（实测），而这一步不依赖顺序，
    用线程池能压到 4-5 秒。东财/腾讯对并发 6 个请求没有表现出限流。
    """
    navs = fetch_nav_batch(symbols)
    if not navs:
        return {}

    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {pool.submit(fetch_close_on, code, nav_date): (code, nav, nav_date)
                   for code, (nav, nav_date) in navs.items()}
        for future in as_completed(pending):
            code, nav, nav_date = pending[future]
            try:
                close = future.result()
            except Exception as e:
                print(f"[提示] {code} 溢价对齐失败: {e}")
                continue
            if not close:
                # 净值日在 90 根K线之外（长期停牌等），这一只就当没有溢价，不要编一个
                print(f"[提示] {code} 在 {nav_date} 没有不复权收盘价，跳过溢价")
                continue
            out[code] = {
                "prem": round((close / nav - 1) * 100, 2),
                "label": nav_date[5:],
                "nav_date": nav_date,
            }
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
    """取历史日线，返回含「日期 / 收盘」的 DataFrame，df.attrs 里带数据源与根数

    场内 ETF 只用市价接口（东财 → 腾讯兜底），绝不退到场外净值接口：
    净值和市价是两套价格，跨境ETF长期高溢价，
    混进同一条序列算出的 MA/RSI 是错的（实测 159941 净值 1.4647 vs 市价 1.661）

    场外基金反过来：必须用累计净值，不能用单位净值。单位净值每次分红会凭空掉一笔
    台阶（270044 每份掉 0.009~0.011 元 ≈ 净值的 0.85%，而它平时的日波动只有
    0.008%~0.08%，等于一根 10~30 倍于平时的假阴线），累计净值在分红日不动
    （累计分红同额增加，把台阶抵消了）。实测这根假阴线的代价：2026-01-14 分红后
    第 8 个交易日，单位净值口径给 RSI 32.7、累计净值口径给 90.1 —— 一个读起来像
    「接近超卖」，一个是「十四年来最强的一档」，结论完全相反，而且 Wilder 平滑
    会让这个错误持续两三个月。270044 是季度分红（1/4/7/10 月），所以它每季度复发。

    为什么换成累计净值没有副作用：无分红期间两条序列只差一个常数，而常数会
    被 RSI 的比值约掉，也不改变「收盘 vs MA200」的相对位置。已实测验证：把每次
    除权金额手工加回单位净值构造的严格复权序列，RSI 与直接用累计净值完全相同（86.5）。

    为什么要把数据源写进 attrs：三条分支给出的窗口长度差得很远——实测同一批 14 只里
    东财带日期是 400 根、东财不带日期是全历史（162411 拿到 3346 根）、腾讯是 601 根。
    对单只标的的指标没影响（滞回递推会收敛），但任何跨标的比较都会因窗口不同而不可比。
    而旧版的失败只在 stdout 打一行，消息里仍统一写「数据来源：东方财富 / 腾讯」，
    读者无从判断某只到底走的哪条源。这个标签现在会一路带到推送底部。
    """
    import akshare as ak
    now = beijing_now()
    end_date = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    last_error = None

    if is_onexchange(symbol):
        attempts = [
            ("eastmoney-band", lambda: ak.fund_etf_hist_em(
                symbol=symbol, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq")),
            ("eastmoney-full", lambda: ak.fund_etf_hist_em(
                symbol=symbol, period="daily", adjust="qfq")),
        ]
    else:
        # 累计净值而不是单位净值，见函数开头那段分红台阶的说明。取数层就定死口径，
        # 不留给调用方选：它们要是各自挑一条，两条序列差 39 个 RSI 点的事还会重演。
        # 注意只改这一条路径——算溢价必须用单位净值（fetch_nav_history 那边不许动），
        # 因为溢价是「场内价格相对每份净值的偏离」，加回了分红就不是那个东西了。
        attempts = [
            ("eastmoney-nav", lambda: ak.fund_open_fund_info_em(
                symbol=symbol, indicator="累计净值走势")),
        ]

    for source, call in attempts:
        for _attempt in range(max_retries):
            try:
                df = call()
                if df is not None and not df.empty:
                    df.attrs["source"] = source
                    df.attrs["bars"] = len(df)
                    return df
            except Exception as e:
                last_error = e
            time.sleep(1.0)

    # 场内 ETF 兜底：腾讯历史日线，同样是市价 + 前复权，口径与东财一致
    if is_onexchange(symbol):
        df = fetch_kline_tencent(symbol)
        if df is not None:
            df.attrs["source"] = "tencent"
            df.attrs["bars"] = len(df)
            print(f"[提示] {symbol} 东财未返回行情，改用腾讯历史日线")
            return df

    print(f"[警告] {symbol} 获取失败: {last_error}")
    return None


def prem_flag(prem, base=None):
    """溢价是否该提示：'' / 'warn' / 'heavy'

    有台账中位数就按「本品种中位数 + 偏离量」判，没有才退回绝对阈值。
    抽成函数是为了让 app 和推送用同一条线——两边各写一遍阈值，早晚会分叉。
    """
    if prem is None:
        return ""
    if base is None:
        warn, heavy = PREMIUM_WARN, PREMIUM_HEAVY
    else:
        warn = base + PREMIUM_DEV_WARN
        heavy = base + PREMIUM_DEV_HEAVY
    if prem >= heavy:
        return "heavy"
    if prem >= warn:
        return "warn"
    return ""


def find_last_cross(df, lookback=EVENT_LOOKBACK):
    """在最近 lookback+1 根K线里找最近一次 MA20/MA60 交叉

    返回 (event, days_ago, 交叉日)；没有交叉返回 ("", None, "")。

    判定式与原版「只看最后两根」逐字一致（prev<= 且 cur> 算金叉，prev>= 且 cur< 算死叉），
    所以第 0 根的行为没有任何变化——向后扩展只是让 1~lookback 根前的交叉也能被看见。
    持平（MA20 == MA60）时按原语义两边都能触发，债基连续横盘不会凭空产生事件。

    为什么必须回看：金叉/死叉是「事件」不是「状态」，只在发生那一刻有信息。
    只看最后两根意味着——如果推送在交叉当天没发出去（占坑失败/网络失败/手动跳过），
    这个事件就永久消失了：后一天的推送不会提它，台账也不会记它。
    “实盘胜率”的样本于是天然只含“推送成功那天发生的交叉”，这是选择偏差。
    """
    if df is None or len(df) < 2:
        return "", None, ""
    ma20, ma60 = df["MA20"], df["MA60"]
    for k in range(0, min(lookback, len(df) - 1) + 1):
        i = len(df) - 1 - k
        if i < 1:
            break
        p20, p60 = ma20.iloc[i - 1], ma60.iloc[i - 1]
        c20, c60 = ma20.iloc[i], ma60.iloc[i]
        # NaN 参与比较恒为 False，暖机不足的早期下标会自动跳掉
        if p20 <= p60 and c20 > c60:
            return "golden", k, str(df["日期"].iloc[i])[:10]
        if p20 >= p60 and c20 < c60:
            return "death", k, str(df["日期"].iloc[i])[:10]
    return "", None, ""


def evaluate_df(df, show_long=True):
    """从日线序列算出全部指标与信号。不取数、不渲染、不联网

    这是推送和 app 的唯一口径来源。app 以前自己用简单移动平均算 RSI，
    同一只 159941 在 2026-10-29 算出 34.3，推送给出 48.6，差 14 个点，
    还拿这个数去过滤金叉。现在两边都调这个函数，口径不可能再分叉。

    数据不足（少于 MIN_BARS 根）返回 None。MIN_BARS 必须 > 60：判金叉要比较
    前一根和当前根的 MA60，只有 60 根时前一根的 MA60 还是 NaN，比较恒为 False。
    """
    if df is None or len(df) < MIN_BARS:
        return None

    # 场外基金给的是净值口径（现在是「累计净值」），统一成「收盘/日期」，下游不用分两套。
    # 用 next() 在一串候选列名里挑，而不是写死一个：口径换过一次就会漏改另一处，
    # 让它自适应比四处 if 更难出错。
    nav_col = next((c for c in ("收盘", "单位净值", "累计净值", "净值")
                    if c in df.columns), None)
    if nav_col and nav_col != "收盘":
        df = df.rename(columns={nav_col: "收盘"})
    if "日期" not in df.columns and "净值日期" in df.columns:
        df = df.rename(columns={"净值日期": "日期"})
    df = df[["日期", "收盘"]].sort_values("日期").reset_index(drop=True)

    df["MA20"] = df["收盘"].rolling(20).mean()
    df["MA60"] = df["收盘"].rolling(60).mean()
    df["RSI"] = calculate_rsi(df["收盘"], 14)

    latest = df.iloc[-1]
    # 持平算「不偏空」：连续横盘（债基净值天天一个价）会让 MA20 和 MA60 完全相等，
    # 用严格大于会把这种行情印成「偏空观望」——凭空给了方向，而它其实没有
    ma20_above = bool(latest["MA20"] >= latest["MA60"])

    # 连续横盘会让 avg_gain / avg_loss 都为 0，得 NaN（债基净值很常见）。
    # NaN 无法参与大小比较，显式转成 None，展示时印成 "--" 而不是 "nan"
    raw_rsi = float(latest["RSI"])
    rsi = round(raw_rsi, 1) if raw_rsi == raw_rsi else None

    event, event_days_ago, event_date = find_last_cross(df)
    return {
        "df": df,
        "bar_date": str(latest["日期"])[:10],
        "close": float(latest["收盘"]),
        "ma20": float(latest["MA20"]),
        "ma60": float(latest["MA60"]),
        "rsi": rsi,
        # golden/death 保留「当日交叉」的旧语义，供需要二值判断的地方用；
        # 跨日事件走 event/event_days_ago，两者不要混用
        "golden": event == "golden" and event_days_ago == 0,
        "death": event == "death" and event_days_ago == 0,
        "ma20_above": ma20_above,
        "event": event,
        "event_days_ago": event_days_ago,
        "event_date": event_date,
        "signal": signal_label(event, ma20_above, event_days_ago or 0),
        "long": calc_long_trend(df) if show_long else None,
    }


def evaluate(symbol, now, spot_prices=None, use_spot=False, fund_snap=None,
             show_long=True):
    """取数 + 补当日价 + 算指标，推送和 app 共用这一个入口

    返回 dict；ok=False 时只有 reason 有意义。溢价原样带出（prem / nav_date），
    要不要提示、要不要加⚠交给 prem_flag 判——阈值属于展示决策，不该混进取数。
    溢价基准（台账中位数）也不在这里处理：同一个数字在「有基准」和「无基准」
    两种情形下的含义不同，取数层不该知道台账长什么样。

    补价规则：历史最后一根早于今天才补，且相对上一根变动超过 SPOT_JUMP_LIMIT
    就不补。前复权价和未复权实时价混拼会造出一根假跳空，宁可退回上一交易日信号。
    """
    import pandas as pd
    spot_prices = spot_prices or {}
    df = fetch_data(symbol)
    if df is None:
        return {"ok": False, "reason": "取不到行情"}

    # 必须在切片前读 attrs：df[["日期", "收盘"]] 之后 attrs 不保证跟着走
    source = df.attrs.get("source", "")
    bars = df.attrs.get("bars", 0)

    nav_col = next((c for c in ("收盘", "单位净值", "累计净值", "净值")
                    if c in df.columns), None)
    if nav_col and nav_col != "收盘":
        df = df.rename(columns={nav_col: "收盘"})
    if "日期" not in df.columns and "净值日期" in df.columns:
        df = df.rename(columns={"净值日期": "日期"})
    df = df[["日期", "收盘"]].sort_values("日期").reset_index(drop=True)

    today = now.strftime("%Y-%m-%d")
    patched = False
    if use_spot and len(df) and symbol in spot_prices \
            and str(df.iloc[-1]["日期"])[:10] < today:
        last_close = float(df.iloc[-1]["收盘"])
        spot_price = spot_prices[symbol]
        if last_close > 0 and abs(spot_price / last_close - 1) > SPOT_JUMP_LIMIT:
            print(f"[提示] {symbol} 实时价 {spot_price} 相对上一根 {last_close} "
                  f"变动超过 {SPOT_JUMP_LIMIT:.0%}，疑似折算/除权，不补价")
        else:
            df = pd.concat(
                [df, pd.DataFrame([{"日期": today, "收盘": spot_price}])],
                ignore_index=True,
            )
            patched = True

    ev = evaluate_df(df, show_long)
    if ev is None:
        return {"ok": False, "reason": "数据不足"}
    ev["ok"] = True
    ev["patched"] = patched
    ev["source"] = source
    ev["bars"] = bars
    # 溢价是 dict 而不是 tuple 了：label 就是「这个溢价对应哪一天的净值」，
    # 展示层必须把它印出来——同一个 14.5% 在净值日 T-1 和 T-5 下含义完全不同
    snap = (fund_snap or {}).get(symbol) or {}
    ev["prem"] = snap.get("prem")
    ev["prem_label"] = snap.get("label") or ""
    ev["prem_nav_date"] = snap.get("nav_date") or ""
    return ev


# 数据源 → 展示名，用于推送底部那句「各只到底走的哪条源」
SOURCE_NAMES = {
    "eastmoney-band": "东财",
    "eastmoney-full": "东财全历史",
    "eastmoney-nav": "东财净值",
    "tencent": "腾讯",
    "": "未知",
}


def source_summary(sources):
    """把数据源计数拼成一行：{source: n} → '东财 8 · 腾讯 6'"""
    counts = {}
    for s in sources:
        counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    return " · ".join(f"{SOURCE_NAMES.get(k, k)} {v}"
                      for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def analyze_one(symbol, now, spot_prices, use_spot=True, fund_snap=None,
                show_long=True, prem_base=None):
    """分析单只，返回 (展示文本, 数据基准日 MM-DD, 是否用了当日实时价, 结构化数据)

    数据基准日形如 '10-28'，用来暴露"东财历史日线当天还没收录当日K线"这种情况；
    此时若 use_spot 为真，会用实时价补一根当日K线。基准日后面跟什么由 bar_suffix
    决定：今天这根在收盘前标「盘中」，收盘后只写日期、不再区分它是接口给的还是补的。
    返回的展示文本是 Markdown 列表项，供 Server酱 正文直接渲染。

    第 4 项给台账用（带了日期和收盘序列），展示层不需要它，失败时为 None。
    show_long=False 时不输出长期趋势：场外债基换成累计净值之后，MA200 已经不再被
    分红台阶踢来踢去（十四年持续判↑、翻转 0 次），但正因如此它对这个品种毫无区分度
    —— 一条永远为真的线印出来只是噪音，还会让人误以为它和 ETF 用同一个阈值。
    RSI 不一样，它是会被分红砸错的，所以这个字段必须留、也必须算对。
    prem_base 是该品种自己的溢价中位数（台账攒够了才有），
    有它就用「中位数 + 偏离量」判定，没它就用绝对阈值兜底。
    """
    try:
        ev = evaluate(symbol, now, spot_prices, use_spot, fund_snap, show_long)
        if not ev["ok"]:
            if ev["reason"] == "数据不足":
                return f"- ⚪ **{symbol}** 数据不足", "", False, None
            label = f"{symbol} {FUND_NAMES[symbol]}" if symbol in FUND_NAMES else symbol
            return f"- ❌ **{label}** 分析失败", "", False, None

        # 指标、信号、长期趋势全在 evaluate 里算好，这里只取
        bar_date = ev["bar_date"]
        rsi = ev["rsi"]
        event = ev["event"]
        signal = ev["signal"]
        long_state = ev["long"]

        # 溢价只要取到就印，不再「不严重就不印」：不印的话读者无法区分
        # 「没有溢价数据」和「溢价正常」，而溢价是这个模型唯一还有证据支持的维度。
        # 阈值只决定加不加 ⚠——文字标签仍然是「净值日」，不是「此刻」。
        # 159941 的动态基准中位是 11.55%，所以 14.5% 在它自己的尺度上只是偏高而非异常；
        # 同期固定阈值（heavy 12%）会让它天天报⚠。
        prem = ev["prem"]
        prem_label = ev["prem_label"]
        prem_text = ""
        if prem is not None:
            prem_text = f"溢价 {prem:.1f}%"
            if prem_label:
                prem_text += f"({prem_label})"
            if prem_flag(prem, (prem_base or {}).get(symbol)) == "heavy":
                prem_text += "⚠"

        # Markdown 列表项：- 图标 **代码** 产品名 · 信号 · RSI · 长期 · 溢价 · 基准日
        # 名称紧跟代码、用空格隔开（不拿 · 隔，否则一行被切成太多块反而难读）
        # 当日标记直接用文字：全角＊在手机上渲染得太小看不清，半角 * 又会被 Markdown 当斜体
        rsi_text = f"RSI {rsi}" if rsi is not None else "RSI --"
        head = f"- {signal_icon(signal)} **{symbol}**"
        name = FUND_NAMES.get(symbol, "")
        if name:
            head += f" {name}"
        parts = [head, f"**{signal}**" if event else signal, rsi_text]
        if long_state == 1:
            parts.append("长期↑")
        elif long_state == 0:
            # 不加图标、不单独分组：上几轮试过置顶风险区，实际是噪音，这里只加粗
            parts.append("**长期↓**")
        if prem_text:
            parts.append(prem_text)
        suffix = bar_suffix(bar_date, now)
        if suffix:
            parts.append(suffix)

        meta = {
            "code": symbol,
            "signal": signal,
            "event": event,
            # 交叉发生在哪一天、距今天几根：台账要靠它去重，展示靠它标「N日前」
            "event_date": ev["event_date"],
            "event_days_ago": ev["event_days_ago"],
            "long": long_state,
            "close": ev["close"],
            "prem": prem,
            # 台账只存净值口径的 prem（基准是同一分布的中位数，混入口径就失效了）。
            # 目前两者是同一个值；将来若接入实时溢价，这里就要保持净值口径不变。
            "prem_nav": prem,
            "bar_date": bar_date,
            "source": ev.get("source", ""),
            "bars": ev.get("bars", 0),
            "dates": [str(d)[:10] for d in ev["df"]["日期"]],
            "closes": [float(v) for v in ev["df"]["收盘"]],
        }
        return " · ".join(parts), bar_date[5:], ev["patched"], meta
    except Exception as e:
        print(f"[警告] {symbol} 分析失败: {e}")
        label = f"{symbol} {FUND_NAMES[symbol]}" if symbol in FUND_NAMES else symbol
        return f"- ❌ **{label}** 分析失败", "", False, None


# ==================== 信号台账（实盘胜率） ====================
# 为什么需要落盘：Actions 每次跑都是全新容器，不存就永远是「第一次」
# 台账结构（紧凑键名，控制文件体积）：
#   {"version":1, "start":"摘要日", "days": {"2026-10-30": {"159941": {"sig":事件, "long":状态, "prem":溢价}}}}

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

    # prem_hist 同样要裁：回填一次就是 90 天 × 14 只，不裁会无限长下去
    hist = ledger.get("prem_hist")
    if isinstance(hist, dict) and hist:
        pruned = {}
        for code, series in hist.items():
            if not isinstance(series, dict):
                continue
            pruned[code] = {d: series[d] for d in sorted(series)[-PREM_HIST_KEEP:]}
        ledger["prem_hist"] = pruned

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
    """取每只最近 LEDGER_PREM_WINDOW 个溢价观测的中位数，样本不足 LEDGER_PREM_MIN 的不给基准

    为什么不信固定阈值：159941 的年度溢价中位数 2019 年是 -0.82%，2026 年是 +6.80%，
    差出七个多点。拿一个写死的数当尺子，过两年必然失效。

    为什么也不是全历史中位数（上一版就是这么写的，注释还写着「才跟得上漂移」，方向正好相反）：
    中位数不会被漂移带过去，它会被旧样本拽住。假设溢价中枢从 0% 抬到 8%，台账前 240 天
    记 0、最近 60 天记 8——全历史中位数仍是 0，于是「今天的 8%」被算成偏离基准 8 个点，
    天天报⚠；中枢下移时反过来，旧的高值把基准抬住，该报的漏报。
    漂移越慢危害越大，因为每一条单独看都像「今天有点高」，几乎不像异常。

    现在合并两个来源：
      ① prem_hist（backfill_prem 回填的历史溢价）——让基准立刻可用，不必空等 40 个交易日
      ② days 里的 prem（每天真实推送时算出来的）
    同一天两边都有时以 days 为准（它是当日实算的）；按日期排序后取最近 60 个值。
    """
    merged = {}
    for code, series in (ledger.get("prem_hist") or {}).items():
        if not isinstance(series, dict):
            continue
        bucket = merged.setdefault(code, {})
        for d, v in series.items():
            if v is None:
                continue
            try:
                bucket[str(d)] = float(v)
            except (TypeError, ValueError):
                continue

    for day_str, rec in (ledger.get("days") or {}).items():
        for code, item in rec.items():
            value = item.get("prem")
            if value is None:
                continue
            try:
                merged.setdefault(code, {})[day_str] = float(value)
            except (TypeError, ValueError):
                continue

    out = {}
    for code, series in merged.items():
        vals = [series[d] for d in sorted(series)][-LEDGER_PREM_WINDOW:]
        if len(vals) >= LEDGER_PREM_MIN:
            out[code] = statistics.median(vals)
    return out


def record_ledger(ledger, metas):
    """把每只标的的快照按它自己的 bar_date 写进台账，返回 (写入只数, 跳过名单)

    为什么不再用「全部标的都是今天的 K 线才记录」：旧条件是
        if record and bar_dates == {today_full}: record_ledger(...)
    14 只里只要有 1 只的最后一根不是今天（停牌、接口落后、走腾讯兜底拿到旧数据），
    当天 14 只全部不记录，而且只在 stdout 打一行。而台账是「实盘胜率」和
    「溢价动态基准」唯一的数据源——一只拖累全池，这是很差的爆炸半径。
    改成按各自的 bar_date 记：拿到哪天的数据就记在哪一天，哪只缺了单独列出来。

    sig 只记事件型（金叉/死叉），状态型空着——“持有/观望”每天都成立，
    存下来没有验证价值，只会把文件撑大。但 sig_date 必须一起存：
    加了事件回看窗口之后，同一次交叉会在连续 5 天里都被识别到，
    没有交叉日期就无法去重，胜率分母会虚高 5 倍。

    prem 存的是净值口径（meta["prem_nav"]）而不是展示用的那个：
    基准是把一个观测和它自己的历史比，口径必须固定；哪天盘中/盘后混着存，
    中位数就不再是同一个分布的中位数了。

    不记收盘价：前复权价会随分红除权整体重算，当年记下的价格和今天查到的
    同一天价格对不上，会凭空多出涨跌幅（verify_ledger 里也是只在同一条序列
    内部比）。既然不能用来对账，那它就是个死字段。
    """
    written = []
    skipped = []
    for meta in metas:
        if not meta:
            continue
        code = meta.get("code") or ""
        day_str = meta.get("bar_date") or ""
        if not code or not day_str:
            skipped.append(code or "?")
            continue
        rec = ledger["days"].setdefault(day_str, {}).setdefault(code, {})
        rec["sig"] = meta.get("event") or ""
        rec["sig_date"] = meta.get("event_date") or ""
        rec["long"] = meta.get("long")
        # 只在有新值时才覆盖 prem：取不到净值的那天不应该把之前算好的溢价抹掉
        if meta.get("prem_nav") is not None:
            rec["prem"] = meta["prem_nav"]
        written.append(code)
    if written and not ledger.get("start"):
        ledger["start"] = min(ledger["days"])
    return len(written), skipped


# 信号方向：金叉后面要涨才算对，死叉后面要跌才算对
LEDGER_DIRECTION = {"golden": ("金叉", 1), "death": ("死叉", -1)}


def verify_ledger(ledger, metas_by_code):
    """用台账里的交叉日期 + 今天的真实行情，算交叉后第 N 个交易日的涨跌

    为什么不用台账里记的收盘价：前复权价会随分红除权整体重算，
    拿当年记下的价格和今天查到的同一天价格比，会凭空多出涨跌幅。
    所以只在同一条序列内部比（交叉日收盘 vs N 个交易日后的收盘）。

    两道去重：
      ① 按 (代码, 交叉类型, 交叉日) 去重。加了回看窗口之后，同一次交叉会在连续
         5 天的推送里都被识别到、也被记 5 次；不去重的话分母直接虚高 5 倍。
      ② sig_date 缺失（老台账）时退回用记录日当交叉日。

    返回 {信号类型: {N: [每笔涨跌]}}，未满 N 日的不计入。
    """
    out = {}
    seen = set()
    for code, meta in metas_by_code.items():
        if not meta:
            continue
        idx = {d: i for i, d in enumerate(meta["dates"])}
        closes = meta["closes"]
        for day_str, rec in (ledger.get("days") or {}).items():
            item = rec.get(code)
            if not item or not item.get("sig"):
                continue
            sig_date = item.get("sig_date") or day_str
            key = (code, item["sig"], sig_date)
            if key in seen:
                continue
            i = idx.get(sig_date)
            if i is None:
                continue
            seen.add(key)
            for n in LEDGER_FORWARD:
                j = i + n
                if j >= len(closes) or closes[i] <= 0:
                    continue
                out.setdefault(item["sig"], {}).setdefault(n, []).append(
                    closes[j] / closes[i] - 1)
    return out


def calc_baseline(metas_by_code, window=250):
    """每只标的在统一窗口内的「无条件 N 日收益均值」，作为信号收益的对照

    没有这个对照，「5日 22/40 胜 均+0.4%」是无法解读的：如果同期这 14 只随便哪天买、
    持有 5 天的平均收益是 +1.2%，那这个金叉实际是跑输 0.8 个点，而文案读起来像「小赚」。
    信号赚的到底是信号本身还是市场 beta，只有减掉基准才看得出来。

    窗口统一取最近 250 个交易日：不同标的的序列长度实测从 400 根到 3346 根不等，
    不统一窗口的话基准本身就被长历史的早期行情带偏了。
    """
    per_n = {}
    for meta in metas_by_code.values():
        if not meta:
            continue
        closes = meta["closes"][-window:]
        for n in LEDGER_FORWARD:
            if len(closes) <= n:
                continue
            rets = [closes[i + n] / closes[i] - 1
                    for i in range(len(closes) - n) if closes[i] > 0]
            if rets:
                per_n.setdefault(n, []).append(sum(rets) / len(rets))
    return {n: sum(v) / len(v) for n, v in per_n.items() if v}


def count_signals(days, sig):
    """台账里某类交叉的真实次数（按 代码 + 交叉日 去重）

    不能直接数 sig 字段：回看窗口让同一次交叉连着记好几天，直接计数会把 1 次算成 5 次。
    """
    seen = set()
    for day_str, rec in days.items():
        for code, item in rec.items():
            if not item or item.get("sig") != sig:
                continue
            seen.add((code, item.get("sig_date") or day_str))
    return len(seen)


def backfill_prem(ledger, codes=None, days=PREM_HIST_KEEP):
    """用历史净值 + 不复权收盘，回填台账里缺失的溢价历史，返回成功只数

    为什么需要这个：calc_prem_baseline 要求每只标的攒够 LEDGER_PREM_MIN 个观测，
    而台账每天只贡献 1 个——意味着动态基准要等 8 周才首次生效，这段时间里
    一直在用注释里刚论证过「注定失效」的固定阈值。
    fund_open_fund_info_em 能一次拿到完整净值历史（ETF 与 LOF 都覆盖，实测）。
    所以这一步可以一次把 90 天补满。

    只写 prem_hist，不碰 days：days 是「真实推送记录」，塞进回填数据会让
    「自 X 月 X 日起累计 N 个交易日」变成一句谎话，而那句话是用来判断
    台账有没有在正常回写的。
    """
    codes = codes or ETF_LIST
    hist = ledger.setdefault("prem_hist", {})
    filled = 0
    for code in codes:
        navs = fetch_nav_history(code, days=days)
        if not navs:
            print(f"[回填] {code} 净值历史不可用，跳过")
            continue
        px = fetch_kline_tencent(code, count=days + 40, adjust=False)
        if px is None or px.empty:
            print(f"[回填] {code} 不复权日线不可用，跳过")
            continue
        close_map = {str(d)[:10]: float(v) for d, v in zip(px["日期"], px["收盘"])}
        series = {}
        for d, nav in navs.items():
            close = close_map.get(d)
            if not close or not nav or nav <= 0:
                continue
            series[d] = round((close / nav - 1) * 100, 2)
        if not series:
            print(f"[回填] {code} 净值与收盘没有重合日期，跳过")
            continue
        hist.setdefault(code, {}).update(series)
        filled += 1
        print(f"[回填] {code} 写入 {len(series)} 天溢价 "
              f"（{min(series.values()):+.2f}% ~ {max(series.values()):+.2f}%，"
              f"截至 {max(series)}）")
    return filled


def ensure_prem_history():
    """盘后写台账前，给还没有溢价基准的标的补一次历史，返回补成功的只数

    为什么不要求人手动跑 --backfill-prem：那份说明贴在 README 里，而真正会看
    README 的人本来就不会被这个问题困住。不补的话，动态基准要空等 40 个交易日，
    这 8 周里天天用的都是注释里刚证伪的固定阈值。

    为什么不会每跳都联网：开关就是「有没有基准」。建好之后 calc_prem_baseline
    直接返回，pending 为空，这里一次请求都不发。只有首次部署、新加标的、
    或净值历史被裁掉时才触发。
    """
    ledger = load_ledger()
    have = calc_prem_baseline(ledger)
    pending = [c for c in ETF_LIST if c not in have]
    if not pending:
        return 0
    print(f"[回填] {len(pending)} 只还没有溢价基准，补历史净值：{pending}")
    filled = backfill_prem(ledger, codes=pending)
    if filled:
        save_ledger(ledger)
        print(f"[回填] 已写入 {filled} 只的溢价历史")
    return filled


def ledger_staleness(ledger, metas_by_code):
    """台账最后一天落后最新行情几个交易日；判断不了时返回 None

    为什么用行情日期序列，而不是自然日或交易日历：
      ① dates 本身就是交易所日历——节假日没有 K 线，不必另外拉取；
      ② is_trade_day 拿不到日历时会退化成「周一至周五」，用它数天数会把长假算成正常；
      ③ 这条检查的全部意义是「发现数据没写进去」，它自己不能再依赖一个可能失败的请求。

    返回 None 的两种情况（都不告警）：台账为空；台账最后一天不在任何行情序列里
    （那是脏数据、不是陈旧，硬报一个天数反而是误导）。
    """
    last = max((ledger.get("days") or {}), default=None)
    if not last:
        return None
    # 取日期最长的那只当参照：它最可能覆盖台账里的最后一天
    ref = max((m["dates"] for m in metas_by_code.values()
               if m and m.get("dates")), key=len, default=None)
    if not ref or last not in ref:
        return None
    return len(ref) - 1 - ref.index(last)


def build_ledger_lines(ledger, stats, stale=None, baseline=None):
    """拼信号跟踪段落；该 N 日样本不足 LEDGER_MIN_SAMPLE 笔时只报笔数

    为什么要设门槛：原来「5日 1/1 胜 均+2.3%」和「5日 52/68 胜 均+0.4%」
    在微信里排版一模一样——都是一行里跟着「胜 均」。但 1 笔的胜率没有信息量，
    它就是那一天的涨跌，换个日期就能从 100% 翻到 0%，读的人却会当成「这套规则准」。
    门槛以下只报笔数（N 日 M 笔），凑够了才谈胜率和均值。

    为什么要给超额（baseline）：没有对照时「均+0.4%」无法解读——同期这批标的
    随便哪天买、持有 N 天的平均收益如果是 +1.2%，那这个信号实际是跑输 0.8 个点，
    而文案读起来像「小赚」。超额 =（信号后涨跌 − 同期无条件涨跌）× 信号方向，
    只有它才是「信号本身有没有信息」，均值里混着市场 beta。

    标题从「实盘验证」改成「信号跟踪」：后者不替这套规则背书。如果金叉真的
    没有预测力，这一节就应该稳定显示 50% 和负超额——那是验证结果，不是罪。
    叫「验证」会让人默认它已经通过了。

    stale 是台账落后最新行情几个交易日（见 ledger_staleness）。传 None 表示判断不了，
    不告警——宁可漏报，不要把「不知道」印成一条假警报。
    """
    days = ledger.get("days") or {}
    lines = ["### 🧪 信号跟踪", ""]
    if not days:
        lines.append("- 台账为空，盘后推送后开始累计（首日没有胜率可算）")
        lines.append("")
        return lines

    start = ledger.get("start") or min(days)
    lines.append(f"- 自 {start[5:]} 起累计 {len(days)} 个交易日")
    # 台账静默失败检查：workflow 里回写那步是 continue-on-error，重试失败就丢当天记录，
    # 连丢几天也看不出来，直到发现胜率分母对不上。落后 0~2 天都是常态
    # （盘中/盘后推送时台账本来就还是上一交易日的），超过才说明真丢了。
    if stale is not None and stale > LEDGER_STALE_BARS:
        lines.append(f"- ⚠️ 台账停在 {max(days)[5:]}，已落后 {stale} 个交易日"
                     "——回写台账那步可能失败了，胜率分母会偏小")

    has_any = False     # 有已结算样本，哪怕只有 1 笔（下面文案要说"样本不足"而不是"没有"）
    has_stat = False    # 有达到门槛的样本，才配叫胜率
    for sig, (name, direction) in LEDGER_DIRECTION.items():
        # 按 代码+交叉日 去重：回看窗口会让同一次交叉连续记录多天
        total = count_signals(days, sig)
        if not total:
            continue
        chunks = []
        for n in LEDGER_FORWARD:
            vals = (stats.get(sig) or {}).get(n) or []
            if not vals:
                chunks.append(f"{n}日 待满")
                continue
            has_any = True
            win = sum(1 for v in vals if v * direction > 0)
            # 门槛逐 N 判定：5 日攒够了不代表 20 日也攒够，不能一刀切
            if len(vals) < LEDGER_MIN_SAMPLE:
                chunks.append(f"{n}日 {len(vals)}笔")
                continue
            has_stat = True
            mean = sum(vals) / len(vals) * 100
            text = f"{n}日 {win}/{len(vals)} 胜 均{mean:+.1f}%"
            bench = (baseline or {}).get(n)
            if bench is not None:
                excess = (mean / 100 - bench) * direction * 100
                text += f" 超额{excess:+.1f}%"
            chunks.append(text)
        lines.append(f"- **{name}** 发出 {total} 次 · " + " · ".join(chunks))

    if not has_any:
        lines.append("- 暂无已结算样本，满 5 个交易日后出现胜率")
    elif not has_stat:
        lines.append(f"- 已结算样本不足 {LEDGER_MIN_SAMPLE} 笔，暂不出胜率"
                     "（样本少时一两天就能把胜率翻面）")
    else:
        lines.append(f"- 满 {LEDGER_MIN_SAMPLE} 笔才出胜率；超额 =（信号后涨跌 − 同期无条件涨跌）"
                     "× 信号方向，只有它才是信号自己的贡献")
    lines.append("- 这一节是记录，不是成绩单：金叉/死叉判方向已被证伪，"
                 "接近 50% 的胜率是预期内结果")
    lines.append("")
    return lines


# ==================== 消息拼装与推送 ====================
def build_message(now, slot, record=False):
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%m-%d")

    # 只拉一次全市场实时行情，所有 ETF 复用，避免逐只重复请求
    use_spot = should_apply_spot(slot, now)
    spot_prices = get_spot_prices(ETF_LIST) if use_spot else {}
    # 溢价率同理：一次拉全市场 1600+ 只，这里只取池子里那 14 只
    fund_snap = fetch_fund_snapshot(ETF_LIST)

    # 台账先读出来：溢价基准要从里面的历史溢价中位数算，攒够了就不再信固定阈值
    ledger = load_ledger()
    prem_base = calc_prem_baseline(ledger)
    if prem_base:
        print(f"[溢价] 已改用台账最近 {LEDGER_PREM_WINDOW} 日中位数作基准: " + "  ".join(
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
            # 当日数据有两条来路：历史接口自己收录了，或用实时价补了一根。
            # 对读者来说这区别没有意义（盘中都是瞬时值、盘后都是收盘价），
            # 正文只按 bar_date 判断「是不是今天」，补价只进运行日志。
            if bar_date == today:
                fresh_count += 1
                if patched:
                    patched_count += 1
            # 只有「本该能拿到当日却没拿到」才算异常；盘前不补价，拿到上一交易日数据是正常的
            if use_spot and bar_date and bar_date != today and not patched:
                stale.append(code)
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

    # 台账：先把当日快照落盘，再算跟踪，这样首日推送就能显示「累计 1 个交易日」
    # 按每只标的自己的 bar_date 记，不再要求全池就绪：旧条件
    # （bar_dates == {today}）在 14 只里只要有 1 只落后就导致全池当天不记录，
    # 而它只在 stdout 打一行，没有任何人能看见。
    if record and metas:
        written, skipped = record_ledger(ledger, metas)
        if written:
            save_ledger(ledger)
        print(f"[台账] 已记录 {written} 只")
        if skipped:
            print(f"[台账] {len(skipped)} 只没有基准日，本次未记录: {skipped}")

    metas_by_code = {m["code"]: m for m in metas}
    lines.extend(build_ledger_lines(
        ledger,
        verify_ledger(ledger, metas_by_code),
        ledger_staleness(ledger, metas_by_code),
        calc_baseline(metas_by_code),
    ))


    # 数据来源说明，让消息里能看出算的是哪一天的价
    # 说明也用列表项，理由同上：纯文本换行在 Markdown 里不生效
    lines.append("---")
    lines.append("")
    notes = []
    total = len(ETF_LIST)
    # 补价只留在运行日志里：排查数据源时有用，对读者没有信息量
    if patched_count:
        print(f"[数据] {patched_count}/{total} 只用实时价补了当日K线")
    if not fresh_count:
        # 历史接口和实时价都没给到今天的数据
        notes.append("⚠ 当日行情未取到，ETF 信号为上一交易日数据")
    else:
        # 判据是基准日是不是今天，不是有没有补价：
        # 历史接口（含腾讯兜底）本身就有当日K线时 patched_count=0，但数据是当天的，
        # 不能报「当日行情未取到」
        if bar_is_final(now):
            notes.append(f"ETF 信号已含今日收盘数据（{fresh_count}/{total} 只）")
        else:
            # 盘中这根K线还没收盘，MA/RSI 每几分钟就变一次。上一版对此一字不提，
            # 10:13 算出来的信号会被当成当天的定论。
            notes.append(f"今日盘中数据（{fresh_count}/{total} 只），未收盘："
                         "MA/RSI 会随价格变动")
        if stale:
            # 不区分“接口没给价”和“触发折算保护主动不补”，两种情况都去日志里看具体原因
            notes.append(f"⚠ 当日价未补齐：{name_list(stale)}（原因见运行日志）")
    notes.append(f"场外基金（{BOND_CODE}/{OTHER_FUND}）净值为 T+1，通常滞后一天")
    # 数据源分布：三条分支的窗口长度差很多（实测 400 / 601 / 3346 根），
    # 而对外文案原来统一写「数据来源：东方财富 / 腾讯」，读者无从判断某只到底走的哪条源。
    # 盘后排查「某只数字不对劲」时，这一行是第一手线索。
    src_line = source_summary([m.get("source", "") for m in metas if m])
    if src_line:
        notes.append(f"数据源：{src_line}")
    lines.extend(f"- {note}" for note in notes)
    lines.append("")

    # 尾注只说证据支持的话，且按证据强度分档——不能把 6 只样本的结论和 30 只样本的
    # 结论写成同一句话。原来那句「有证据的三件事」里，金叉证伪用了 30 只/22260 个观测，
    # 而 MA200 缓冲带只用了 6 只/742 个交易日、回撤从 19.1% 到 18.4%（0.7 个点），
    # 两者差一个量级，并列在一起等于把弱证据抬成强证据。
    # 必须带 "- " 列表前缀：上面约 170 行写过这条规矩——两个 append 之间只有一个 \n，
    # 在 Markdown 里是软换行，微信会把三句挤成一段。凑成一段就没人读完。
    lines.append("- 💡 证据较强的两件事：**溢价率** 只和该品种自己的历史中位比（不跨品种比）、"
                 "**长期↓** 当排除器用（↑ 内部排序≈噪声）")
    lines.append("- 💡 证据较弱的一件事：**MA200 缓冲带** 只在 6 只/742 个交易日上验过，"
                 "最大回撤 19.1%→18.4%（0.7pp）——方向可能有，幅度接近噪声，别当承诺")
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
    parser.add_argument("--slot", default=None, help="指定时段：mid / close / manual，留空按当前北京时间判定")
    parser.add_argument("--detect-slot", action="store_true", help="只输出当前时段，供 GitHub Actions 使用")
    parser.add_argument("--preview", action="store_true", help="只生成并打印消息，不调用 Server酱（不占每天 5 条额度）")
    parser.add_argument("--record", action="store_true",
                        help="强制把当日快照写入台账（默认只在盘后自动推送时写，预览不污染数据）")
    parser.add_argument("--backfill-prem", action="store_true",
                        help="用历史净值回填溢价历史（一次性，不推送），让动态基准立刻生效")
    args = parser.parse_args()

    now = beijing_now()

    # 回填是一个独立的一次性任务：它不取行情、不算指标、不推送，
    # 放在时段判定之前，否则会被窗口逻辑直接跳过。
    if args.backfill_prem:
        ledger = load_ledger()
        filled = backfill_prem(ledger)
        if not filled:
            print("[回填] 没有写入任何数据，台账保持不变")
            return 1
        save_ledger(ledger)
        base = calc_prem_baseline(ledger)
        print(f"\n[回填完成] {filled} 只；已建立动态基准的 {len(base)} 只"
              f"（门槛 {LEDGER_PREM_MIN} 个观测）:")
        for code, value in sorted(base.items()):
            print(f"  {code}: 中位 {value:+.2f}%  "
                  f"→ warn {value + PREMIUM_DEV_WARN:+.2f}% / "
                  f"heavy {value + PREMIUM_DEV_HEAVY:+.2f}%")
        missing = [c for c in ETF_LIST if c not in base]
        if missing:
            print(f"  仍未建立基准（样本不足或取不到净值）: {missing}")
        return 0

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
    # 回填必须在 build_message 之前：build_message 内部会用台账算溢价基准，
    # 先补上这一轮消息就能直接用新基准，不用等下一跳。
    if record:
        try:
            ensure_prem_history()
        except Exception as exc:   # 回填失败不能拖垮推送，它只是锦上添花
            print(f"[回填] 跳过（{exc}）")
    content = build_message(now, slot, record=record)
    print(content)
    print("-" * 40)

    # 标题会显示在微信通知栏，带上时间便于区分一天里的两条
    title = f"ETF信号助手·{slot_label(slot)}·{now.strftime('%m-%d %H:%M')}"
    if not send_serverchan(title, content, preview=args.preview):
        # 退出码非 0，让 Actions 变红，避免"静默失败"
        return 1

    print("预览完成（未发送）" if args.preview else "推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())

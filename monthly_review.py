#!/usr/bin/env python3
"""月度滚动体检：每条规则在最近 N 个月里，到底有没有可检测的超额

为什么单独一个脚本、不并进推送：推送回答「今天是什么状态」，体检回答「这套规则
最近还灵吗」。后者是统计问题，需要样本量和置信区间，混进每日正文会把一条消息
撑成两份文档，也让「今天该不该动手」被一个几个月才有一次的结论干扰。

为什么不是「学习」：这里不做任何拟合。规则、参数、判定式全部照搬线上那套
（long_state_series / cross_points / PREMIUM_HEAVY），只是把同一条规则在过去
N 个月的表现数出来，再减掉「同期同池随便哪天买」的基准。参数不动，所以不存在
过拟合到最近这段行情的问题；它只能告诉你「没检测到」「正向」「反向」。

为什么基准必须在同一窗口内重算：跨年比较会把牛熊差距当成规则能力。2024 年整年
上涨，任何条件都会显得「有效」。同期同池的无条件收益是这里唯一可信的对照。

判定阈值为什么是 3.0 而不是 2.0：一次体检同时检验 6 条规则（长期↑/↓、金叉、
死叉、溢价重、闸门），5% 双侧下 6 次里至少一次 |t|>2 的概率约 26%。按 Bonferroni
把单次阈值压到 0.05/6≈0.008，对应 |t|≈2.9，取整到 3.0。

为什么用「不重叠子样本」算 t：20 日收益相邻两天共享 19 天，直接拿全部日观测算
t 会把样本量虚高近 20 倍，t 值随之虚高。这里每 h 天取一个观测，宁可损失样本，
也不报一个假的显著性。
"""
import argparse
import os
import statistics
import sys
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import daily_etf_push as dp

FORWARD = (5, 10, 20)
VERDICT_H = 20            # 只对 20 日给结论：5/10 日噪声太大，月度看没有决策价值
MIN_SAMPLE = 12           # 不重叠样本少于此数只报笔数，不给结论
T_THRESHOLD = 3.0         # 6 条规则一起检验，详见文件头
REVIEW_DIR = "review"

RULES = [
    ("long_up", "长期↑"),
    ("long_dn", "长期↓"),
    ("golden", "金叉"),
    ("death", "死叉"),
    ("prem_heavy", f"溢价≥{dp.PREMIUM_HEAVY:g}%"),
    ("gate", "闸门(长期↓+溢价重)"),
]


def build_series(df):
    """把一只标的的日线整理成体检要用的四条序列，口径全部来自 daily_etf_push"""
    ev = dp.evaluate_df(df)
    if not ev:
        return None
    d = ev["df"]
    states = dp.long_state_series(d)
    return {
        "dates": d["日期"].astype(str).str[:10].tolist(),
        "closes": d["收盘"].astype(float).tolist(),
        # NaN 是 MA200 暖机段，转 None 免得后面和 0/1 比较时静默为假
        "long": [None if v != v else int(v) for v in states.tolist()],
        "crosses": dict(dp.cross_points(d)),
    }


def fetch_all(codes):
    """并发取日线；单只失败只跳过它，不让整份体检挂掉"""
    out, failed = {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(dp.fetch_data, c): c for c in codes}
        for fut, code in futures.items():
            try:
                series = build_series(fut.result())
            except Exception as e:               # 网络/解析问题都归到这里
                series = None
                print(f"[体检] {code} 取数失败: {type(e).__name__}: {e}")
            if series and len(series["closes"]) > VERDICT_H:
                out[code] = series
            else:
                failed.append(code)
    if failed:
        print(f"[体检] 跳过 {len(failed)} 只（无数据或历史太短）: {' '.join(failed)}")
    return out


def collect(series_by_code, prem_by_code, window_start):
    """构造逐日观测：每行 = (日期, 标的, 各规则的成立与否, 各周期的远期收益)

    溢价只在台账有记录的日子才算成立——没有当天的溢价就没有「溢价重」这个条件，
    用前一天的溢价顶替会把「今天反不反常」变成「昨天反不反常」。
    """
    rows = []
    for code, s in series_by_code.items():
        prems = prem_by_code.get(code) or {}
        states = s["long"]
        for i, day in enumerate(s["dates"]):
            if day < window_start:
                continue
            fwd = {}
            for h in FORWARD:
                j = i + h
                if j < len(s["closes"]) and s["closes"][i] > 0:
                    fwd[h] = s["closes"][j] / s["closes"][i] - 1
            if not fwd:
                continue
            state = states[i]
            prem = prems.get(day)
            rows.append({
                "code": code,
                "date": day,
                "pos": i,
                "fwd": fwd,
                "cond": {
                    "long_up": state == 1,
                    "long_dn": state == 0,
                    "golden": s["crosses"].get(day) == "golden",
                    "death": s["crosses"].get(day) == "death",
                    "prem_heavy": prem is not None and prem >= dp.PREMIUM_HEAVY,
                    "gate": state == 0 and prem is not None
                            and prem >= dp.PREMIUM_HEAVY,
                },
            })
    return rows


def base_means(rows):
    """同一窗口、同一批标的的无条件远期收益：随便哪天买，持有 h 天"""
    out = {}
    for h in FORWARD:
        vals = [r["fwd"][h] for r in rows if h in r["fwd"]]
        out[h] = statistics.fmean(vals) if vals else None
    return out


def t_stat(values):
    """不重叠子样本上的 t；样本太小时返回 None，不硬算"""
    if len(values) < 3:
        return None
    mu = statistics.fmean(values)
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    return mu / (sd / len(values) ** 0.5)


def nonoverlap(picked, h):
    """同一只标的内，挑出前后至少隔 h 个交易日的观测

    不能用「下标 % h == 0」：交叉落在哪一天是随机的，取模会把 29 次交叉筛成 1 次
    （第一版就是这么错的，金叉显示「有效样本 1」）。按标的逐个贪心取，才能既去掉
    重叠的远期窗口，又不把稀疏事件一起丢掉。
    """
    by_code = {}
    for r in picked:
        by_code.setdefault(r["code"], []).append(r)
    out = []
    for items in by_code.values():
        last = None
        for r in sorted(items, key=lambda x: x["pos"]):
            if last is None or r["pos"] - last >= h:
                out.append(r)
                last = r["pos"]
    return out


def evaluate_rule(rows, rule, h, baseline):
    """一条规则在一个周期上的表现：全样本均值 + 不重叠子样本的 t

    均值用全部观测（更稳），t 用子样本（更诚实）。两者样本量不同是故意的，
    报告里分开写 n 和 n_eff，避免把「600 个日观测」读成「600 个独立事件」。
    """
    picked = [r for r in rows if r["cond"][rule] and h in r["fwd"]]
    if not picked:
        return None
    rets = [r["fwd"][h] for r in picked]
    sub = [r["fwd"][h] - baseline for r in nonoverlap(picked, h)]
    return {
        "n": len(rets),
        "n_eff": len(sub),
        "mean": statistics.fmean(rets),
        "excess": statistics.fmean(rets) - baseline if baseline is not None else None,
        "t": t_stat(sub),
    }


def verdict(stat):
    """20 日的判定：只有过了样本和显著性两道门槛才给方向"""
    if stat is None or stat["n_eff"] < MIN_SAMPLE:
        return "样本不足"
    if stat["t"] is None:
        return "样本不足"
    if stat["t"] >= T_THRESHOLD:
        return "正向"
    if stat["t"] <= -T_THRESHOLD:
        return "反向"
    return "未检测到"


def plain_dir(x):
    return "多赚" if (x or 0) >= 0 else "少赚"


def plain_summary(stats, base, months):
    """给不想看数字的人的三四行话

    全部由同一份 stats 生成，不另算一遍：月报里的「人话」和「明细」必须同源，
    否则读者会看到两个口径。它只做翻译，不额外给建议——「本月不动参数」是流程规则，
    不是这里的判断。
    """
    out = []
    if base.get(VERDICT_H) is not None:
        out.append(f"- 这 {months} 个月，这 14 只随便哪天买、拿 20 天，平均 "
                   f"{base[VERDICT_H] * 100:+.2f}%。下面的「多赚/少赚」都是和它比。")

    judged = [(label, stats[rule][VERDICT_H]) for rule, label in RULES]
    passed = [(label, st) for label, st in judged
              if st is not None and verdict(st) in ("正向", "反向")]
    if passed:
        names = "、".join(f"{label}（{verdict(st)}）" for label, st in passed)
        out.append(f"- **{len(passed)} 条规则过了「不像巧合」这条线**：{names}。"
                   "单月不算数，要连续两个月同方向才考虑动参数。")
    else:
        out.append(f"- **{len(RULES)} 条规则没有一条能证明自己有优势**，"
                   "全都过不了「不像巧合」这条线。这不是坏消息："
                   "「分不出来」和「没用」是两件事。")

    weak = [(label, st) for label, st in judged
            if st is not None and verdict(st) == "样本不足"]
    if weak:
        label, st = max(weak, key=lambda x: abs(x[1]["excess"] or 0))
        out.append(f"- 数字上最扎眼的是「{label}」：比乱买{plain_dir(st['excess'])}"
                   f" {abs(st['excess']) * 100:.1f} 个百分点，但独立样本只有 "
                   f"{st['n_eff']} 个，先记着，别当结论。")

    out.append("- **本月不动参数**，继续照旧：长期↓ 当排除器、溢价当成本、金叉死叉当标记。"
               if not passed else
               "- **本月也先不动参数**：等第二个月同方向再说。")
    return out


def pp(x):
    return "—" if x is None else f"{x * 100:+.2f}pp"


def build_report(series_by_code, prem_by_code, ledger, months, prem_days, now):
    window_start = (now - timedelta(days=int(months * 30.4))).strftime("%Y-%m-%d")
    rows = collect(series_by_code, prem_by_code, window_start)
    if not rows:
        return "**体检失败**：窗口内没有任何可用观测（取数全部失败？）", None

    base = base_means(rows)
    stats = {rule: {h: evaluate_rule(rows, rule, h, base[h]) for h in FORWARD}
             for rule, _ in RULES}

    dates = sorted({r["date"] for r in rows})
    data_end = max(s["dates"][-1] for s in series_by_code.values())
    interval = (f"**区间 {dates[0]} ~ {dates[-1]}**（{len(dates)} 个可结算交易日；"
                f"行情已到 {data_end}，最后 {VERDICT_H} 天的 20 日收益还没走完，故不计入）")
    summary = plain_summary(stats, base, months)

    lines = [
        f"# 滚动体检 · {now:%Y-%m}",
        "",
        f"> 只做一件事：把线上跑了 {months} 个月的规则，拿回去和「同期乱买」比一比。"
        "不改参数，也不预测明天。",
        "",
        interval,
        "",
        "### 🗣 一句话",
        "",
        *summary,
        "",
        "### 📋 每条规则（20 日后卖出）",
        "",
    ]
    for rule, label in RULES:
        st = stats[rule][VERDICT_H]
        if st is None:
            lines.append(f"- **{label}** · 这段时间没出现过，没有数据")
            continue
        tail = f"比乱买{plain_dir(st['excess'])} {abs(st['excess']) * 100:.1f} 个百分点"
        if st["t"] is not None:
            tail += f"，可信度 {st['t']:.1f}"
        lines.append(f"- **{label}** · {verdict(st)} · {tail}（独立样本 {st['n_eff']} 个）")

    lines += [
        "",
        "### 📊 明细",
        "",
        "| 规则 | 5日后 | 10日后 | 20日后 | 可信度 | 独立样本 | 总天数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for rule, label in RULES:
        cells = []
        for h in (5, 10, VERDICT_H):
            st = stats[rule][h]
            cells.append(pp(st["excess"]) if st else "—")
        st = stats[rule][VERDICT_H]
        cells.append(f"{st['t']:.1f}" if st and st["t"] is not None else "—")
        cells.append(str(st["n_eff"]) if st else "0")
        cells.append(str(st["n"]) if st else "0")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    base_line = " · ".join(f"{h}日 {base[h] * 100:+.2f}%" for h in FORWARD
                           if base[h] is not None)
    lines += [
        "",
        f"**乱买基准**（同期这 14 只随便哪天买）：{base_line}。"
        "表里所有数字都是减掉它之后的结果。",
        "",
        "### 🧭 怎么读",
        "",
        "- **「比乱买多赚 X 个百分点」**：正数是这套规则挑中的日子，比同期随便买好；"
        "负数是更差。基准本身是跌的（20 日 −0.28%），所以「少亏」也算正超额。",
        "- **「可信度」**：这个差距有多不像巧合。≥3 才算数；1 附近的意思是"
        "「这个月这样、下个月可能反过来」。",
        "- **「独立样本」**：连着 20 天算同一个信号，只记 1 个样本。所以总天数有一千多、"
        "独立样本只有几十个，这是正常的。",
        "- **「样本不足」** 是字面意思：数太少，先不下结论。它不等于「没效果」。",
        "- **正向也不自动改参数**：连续两个月同向才动，一次只动一条。单月翻面多半是行情换了。",
        f"- 溢价两条规则只回看了 {prem_days} 天（台账只保留 90 天，月报临时补），"
        "窗口比另外几条短。",
        "- 统计口径（Bonferroni 门槛、不重叠子样本）写在 `monthly_review.py` 文件头。",
    ]

    coverage = cross_coverage(ledger, series_by_code, window_start)
    if coverage:
        lines += ["", "### 🧾 落地覆盖", "", coverage]
    # 微信里只发「区间 + 一句话」：明细表在手机上会挤成一团，详细版看仓库
    brief = "\n".join([interval, "", "### 🗣 一句话", "", *summary])
    return "\n".join(lines), brief


def cross_coverage(ledger, series_by_code, window_start):
    """重构出的交叉次数 vs 台账真正记下的次数：差得越多，推送漏发越多

    两边必须用同一个起点。台账里的 sig_date 可能早于台账首日（事件回看窗口会把
    前几天发生的交叉记进今天），不统一口径就会出现「记录 3 次、重构 1 次、
    覆盖 300%」这种读不懂的数——第一版就是这么错的。
    """
    days = ledger.get("days") or {}
    if not days:
        return ""
    start = max(window_start, min(days))
    rebuilt = {d for s in series_by_code.values() for d in s["crosses"] if d >= start}
    recorded = set()
    for day_str, rec in days.items():
        for code, item in rec.items():
            sig_date = (item or {}).get("sig_date") or day_str
            if item and item.get("sig") and sig_date >= start:
                recorded.add((code, item["sig"], sig_date))
    if not rebuilt and not recorded:
        return ""
    if len(rebuilt) < 10:
        return (f"- {start} 起重构出 {len(rebuilt)} 次交叉、台账记录 {len(recorded)} 次。"
                "样本还太少，暂不评估覆盖率。")
    if len(recorded) > len(rebuilt):
        return (f"- {start} 起重构出 {len(rebuilt)} 次交叉，台账记录 {len(recorded)} 次。"
                "台账更多通常来自事件回看（交叉日早于记录日）或复权口径差异，"
                "不一定是漏记。")
    cover = len(recorded) / len(rebuilt) * 100
    tail = ("缺口来自「那天没发出去」，不是代码漏记。" if cover < 100 else "")
    return (f"- {start} 起重构出 {len(rebuilt)} 次交叉，台账记录 {len(recorded)} 次，"
            f"覆盖 {cover:.0f}%。{tail}")


def review_path(now):
    return os.path.join(REVIEW_DIR, f"review-{now:%Y-%m}.md")


def main():
    parser = argparse.ArgumentParser(description="月度滚动体检")
    parser.add_argument("--months", type=int, default=12, help="回看月数，默认 12")
    parser.add_argument("--save", action="store_true", help=f"写入 {REVIEW_DIR}/")
    parser.add_argument("--notify", action="store_true", help="经 Server酱 推一条")
    parser.add_argument("--if-due", action="store_true",
                        help="当月报告已存在就秒退（给定时任务用）")
    args = parser.parse_args()

    now = dp.beijing_now()
    path = review_path(now)
    # 先判存在再取数：一个月里除第一次外，这一步应当是零网络请求
    if args.if_due and os.path.exists(path):
        print(f"[体检] {path} 已存在，本月不再重复生成")
        return 0

    print(f"开始滚动体检（{now:%Y-%m-%d %H:%M}，回看 {args.months} 个月）")
    series_by_code = fetch_all(dp.ETF_LIST)
    if not series_by_code:
        print("[体检] 没有一只取到数据，放弃")
        return 1

    ledger = dp.load_ledger()
    # 台账里只留 90 天溢价，12 个月的体检会把溢价两条规则缩成半份样本。
    # 这里往内存里的台账副本回填（backfill_prem 只改入参），不写回文件——
    # 体检不该改动线上台账，那也是 ensure_prem_history 的职责。
    prem_days = min(args.months * 21, 400)
    print(f"[体检] 回填 {prem_days} 天溢价历史（仅本次体检使用，不写回台账）")
    filled = dp.backfill_prem(ledger, days=prem_days)
    print(f"[体检] 溢价历史可用 {filled}/{len(dp.ETF_LIST)} 只")
    prem_by_code = dp.prem_history(ledger)
    report, brief = build_report(series_by_code, prem_by_code, ledger,
                                 args.months, prem_days, now)
    print(report)

    if args.save:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"[体检] 已写入 {path}")

    if args.notify:
        body = brief if brief else report
        title = f"ETF信号助手·月度体检·{now:%Y-%m}"
        if not dp.send_serverchan(title, body):
            print("[体检] 推送失败")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

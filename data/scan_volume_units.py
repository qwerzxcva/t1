#!/usr/bin/env python3
"""scan_volume_units.py — 只读扫描 day_qfq 分片的成交量量纲（手 / 股）混用问题。

背景: bulk 下 K 线 vol 的历史口径是「手」。补数脚本的新浪回退路径曾把「股」直接
写进分片（量级是手的 100 倍），因此 2026-10-14 之后追加的尾部行可能与历史段不一致。
本工具只做扫描 + 干跑，默认绝不写数据；`--apply` 也只写 `bulk/<分片>.fixed`。

判据:
  相邻两根 vol 比值落在 [60,160] 或 [1/160,1/60]，且价格连续（|涨跌| <= 25%）
  -> 判为该序列发生了「手<->股」切换。
  分片内某票的首段若没有同分片前序行，则用「更早日期锚点」或「该票基准量级中位数」
  做跨分片判定（高/中置信，中置信只进报告，不参与 --apply）。

用法:
  python3 scan_volume_units.py                        # 全量扫描 + 出报告（干跑）
  python3 scan_volume_units.py --code sh600000        # 单票判定过程
  python3 scan_volume_units.py --code sh600000 --dry-run
  python3 scan_volume_units.py --apply                # 写 bulk/<分片>.fixed（不改原文件）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import deque
from datetime import date as _date
from datetime import datetime

SHARD_RE = re.compile(r"^day_qfq_(\d+)\.csv$")
HASH_RE = re.compile(r"^day_qfq_0\d\.csv$")   # 00~09: 按代码哈希分片，实测 vol 为「手」
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BAND_LO, BAND_HI = 60.0, 160.0          # 手<->股 的合法倍数区间
MAX_PRICE_MOVE = 0.25                   # 价格连续性上限
TAIL_N = 64                             # 每 (code, shard) 尾部样本量，用于基准量级
CONFLICT_CAP = 2000                     # 冲突明细上限
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BULK = os.path.join(HERE, "bulk")
DEFAULT_REPORTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "reports")
HAND, SHARES = "hand", "shares"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fmt_vol(v: float) -> str:
    """紧凑地显示量级，同时保留精确值含义。"""
    if v >= 1e8:
        return f"{v:.3e}"
    if v >= 1e5:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fmt_ratio(r: float) -> str:
    return f"{r:.1f}x" if r >= 1 else f"1/{1.0 / r:.1f}x"


def median_or_none(values):
    vals = [v for v in values if v > 0]
    return statistics.median(vals) if vals else None


class Series:
    """单个 (code, shard) 的时间序列累积状态，流式处理，不保留全部行。"""

    __slots__ = (
        "code", "shard", "rows", "first_date", "first_vol", "first_close",
        "last_date", "last_vol", "last_close", "prev_date", "prev_vol", "prev_close",
        "tail", "seg_start", "seg_rows", "seg_sum", "segments", "switches",
        "non_monotonic", "first_unit",
    )

    def __init__(self, code: str, shard: str, d: str, c: float, v: float) -> None:
        self.code = code
        self.shard = shard
        self.rows = 0
        self.first_date, self.first_vol, self.first_close = d, v, c
        self.last_date, self.last_vol, self.last_close = d, v, c
        self.prev_date, self.prev_vol, self.prev_close = d, v, c
        self.tail = deque(maxlen=TAIL_N)
        self.seg_start = d
        self.seg_rows = 0
        self.seg_sum = 0.0
        self.segments: list[dict] = []
        self.switches: list[dict] = []
        self.non_monotonic = 0
        self.first_unit = HAND

    def add(self, d: str, c: float, v: float) -> None:
        if self.rows:
            if d > self.prev_date:
                if self.prev_vol > 0 and v > 0:
                    ratio = v / self.prev_vol
                    price_move = abs(c / self.prev_close - 1.0) if self.prev_close > 0 else None
                    hit = (BAND_LO <= ratio <= BAND_HI) or (1.0 / BAND_HI <= ratio <= 1.0 / BAND_LO)
                    if hit and price_move is not None and price_move <= MAX_PRICE_MOVE:
                        self.switches.append({
                            "date": d,
                            "prev_date": self.prev_date,
                            "before_vol": self.prev_vol,
                            "after_vol": v,
                            "ratio": ratio,
                            "price_change": price_move,
                            "gap_days": _gap_days(self.prev_date, d),
                        })
                        # 本行属于新段，先结旧段再计入（漏这一步会把新行算到旧段）
                        self._close_segment(self.prev_date)
                        self.seg_start = d
                self.prev_date, self.prev_vol, self.prev_close = d, v, c
            else:
                # 同票在分片内乱序/回填：不做相邻判定，避免拿错邻居
                self.non_monotonic += 1
        self.rows += 1
        self.seg_rows += 1
        self.seg_sum += v
        self.tail.append(v)
        if d >= self.last_date:
            self.last_date, self.last_vol, self.last_close = d, v, c

    def _close_segment(self, end_date: str) -> None:
        self.segments.append({
            "start": self.seg_start,
            "end": end_date,
            "rows": self.seg_rows,
            "mean_vol": self.seg_sum / self.seg_rows if self.seg_rows else 0.0,
        })
        self.seg_rows = 0
        self.seg_sum = 0.0

    def finish(self) -> None:
        if self.seg_rows:
            self._close_segment(self.prev_date)

    @property
    def baseline(self) -> float | None:
        return median_or_none(self.tail)


def _gap_days(a: str, b: str) -> int:
    try:
        return (_date.fromisoformat(b) - _date.fromisoformat(a)).days
    except ValueError:
        return -1


def shard_files(bulk: str) -> list[tuple[int, str]]:
    out = []
    for name in os.listdir(bulk):
        m = SHARD_RE.match(name)
        if m:
            out.append((int(m.group(1)), os.path.join(bulk, name)))
    out.sort()
    return out


def scan(bulk: str, code_filter: str | None, since: str) -> tuple[dict, dict, int, int]:
    """单次流式扫描全部数值分片。

    返回 (series_state, multisource, skipped_rows, total_rows)。
    series_state: {(code, shard): Series}
    multisource:  {code: {date: (shard, vol) | [(shard, vol), ...]}}（仅 since 之后的日期）
    """
    state: dict[tuple[str, str], Series] = {}
    multisource: dict[str, dict[str, object]] = {}
    skipped = total = 0
    shards = shard_files(bulk)
    for _, path in shards:
        shard = os.path.basename(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line or line[0] == "#":
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) != 7:
                    skipped += 1
                    continue
                code, d = parts[0], parts[1]
                if not DATE_RE.match(d):
                    skipped += 1
                    continue
                if code_filter is not None and code != code_filter:
                    continue
                try:
                    close = float(parts[3])
                    vol = float(parts[6])
                except ValueError:
                    skipped += 1
                    continue
                total += 1
                key = (code, shard)
                s = state.get(key)
                if s is None:
                    state[key] = Series(code, shard, d, close, vol)
                    state[key].add(d, close, vol)
                else:
                    s.add(d, close, vol)
                if d >= since:
                    bucket = multisource.setdefault(code, {})
                    cur = bucket.get(d)
                    if cur is None:
                        bucket[d] = (shard, vol)
                    elif isinstance(cur, tuple):
                        if cur[1] > 0 and vol > 0 and abs(vol / cur[1] - 1.0) > 0.001:
                            bucket[d] = [cur, (shard, vol)]
                    else:
                        bucket[d].append((shard, vol))
    for s in state.values():
        s.finish()
    return state, multisource, skipped, total


def assign_units(state: dict, scale: dict) -> None:
    """给每段打上 hand / shares 标签。

    首段单位: 序列内部有切换点时按「历史段=手」基线（切换点由相邻行判定）；
    无内部切换点时采用跨分片量级判定结果（scale），无法判定则按手处理。
    """
    for key, s in state.items():
        rec = scale.get(key)
        unit = HAND
        if not s.switches and rec and rec.get("unit") == SHARES \
                and rec.get("confidence") in ("high", "medium"):
            unit = SHARES
        s.first_unit = unit
        units = []
        for seg in s.segments:
            units.append(unit)
            if seg is not s.segments[-1]:
                unit = SHARES if unit == HAND else HAND
        for seg, u in zip(s.segments, units):
            seg["unit"] = u


def analyze_scale(state: dict[str, Series]) -> dict:
    """跨分片量级比对：每 (code, shard) 序列的中位量级 对 该票手口径基准的倍数。

    手口径基准取 hash 分片（day_qfq_00~09）的尾部中位数——实测茅台/浦发在该族分片
    为「手」（30k / 600k 量级，若是股则与流通盘矛盾）；20/30/60/68 前缀分片是另一次
    导入，整段为「股」。这两族的差异不代表数据错，但同一票在跨族时倍数为 ~100x。
    """
    by_code: dict[str, list[Series]] = {}
    for s in state.values():
        by_code.setdefault(s.code, []).append(s)
    out: dict[tuple[str, str], dict] = {}
    for code, series in by_code.items():
        hash_series = sorted(((s.baseline, s.rows) for s in series
                              if HASH_RE.match(s.shard) and s.baseline), key=lambda x: x[0])
        ref = None
        if hash_series:
            total_w = sum(w for _, w in hash_series)
            acc = 0
            for v, w in hash_series:
                acc += w
                if acc >= total_w / 2.0:
                    ref = v
                    break
        for s in series:
            med = s.baseline
            if not med:
                continue
            rec = {
                "shard": s.shard,
                "family": "hash" if HASH_RE.match(s.shard) else "prefix",
                "median_vol": med,
                "rows": s.rows,
                "first_date": s.first_date,
                "last_date": s.last_date,
                "has_switch": bool(s.switches),
                "ratio_vs_hash_ref": None,
                "unit": "unknown",
                "confidence": None,
            }
            if ref and ref > 0:
                r = med / ref
                rec["ratio_vs_hash_ref"] = r
                if 1.0 / 3.0 <= r <= 3.0:
                    rec["unit"], rec["confidence"] = HAND, "high"
                elif BAND_LO <= r <= BAND_HI:
                    rec["unit"], rec["confidence"] = SHARES, "high"
                elif 1.0 / BAND_HI <= r <= 1.0 / BAND_LO:
                    rec["unit"], rec["confidence"] = HAND, "high"
                elif 30.0 <= r <= 300.0:
                    rec["unit"], rec["confidence"] = SHARES, "medium"
                elif 1.0 / 300.0 <= r <= 1.0 / 30.0:
                    rec["unit"], rec["confidence"] = HAND, "medium"
                else:
                    rec["unit"] = "ambiguous"
            out[(code, s.shard)] = rec
    return out


def annotate_confidence(state: dict) -> None:
    """给每个切换点标注「是否持续」：后面那段的量级是否稳住（还是单根捅头）。

    switches[i] 起始于 segments[i+1]（每次切换都会关掉当前段并开新段）。
    """
    for s in state.values():
        for i, sw in enumerate(s.switches):
            nxt = s.segments[i + 1] if i + 1 < len(s.segments) else None
            sustained = False
            rows_after = 0
            if nxt is not None:
                rows_after = nxt["rows"]
                sustained = rows_after >= 2 and \
                    sw["after_vol"] / 3.0 <= nxt["mean_vol"] <= sw["after_vol"] * 3.0
            small = min(sw["before_vol"], sw["after_vol"]) < 100.0
            sw["after_segment_rows"] = rows_after
            sw["confidence"] = "low" if (small or not sustained) else "high"


def collect_conflicts(multisource: dict) -> tuple[list, list, int]:
    unit_conf, value_conf, dup = [], [], 0
    for code in sorted(multisource):
        for d in sorted(multisource[code]):
            vals = multisource[code][d]
            if not isinstance(vals, list):
                continue
            nums = [v for _, v in vals if v > 0]
            if len(nums) < 2:
                dup += 1
                continue
            ratio = max(nums) / min(nums)
            rec = {
                "code": code,
                "date": d,
                "values": [{"shard": sh, "vol": v} for sh, v in vals],
                "ratio": ratio,
            }
            if ratio >= BAND_LO:
                if len(unit_conf) < CONFLICT_CAP:
                    unit_conf.append(rec)
            elif ratio > 1.03:
                if len(value_conf) < CONFLICT_CAP:
                    value_conf.append(rec)
            else:
                dup += 1
    return unit_conf, value_conf, dup


def build_report(state, scale, conflicts, meta) -> dict:
    unit_conf, value_conf, dup = conflicts
    by_code: dict[str, dict] = {}
    by_shard: dict[str, dict] = {}
    for (code, shard), s in state.items():
        entry = by_code.setdefault(code, {"sources": [], "switch_points": [],
                                          "whole_series_shares": None})
        rec = scale.get((code, shard))
        src = {
            "shard": shard,
            "rows": s.rows,
            "first_date": s.first_date,
            "last_date": s.last_date,
            "first_unit": getattr(s, "first_unit", HAND),
            "segments": len(s.segments),
            "non_monotonic": s.non_monotonic,
            "median_vol": rec["median_vol"] if rec else None,
            "family": rec["family"] if rec else None,
            "ratio_vs_hash_ref": rec["ratio_vs_hash_ref"] if rec else None,
            "unit_guess": rec["unit"] if rec else "unknown",
        }
        entry["sources"].append(src)
        for sw in s.switches:
            item = dict(sw)
            item["shard"] = shard
            item["kind"] = "hand->shares" if sw["ratio"] >= 1 else "shares->hand"
            entry["switch_points"].append(item)
            st = by_shard.setdefault(shard, {"switch_points": 0, "switch_high": 0,
                                             "stocks": set(), "whole_series": 0})
            st["switch_points"] += 1
            if sw.get("confidence") == "high":
                st["switch_high"] += 1
            st["stocks"].add(code)
        if rec and not s.switches and rec["unit"] == SHARES \
                and rec["confidence"] in ("high", "medium"):
            entry["whole_series_shares"] = {
                "shard": shard,
                "rows": s.rows,
                "first_date": s.first_date,
                "last_date": s.last_date,
                "median_vol": rec["median_vol"],
                "ratio_vs_hash_ref": rec["ratio_vs_hash_ref"],
                "confidence": rec["confidence"],
            }
            st = by_shard.setdefault(shard, {"switch_points": 0, "switch_high": 0,
                                             "stocks": set(), "whole_series": 0})
            st["whole_series"] += 1
            st["stocks"].add(code)
    affected = []
    for code, entry in by_code.items():
        entry["sources"].sort(key=lambda x: x["shard"])
        entry["switch_points"].sort(key=lambda x: x["date"])
        entry["affected"] = bool(entry["switch_points"]) or bool(entry["whole_series_shares"])
        if entry["affected"]:
            affected.append(code)
    affected.sort()
    shard_stats = {}
    for shard in sorted(by_shard):
        shard_stats[shard] = {
            "switch_points": by_shard[shard]["switch_points"],
            "switch_high": by_shard[shard]["switch_high"],
            "whole_series_shares": by_shard[shard]["whole_series"],
            "stocks": len(by_shard[shard]["stocks"]),
        }
    total_switches = sum(len(e["switch_points"]) for e in by_code.values())
    total_high = sum(1 for e in by_code.values()
                     for sw in e["switch_points"] if sw.get("confidence") == "high")
    whole_high = sum(1 for e in by_code.values()
                     if e["whole_series_shares"] and e["whole_series_shares"]["confidence"] == "high")
    whole_med = sum(1 for e in by_code.values()
                    if e["whole_series_shares"] and e["whole_series_shares"]["confidence"] == "medium")
    switch_codes = sorted(c for c, e in by_code.items() if e["switch_points"])
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bulk_dir": meta["bulk"],
        "params": {
            "band": [BAND_LO, BAND_HI],
            "max_price_move": MAX_PRICE_MOVE,
            "since_for_multisource": meta["since"],
            "code_filter": meta["code"],
        },
        "runtime_sec": round(meta["elapsed"], 2),
        "summary": {
            "rows_scanned": meta["rows"],
            "skipped_rows": meta["skipped"],
            "stocks_scanned": len(by_code),
            "stocks_affected": len(affected),
            "switch_points_total": total_switches,
            "switch_points_high": total_high,
            "switch_stocks": len(switch_codes),
            "whole_series_shares_high": whole_high,
            "whole_series_shares_medium": whole_med,
            "unit_conflicts": len(unit_conf),
            "value_conflicts": len(value_conf),
            "multi_source_same_value": dup,
            "shards": shard_stats,
        },
        "affected_codes": affected,
        "stocks": by_code,
        "conflicts": {"unit": unit_conf, "value": value_conf},
    }


def write_json(report: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def write_md(report: dict, path: str, top_n: int) -> None:
    s = report["summary"]
    lines = [
        f"# 成交量量纲扫描报告（vol: 手 / 股混用）",
        "",
        f"- 生成时间: {report['generated_at']}",
        f"- 扫描目录: `{report['bulk_dir']}`",
        f"- 扫描耗时: {report['runtime_sec']} 秒",
        f"- 判据: 相邻 vol 比值 ∈ [{BAND_LO:.0f}, {BAND_HI:.0f}] 或倒数区间，且 |涨跌| ≤ {MAX_PRICE_MOVE:.0%}",
        "",
        "## 汇总",
        "",
        f"| 指标 | 值 |",
        "|---|---|",
        f"| 扫描行数 | {s['rows_scanned']} |",
        f"| 跳过行数（注释/坏行） | {s['skipped_rows']} |",
        f"| 覆盖票数 | {s['stocks_scanned']} |",
        f"| 受影响票数 | {s['stocks_affected']} |",
        f"| 片内切换点总数 | {s['switch_points_total']}（高置信 {s['switch_points_high']}，{s['switch_stocks']} 票） |",
        f"| 整段为股的分片序列（高置信） | {s['whole_series_shares_high']} |",
        f"| 整段为股的分片序列（中置信,仅复核） | {s['whole_series_shares_medium']} |",
        f"| 同票同日量纲冲突(≥{BAND_LO:.0f}x) | {s['unit_conflicts']} |",
        f"| 同票同日数值冲突(>3%) | {s['value_conflicts']} |",
        "",
        "## 按分片统计",
        "",
        "| 分片 | 片内切换点 | 其中高置信 | 整段为股 | 涉及票数 |",
        "|---|---|---|---|---|",
    ]
    for shard, st in s["shards"].items():
        lines.append(f"| {shard} | {st['switch_points']} | {st['switch_high']} "
                     f"| {st['whole_series_shares']} | {st['stocks']} |")
    lines += ["", f"## Top {top_n} 受影响票（按高置信片内切换点数排序）", "",
              "| # | 代码 | 高置信/总切换点 | 切换日期 | 前量级 | 后量级 | 倍数 | 涨跌 | 分片 |",
              "|---|---|---|---|---|---|---|---|---|"]
    def _key(c: str):
        sws = report["stocks"][c]["switch_points"]
        return (-sum(1 for x in sws if x.get("confidence") == "high"), -len(sws), c)
    order = sorted(report["affected_codes"], key=_key)
    for i, code in enumerate(order[:top_n], 1):
        entry = report["stocks"][code]
        if entry["switch_points"]:
            sws = [x for x in entry["switch_points"] if x.get("confidence") == "high"] \
                or entry["switch_points"]
            sw = sws[-1]
            lines.append(
                f"| {i} | {code} | "
                f"{sum(1 for x in entry['switch_points'] if x.get('confidence') == 'high')}"
                f"/{len(entry['switch_points'])} | {sw['date']} "
                f"| {fmt_vol(sw['before_vol'])} | {fmt_vol(sw['after_vol'])} "
                f"| {fmt_ratio(sw['ratio'])} | {sw['price_change']:.1%} | {sw['shard']} |")
        else:
            ws = entry["whole_series_shares"]
            lines.append(
                f"| {i} | {code} | 0(整段股) | {ws['first_date']}~{ws['last_date']} | - "
                f"{fmt_vol(ws['median_vol'])} | {fmt_ratio(ws['ratio_vs_hash_ref'])} "
                f"| - | {ws['shard']} |")
    med = [c for c in report["affected_codes"]
           if report["stocks"][c]["whole_series_shares"]
           and report["stocks"][c]["whole_series_shares"]["confidence"] == "medium"]
    if med:
        lines += ["", "## 中置信「整段为股」序列（需人工复核，不参与 --apply）", "",
                  "| 代码 | 分片 | 行数 | 中位量级 | 对哈希手基准倍数 |",
                  "|---|---|---|---|---|"]
        for code in med[:30]:
            ws = report["stocks"][code]["whole_series_shares"]
            lines.append(f"| {code} | {ws['shard']} | {ws['rows']} | "
                         f"{fmt_vol(ws['median_vol'])} | {ws['ratio_vs_hash_ref']:.0f}x |")
    lines += ["", "## 2026-10 之后的高置信量纲切换（按日期）", ""]
    oct_stats: dict[str, dict[str, int]] = {}
    oct_stocks: dict[str, set] = {}
    for code, entry in report["stocks"].items():
        for sw in entry["switch_points"]:
            if sw.get("confidence") != "high" or sw["date"] < "2026-10-01":
                continue
            st = oct_stats.setdefault(sw["date"], {"hand->shares": 0, "shares->hand": 0})
            st[sw["kind"]] += 1
            oct_stocks.setdefault(sw["date"], set()).add(code)
    if oct_stats:
        lines += ["| 日期 | 手→股 | 股→手 | 涉及票数 |", "|---|---|---|---|"]
        for d in sorted(oct_stats):
            st = oct_stats[d]
            lines.append(f"| {d} | {st['hand->shares']} | {st['shares->hand']} "
                         f"| {len(oct_stocks[d])} |")
    else:
        lines.append("无。")
    if report["conflicts"]["unit"]:
        lines += ["", "## 同票同日量纲冲突（≥60x，多分片并存）", "",
                  "| 代码 | 日期 | 来源值 | 倍数 |", "|---|---|---|---|"]
        for rec in report["conflicts"]["unit"][:30]:
            vals = " vs ".join(f"{v['shard']}={fmt_vol(v['vol'])}" for v in rec["values"])
            lines.append(f"| {rec['code']} | {rec['date']} | {vals} | "
                         f"{fmt_ratio(rec['ratio'])} |")
    lines += [
        "", "## 修复建议", "",
        "1. 先跑 `python3 scan_volume_units.py --code <代码> --dry-run` 逐票复核切换点（会打印每段行数、量级、倍数与置信）；",
        "2. 确认后跑 `python3 scan_volume_units.py --apply`（默认只改片内切换点之后的股段），"
        "只生成 `bulk/<分片>.fixed`，**不原地改分片**；人工 `diff` 抽样后再决定是否替换；",
        "3. 同票同日多分片并存且量纲不同（上表冲突）属于分片加载优先级问题，"
        "应在读取侧定优先级，不要靠改数据掩盖；",
        "4. 「整段为股」的序列（前缀分片 20/30/60/68）是另一代导入的口径，"
        "要统一需显式 `--apply-scope all`，并先评估下游消费方是否已按手校准；",
        "5. 低置信切换点（后续段没稳住 / 单根撑高 / 量级 < 100）多为真放量或低频股噪声，需人工看过再动；",
        "6. 本次只扫 `day_qfq_\\d+\\.csv`；`day_qfq_expand/extra/recent/stockdb` 未纳入，需要时另跑；",
        "7. 根因在补数脚本的新浪回退路径量纲（股 -> 手），修复后应只影响未来追加行。",
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def print_code_detail(state, code: str, multisource: dict, since: str,
                      scale: dict | None = None) -> None:
    series = sorted(((k, v) for k, v in state.items() if k[0] == code), key=lambda kv: kv[0][1])
    if not series:
        print(f"[{code}] 在数值分片中未找到任何行")
        return
    print(f"===== {code} 判定过程 =====")
    for (_, shard), s in series:
        print(f"\n-- 分片 {shard}: {s.rows} 行, {s.first_date} ~ {s.last_date}, "
              f"乱序行 {s.non_monotonic}")
        print(f"   首段: {s.segments[0]['start']} ~ {s.segments[0]['end']} "
              f"({s.segments[0]['rows']} 行, 均值 {fmt_vol(s.segments[0]['mean_vol'])})"
              if s.segments else "   首段: 无")
        print(f"   基准量级(尾部 {min(s.rows, TAIL_N)} 行中位数): "
              f"{fmt_vol(s.baseline) if s.baseline else 'n/a'}")
        rec = (scale or {}).get((code, shard))
        if rec:
            r = rec["ratio_vs_hash_ref"]
            print(f"   跨分片量级: 族={rec['family']}, 对哈希手基准倍数="
                  f"{f'{r:.1f}x' if r else 'n/a'}, 量纲猜测={rec['unit']}"
                  f"({rec['confidence'] or '-'})")
        if not s.switches:
            print("   片内切换点: 无（相邻行量比均不在 60~160 区间，或价格不连续）")
        for i, sw in enumerate(s.switches, 1):
            print(f"   切换点#{i}: {sw['prev_date']} -> {sw['date']} "
                  f"vol {fmt_vol(sw['before_vol'])} -> {fmt_vol(sw['after_vol'])} "
                  f"= {fmt_ratio(sw['ratio'])}, 涨跌 {sw['price_change']:.2%}, "
                  f"间隔 {sw['gap_days']} 天, 后续段 {sw.get('after_segment_rows', 0)} 行, "
                  f"置信={sw.get('confidence', '?')}")
        if len(s.segments) > 1:
            print("   分段:")
            for seg in s.segments:
                print(f"     {seg['start']} ~ {seg['end']}: {seg['rows']} 行, "
                      f"均值 {fmt_vol(seg['mean_vol'])}, 单位={seg.get('unit', '?')}")
    buckets = multisource.get(code, {})
    if buckets:
        multi = [(d, v) for d, v in sorted(buckets.items()) if isinstance(v, list)]
        print(f"\n-- 同票同日多来源（{since} 之后共 {len(buckets)} 个交易日，"
              f"其中 {len(multi)} 天有多个来源）")
        for d, vals in multi[-12:]:
            shown = ", ".join(f"{sh}={fmt_vol(v)}" for sh, v in vals)
            nums = [v for _, v in vals if v > 0]
            r = max(nums) / min(nums) if len(nums) > 1 else 1.0
            print(f"   {d}: {shown}  (max/min={r:.2f}x)")


def apply_fix(bulk: str, state, scope: str = "switch") -> dict:
    """把标记为 shares 的段 /100 写入 bulk/<分片>.fixed，绝不改原分片。

    scope: switch = 仅片内切换点之后的股段（默认）；all = 额外包含「整段为股」的
    高置信序列（如前缀分片 20/30/60/68 的全历史）。
    """
    plan: dict[str, dict[str, list]] = {}
    for (code, shard), s in state.items():
        segs = [seg for seg in s.segments if seg.get("unit") == SHARES]
        if not segs:
            continue
        if not s.switches and scope != "all":
            continue  # 整段为股的序列默认不动，需显式 --apply-scope all
        plan.setdefault(shard, {})[code] = segs
    changed: dict[str, dict] = {}
    for shard, codes in sorted(plan.items()):
        src = os.path.join(bulk, shard)
        dst = os.path.join(bulk, shard + ".fixed")
        per_code = {c: 0 for c in codes}
        with open(src, "r", encoding="utf-8", errors="replace") as fin, \
                open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                if not line or line[0] == "#":
                    fout.write(line)
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) != 7 or parts[0] not in codes or not DATE_RE.match(parts[1]):
                    fout.write(line)
                    continue
                d = parts[1]
                hit = False
                for seg in codes[parts[0]]:
                    if seg["start"] <= d <= seg["end"]:
                        hit = True
                        break
                if hit:
                    try:
                        vol = float(parts[6]) / 100.0
                    except ValueError:
                        fout.write(line)
                        continue
                    parts[6] = ("%.4f" % vol).rstrip("0").rstrip(".")
                    fout.write("|".join(parts) + "\n")
                    per_code[parts[0]] += 1
                else:
                    fout.write(line)
        changed[shard] = per_code
        log(f"[apply] {shard} -> {shard}.fixed, 改动 {sum(per_code.values())} 行")
    total_rows = sum(sum(v.values()) for v in changed.values())
    return {"shards": {k: {"rows_changed": sum(v.values()), "per_code": v}
                       for k, v in changed.items()},
            "total_rows_changed": total_rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="扫描 K 线分片成交量量纲（手/股）混用，默认干跑")
    ap.add_argument("--bulk", default=DEFAULT_BULK, help="分片目录")
    ap.add_argument("--reports-dir", default=DEFAULT_REPORTS, help="报告输出目录")
    ap.add_argument("--date-tag", default=datetime.now().date().isoformat(), help="报告文件名日期标签")
    ap.add_argument("--code", default=None, help="只处理单票并打印判定过程")
    ap.add_argument("--since", default=None, help="多来源比对的起始日期（默认 90 天前）")
    ap.add_argument("--top", type=int, default=20, help="Markdown Top N")
    ap.add_argument("--dry-run", action="store_true", help="显式干跑（默认即干跑）")
    ap.add_argument("--apply", action="store_true", help="生成 bulk/<分片>.fixed（不原地改分片）")
    ap.add_argument("--apply-scope", choices=("switch", "all"), default="switch",
                    help="apply 范围: switch=仅片内切换点之后的股段（默认）, "
                         "all=额外含「整段为股」的序列")
    args = ap.parse_args(argv)

    if args.apply and args.code:
        ap.error("--apply 与 --code 互斥：--apply 需要全量扫描才能写完整 .fixed")
    if not os.path.isdir(args.bulk):
        ap.error(f"bulk 目录不存在: {args.bulk}")
    since = args.since
    if since is None:
        since = "2026-09-01"
    if args.code:
        since = "0000-00-00"

    t0 = time.time()
    state, multisource, skipped, rows = scan(args.bulk, args.code, since)
    scale = analyze_scale(state)
    conflicts = collect_conflicts(multisource)
    elapsed = time.time() - t0
    assign_units(state, scale)
    annotate_confidence(state)

    if args.code:
        print_code_detail(state, args.code, multisource, since, scale)
        entry = {k: v for k, v in state.items() if k[0] == args.code}
        print(f"\n-- 汇总: {len(entry)} 个分片来源, "
              f"片内切换点 {sum(len(s.switches) for s in entry.values())}, "
              f"扫描行数 {rows}, 耗时 {elapsed:.2f}s")
        if args.apply:
            print("[warn] --code 与 --apply 互斥，未写任何文件")
        return 0

    report = build_report(state, scale, conflicts,
                          {"bulk": args.bulk, "since": since, "code": args.code,
                           "elapsed": elapsed, "rows": rows, "skipped": skipped})
    if args.apply:
        report["apply"] = apply_fix(args.bulk, state, args.apply_scope)
    json_path = os.path.join(args.reports_dir, f"volume_units_{args.date_tag}.json")
    md_path = os.path.join(args.reports_dir, f"volume_units_{args.date_tag}.md")
    write_json(report, json_path)
    write_md(report, md_path, args.top)
    s = report["summary"]
    print(f"扫描 {s['rows_scanned']} 行, 跳过 {s['skipped_rows']} 行, "
          f"耗时 {report['runtime_sec']}s")
    print(f"受影响票数 {s['stocks_affected']}, 片内切换点 {s['switch_points_total']}"
          f"(高置信 {s['switch_points_high']}, {s['switch_stocks']} 票), "
          f"整段为股(高) {s['whole_series_shares_high']}, "
          f"整段为股(中) {s['whole_series_shares_medium']}, "
          f"量纲冲突 {s['unit_conflicts']}, 数值冲突 {s['value_conflicts']}")
    print("按分片(切换点/整段股): " + ", ".join(
        f"{k}={v['switch_points']}/{v['whole_series_shares']}"
        for k, v in s["shards"].items()))
    print(f"报告: {json_path}")
    print(f"报告: {md_path}")
    if not args.apply:
        print("干跑模式：未写任何数据文件（--apply 也只写 *.fixed）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

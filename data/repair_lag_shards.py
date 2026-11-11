#!/usr/bin/env python3
"""repair_lag_shards.py — 修复 6 个滞后分片 (02/04/06/07/08/09)。

背景: incr_update.py 上一轮全量增量按"规范分片"(shard_for)归位, 把老分片
(02/04/06/07/08/09)中错位存放股票的新行 (2026-10-17~10-23) 追加到了规范分片
(00/01/60/...), 造成跨片分裂: 老片历史止于 10-16, 新行在别片, 且两片复权基准
不同 (_loader.py 主分片优先只取行数最多者, 新行被丢弃)。

本脚本:
  1) 收集 6 滞后片全部股票的 last_date/last_close;
  2) 从全部 14 个主分片收集这些代码 date > 2026-10-16 的错位新行;
  3) 复权基准判定: 新行首日 open 与老片末日 close 偏差 >15% 视为基准跳变,
     scale = 老片末close/新片首日open 对 o/c/h/l 缩放归位 (vol 不缩);
  4) 归位行插回老片该股票最后一个连续段末尾;
  5) 从规范分片删除错位行 (原文件先备份 .bak_misplaced);
  6) 保底: 归位后最新日期仍 < 2026-10-23 的, 用 incr_update.fetch_stock 补采。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BULK = os.path.join(HERE, "bulk")
sys.path.insert(0, HERE)
from incr_update import fetch_stock, fmt  # noqa: E402

CST = timezone(timedelta(hours=8))
LAG_SHARDS = ["02", "04", "06", "07", "08", "09"]
ALL_MAIN = [f"{s:02d}" for s in range(10)] + ["20", "30", "60", "68"]
CUTOFF = "2026-10-16"
REPORT = os.path.join(BULK, "_repair_report.json")


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def scan_new_rows(path, want_codes):
    """返回 {code: [(lineno, line)]}: want_codes 中 date > CUTOFF 的行。"""
    rows = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if line.startswith("#") or "|" not in line:
                continue
            p = line.split("|", 2)
            if p[0] in want_codes and p[1] > CUTOFF:
                rows.setdefault(p[0], []).append((i, line.rstrip("\n")))
    return rows


def scan_shard_segments(path):
    """返回 {code: [[start,end), ...]} 支持同码多个连续段。"""
    segs = {}
    cur, start = None, 0
    last = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if line.startswith("#") or "|" not in line:
                continue
            code = line.split("|", 1)[0]
            if code != cur:
                if cur is not None:
                    segs.setdefault(cur, []).append([start, last])
                cur, start = code, i
            last = i + 1
    if cur is not None:
        segs.setdefault(cur, []).append([start, last])
    return segs


def parse_row(line):
    p = line.split("|")
    return p[0], p[1], float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[6])


def main():
    t0 = time.time()
    # ---- 1) 6 滞后片: 每只股票 last_date/last_close
    lag_info = {}
    for s in LAG_SHARDS:
        p = os.path.join(BULK, f"day_qfq_{s}.csv")
        lastline = {}
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                code = line.split("|", 1)[0]
                lastline[code] = line
        n = 0
        for code, ln in lastline.items():
            try:
                _, d, _, c, *_ = parse_row(ln)
            except Exception:
                continue
            lag_info[code] = {"shard": s, "last_date": d, "last_close": c}
            n += 1
        log(f"滞后片 {s}: {n} 只股")

    codes = set(lag_info)
    log(f"6 片合计 {len(codes)} 只股")

    # ---- 2) 扫全部主分片找错位新行 (date > CUTOFF)
    misplaced = {}  # code -> {shard: [(lineno, line)]}
    for s in ALL_MAIN:
        p = os.path.join(BULK, f"day_qfq_{s}.csv")
        rows = scan_new_rows(p, codes)
        for code, lst in rows.items():
            if s == lag_info[code]["shard"]:
                continue
            misplaced.setdefault(code, {})[s] = lst
        log(f"扫片 {s} 完, 有错位新行的股累计 {len(misplaced)}")

    # ---- 3) 归位: 基准缩放 + 分组
    insert_by_shard = {s: {} for s in LAG_SHARDS}
    delete_by_shard = {}
    scaled_cnt = plain_cnt = 0
    for code, by_shard in misplaced.items():
        info = lag_info[code]
        rows = {}
        for s, lst in by_shard.items():
            for lineno, line in lst:
                d = parse_row(line)[1]
                rows.setdefault(d, (s, lineno, line))
        dates = sorted(rows)
        if not dates:
            continue
        first = parse_row(rows[dates[0]][2])
        ratio = info["last_close"] / first[2] if first[2] else 1.0
        scale = 1.0
        if abs(ratio - 1.0) > 0.15:
            scale = ratio
            scaled_cnt += 1
        else:
            plain_cnt += 1
        newlines = []
        for d in dates:
            s, lineno, line = rows[d]
            delete_by_shard.setdefault(s, set()).add(lineno)
            if scale != 1.0:
                c, dd, o, cl, h, l, v = parse_row(line)
                line = "|".join([c, dd, fmt(o * scale), fmt(cl * scale),
                                 fmt(h * scale), fmt(l * scale), fmt(v)])
            newlines.append(line)
        insert_by_shard[info["shard"]][code] = newlines
    log(f"归位: 基准一致 {plain_cnt} 只, 缩放 {scaled_cnt} 只")

    # ---- 4) 保底补采
    need_fetch = []
    for code, info in lag_info.items():
        got = insert_by_shard[info["shard"]].get(code, [])
        newest = max([parse_row(l)[1] for l in got], default=info["last_date"])
        if newest < "2026-10-23":
            need_fetch.append(code)
    log(f"需补采 {len(need_fetch)} 只")
    fetched_ok = fetched_fail = 0
    fail_list = []
    for i, code in enumerate(need_fetch):
        info = lag_info[code]
        got = insert_by_shard[info["shard"]].setdefault(code, [])
        have = {parse_row(l)[1] for l in got} | {info["last_date"]}
        last_date = max(have)
        rows, err = fetch_stock(code, n=30)
        if rows is None:
            fetched_fail += 1
            fail_list.append((code, err))
            continue
        new = [r for r in rows if r[0] > last_date]
        if not new:
            continue
        if got:
            base_open = parse_row(got[0])[2]
        else:
            base_open = float(new[0][1])
        ratio = info["last_close"] / base_open if base_open else 1.0
        scale = ratio if abs(ratio - 1.0) > 0.15 else 1.0
        for r in new:
            o, c, h, l = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            got.append("|".join([code, r[0], fmt(o * scale), fmt(c * scale),
                                 fmt(h * scale), fmt(l * scale), fmt(float(r[5]))]))
        fetched_ok += 1
        if (i + 1) % 100 == 0:
            log(f"补采 {i+1}/{len(need_fetch)} ok={fetched_ok} fail={fetched_fail}")
        time.sleep(0.12)
    log(f"补采完 ok={fetched_ok} fail={fetched_fail}")

    # ---- 5) 写回滞后片 (段内插入)
    stats = {}
    for s in LAG_SHARDS:
        ins = insert_by_shard[s]
        if not ins:
            stats[s] = 0
            continue
        p = os.path.join(BULK, f"day_qfq_{s}.csv")
        segs = scan_shard_segments(p)
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        offsets = []
        for code, newlines in ins.items():
            seg = segs[code][-1]
            offsets.append((seg[1], newlines))
        offsets.sort(key=lambda x: x[0])
        out, prev, added = [], 0, 0
        for pos, newlines in offsets:
            out.extend(lines[prev:pos])
            out.extend(l + "\n" for l in newlines)
            added += len(newlines)
            prev = pos
        out.extend(lines[prev:])
        tmp = p + ".tmp_repair"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
        stats[s] = added
        log(f"滞后片 {s}: 插入 {added} 行 ({len(ins)} 只股)")

    # ---- 6) 删除规范分片中的错位行 (先备份)
    for s, linenos in delete_by_shard.items():
        p = os.path.join(BULK, f"day_qfq_{s}.csv")
        bak = p + ".bak_misplaced"
        if not os.path.exists(bak):
            os.rename(p, bak)
            src = bak
        else:
            src = p
        with open(src, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        out = [l for i, l in enumerate(lines) if i not in linenos]
        tmp = p + ".tmp_repair"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
        log(f"分片 {s}: 删除错位行 {len(linenos)} 行 (备份 {os.path.basename(bak)})")

    report = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "lag_shards": LAG_SHARDS, "codes": len(codes),
        "misplaced_moved": plain_cnt + scaled_cnt,
        "scaled": scaled_cnt, "fetched_ok": fetched_ok,
        "fetched_fail": fetched_fail, "fail_list": fail_list[:100],
        "rows_inserted_per_shard": stats,
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(REPORT, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"完成, 报告 -> {REPORT}")


if __name__ == "__main__":
    main()

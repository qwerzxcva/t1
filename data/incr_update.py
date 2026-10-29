#!/usr/bin/env python3
"""incr_update.py — 一次性增量补数脚本 (用完可由维护者删除)。

把 bulk 主分片(00-09/20/30/60/68)与指数 history 补齐到最新交易日。
数据源: 腾讯 ifzq fqkline (qfqday), 与 bulk 既有口径一致: code|date|open|close|high|low|vol
只追加到 15 个主分片 + 指数 history + daily_index.txt, 不写 expand/extra/recent。

用法:
  python3 incr_update.py --codes sh600519,sz000001   # 小样本试跑
  python3 incr_update.py --full                       # 个股全量增量
  python3 incr_update.py --index                      # 指数 5 个
"""
import argparse, json, os, subprocess, sys, time, urllib.error
from datetime import datetime, timezone, timedelta


def hard_get(url, headers, timeout=15, total_cap=25):
    """curl subprocess: proot 容器内 urllib socket read 会挂死, curl --max-time 可靠。"""
    cmd = ["curl", "-s", "--max-time", str(total_cap), "--connect-timeout", "10", url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    r = subprocess.run(cmd, capture_output=True, timeout=total_cap + 5)
    if r.returncode != 0:
        raise IOError(f"curl_rc_{r.returncode}")
    return r.stdout

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}
HERE = os.path.dirname(os.path.abspath(__file__))
BULK = os.path.join(HERE, "bulk")
IDX_HIST = os.path.join(HERE, "index", "history")
MAIN_SHARDS = [f"day_qfq_{s:02d}.csv" for s in range(10)] + \
              ["day_qfq_20.csv", "day_qfq_30.csv", "day_qfq_60.csv", "day_qfq_68.csv"]
PROGRESS = os.path.join(BULK, "_incr_progress.json")
INDICES = [("sh000001", "上证综指"), ("sh000300", "沪深300"), ("sz399001", "深证成指"),
           ("sz399006", "创业板指"), ("sh000688", "科创50")]


def log(m):
    print(f"[{datetime.now(CST):%H:%M:%S}] {m}", flush=True)


def is_bad(body):
    return (b"501page" in body[:400] or b"waf.tencent.com" in body[:400]
            or not body.startswith(b"{"))


def fetch_sina(code, n=800):
    """新浪 getKLineData (qfq, 价格与腾讯 qfq 一致; volume=股数)。
    -> (rows[[date,open,close,high,low,vol],...] , err)"""
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/"
           f"CN_MarketDataService.getKLineData?symbol={code}&scale=240&ma=no&datalen={n}")
    for _ in range(3):
        try:
            body = hard_get(url, UA, total_cap=20)
            txt = body.decode("utf-8", "replace")
            i = txt.find("x(")
            if i < 0:
                return None, "sina_bad"
            arr = json.loads(txt[i + 2:txt.rfind(")")])
            rows = []
            for r in arr:
                # 新浪 volume 单位是「股」，腾讯 qfqday 是「手」，bulk 历史口径也是「手」。
                # 不折算就会在 append 点留下 100 倍量纲跳变（实测 sh600000 2026-10-13 的
                # 529089 → 10-19 的 54074699，价格却 9.61→9.78 连续）。
                rows.append([r["day"], r["open"], r["close"], r["high"],
                             r["low"], float(r["volume"]) / 100.0])
            return rows, None
        except (IOError, subprocess.TimeoutExpired):
            return None, "hard_timeout"
        except Exception:
            time.sleep(2)
    return None, "network"


def fetch_kline(code, n=800, max_wait=180):
    """-> (rows[[date,open,close,high,low,vol],...] , err)"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{n},qfq")
    waited = 0
    for _ in range(4):
        try:
            body = hard_get(url, UA)
            if is_bad(body):
                return None, "waf"
            d = json.loads(body.decode("utf-8", "replace"))
            node = (d.get("data") or {}).get(code) or {}
            arr = node.get("qfqday") or node.get("day") or []
            rows = []
            for r in arr:
                if isinstance(r, list) and len(r) >= 6:
                    rows.append([str(r[0]), r[1], r[2], r[3], r[4], r[5]])
            return rows, None
        except (IOError, subprocess.TimeoutExpired):
            return None, "hard_timeout"
        except Exception:
            time.sleep(2)
    return None, "network"


def fetch_stock(code, n=800):
    """个股: 新浪主源 (腾讯被 WAF 封) -> 腾讯回退。"""
    rows, err = fetch_sina(code, n)
    if rows is not None:
        return rows, None
    rows, err2 = fetch_kline(code, n)
    if rows is not None:
        return rows, None
    return None, f"{err}/{err2}"


def fmt(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_index():
    """code -> last_date (跨全部主分片取最大; 已知跨片污染, 增量统一按规范分片归位)"""
    info = {}
    for name in MAIN_SHARDS:
        p = os.path.join(BULK, name)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) < 7:
                    continue
                code, date = parts[0], parts[1]
                if date > info.get(code, ""):
                    info[code] = date
    return info


def load_universe():
    codes = []
    with open(os.path.join(BULK, "universe.csv"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            c = line.split("|")[0].strip()
            if c:
                codes.append(c)
    return codes


def shard_for(code):
    num = code[2:] if code[:2] in ("sh", "sz") else code
    for pre, shard in (("30", "day_qfq_30.csv"), ("68", "day_qfq_68.csv"),
                       ("60", "day_qfq_60.csv"), ("20", "day_qfq_20.csv")):
        if num.startswith(pre):
            return shard
    return f"day_qfq_{int(num[0]) if num[:1].isdigit() else 0:02d}.csv"


def flush(appends):
    for name, rows in appends.items():
        if not rows:
            continue
        with open(os.path.join(BULK, name), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(r + "\n")
            f.flush(); os.fsync(f.fileno())


def run_stocks(todo, sleep):
    log("构建分片索引 ...")
    info = build_index()
    log(f"已有股票 {len(info)}")
    appends = {n: [] for n in MAIN_SHARDS}
    n_ok = n_nodata = n_fail = 0
    failed = []
    for i, code in enumerate(todo):
        last_date = info.get(code)
        rows, err = fetch_stock(code, n=800)
        if rows is None:
            n_fail += 1; failed.append((code, err))
            if n_fail >= 50 and n_fail > n_ok * 2:
                log(f"失败风暴 ok={n_ok} fail={n_fail}, 提前退出")
                break
            continue
        new = []
        for r in rows:
            d = r[0]
            if last_date and d <= last_date:
                continue
            new.append("|".join([code, d, fmt(r[1]), fmt(r[2]), fmt(r[3]),
                                 fmt(r[4]), fmt(r[5])]))
        if new:
            shard = shard_for(code)
            appends[shard].extend(new)
            n_ok += 1
        else:
            n_nodata += 1
        if (i + 1) % 50 == 0:
            flush(appends)
            appends = {n: [] for n in MAIN_SHARDS}
            json.dump({"i": i, "ok": n_ok, "nodata": n_nodata, "fail": n_fail,
                       "ts": datetime.now(CST).isoformat(timespec="seconds")},
                      open(PROGRESS, "w"))
            log(f"{i+1}/{len(todo)} ok={n_ok} nodata={n_nodata} fail={n_fail}")
        time.sleep(sleep)
    flush(appends)
    json.dump({"done": True, "ok": n_ok, "nodata": n_nodata, "fail": n_fail,
               "failed": failed[:300],
               "ts": datetime.now(CST).isoformat(timespec="seconds")},
              open(PROGRESS, "w"))
    log(f"个股完成 ok={n_ok} nodata={n_nodata} fail={n_fail}")
    return n_ok, n_fail, failed


def run_index():
    daily_rows = []
    for code, name in INDICES:
        p = os.path.join(IDX_HIST, f"{code}.txt")
        last_date = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "|" in line:
                        d = line.split("|")[1]
                        if d > last_date:
                            last_date = d
        rows, err = fetch_kline(code, n=800)
        if rows is None:
            log(f"指数 {code} 失败: {err}")
            continue
        prev_by_date = {}
        prev_close = None
        for r in rows:
            if r[0] <= last_date:
                prev_close = float(r[2])
        new = [r for r in rows if r[0] > last_date]
        if new:
            with open(p, "a", encoding="utf-8") as f:
                for r in new:
                    f.write("|".join([code, r[0], fmt(r[1]), fmt(r[2]),
                                      fmt(r[3]), fmt(r[4]), fmt(r[5])]) + "\n")
            log(f"指数 {code} +{len(new)} 行 -> {new[-1][0]}")
        else:
            log(f"指数 {code} 已最新 ({last_date})")
        for r in new:
            o, c, h, l, v = map(float, (r[1], r[2], r[3], r[4], r[5]))
            chg = (c / prev_close - 1) * 100 if prev_close else 0.0
            amp = (h - l) / prev_close * 100 if prev_close else 0.0
            daily_rows.append("|".join([r[0], code, code, fmt(o), fmt(c), fmt(h),
                                        fmt(l), fmt(v), f"{chg:.2f}", f"{amp:.2f}"]))
            prev_close = c
        time.sleep(0.5)
    if daily_rows:
        daily_rows.sort()
        with open(os.path.join(HERE, "daily_index.txt"), "a", encoding="utf-8") as f:
            for r in daily_rows:
                f.write(r + "\n")
        log(f"daily_index.txt +{len(daily_rows)} 行")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--start", type=int, default=0, help="universe 起始下标(断点续跑)")
    args = ap.parse_args()
    if args.index:
        run_index()
        return
    if args.codes:
        todo = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.full:
        todo = load_universe()[args.start:]
        log(f"universe 总数含已跳过, 本次从 {args.start} 起, todo={len(todo)}")
    else:
        ap.error("需要 --codes/--full/--index")
    run_stocks(todo, args.sleep)


if __name__ == "__main__":
    main()

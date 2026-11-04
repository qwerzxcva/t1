#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""团队预设体检器 —— 校验 teams/*.md 引用的角色在当前启用名册里是否真的点得出名。

为什么需要它：团队预设的 agent 字段是**硬编码角色名快照**。一旦某个角色被禁用
（agents.json 的 disabled 名单）或改名，整个团队会在第一个阶段直接
AGENT_NOT_FOUND 全队失败，而且只有真跑一次才会发现。

用法：
    python3 ~/workspace/scripts/validate_teams.py            # 校验全部团队
    python3 ~/workspace/scripts/validate_teams.py -v         # 附带打印启用名册
退出码：0 全部可用；1 有硬错误（角色不存在/已禁用/字段缺失/路径相交）。

检查项：
  1. stages[].agent / tasks[].agent 是否在启用名册里（不存在 = MISSING，存在但禁用 = DISABLED）
  2. 同一阶段内 writePaths 是否相交（目录包含关系也算相交）
  3. 阶段内任务数 > 3 时告警（本项目实测：5 并行子代理曾全部 connection closed）
  4. 是否有团队公约（正文非空）
  5. 阶段 prompt 是否带「内联角色卡前缀」（首行 `【角色】<名字>：<2~3 条执行标准>`，不带只告警）
"""
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
WS = os.environ.get("WORKSPACE") or os.path.join(HOME, "workspace")
GLOBAL_TEAMS = os.path.join(HOME, ".aharou", "teams")
PROJ_TEAMS = os.path.join(WS, ".aharou", "teams")
AGENTS_DIR = os.path.join(HOME, ".aharou", "agents")

MAX_PARALLEL_PER_STAGE = 3


HAS_YAML = True
try:  # PyYAML 不一定装（本机 2026-10-31 实测：容器内无 yaml）
    import yaml  # noqa: F401
except Exception:  # pragma: no cover - 环境相关
    HAS_YAML = False


def _frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    if HAS_YAML:
        try:
            import yaml
            return (yaml.safe_load(raw) or {}), body
        except Exception:
            return {"_parse_error": True}, body
    return _mini_frontmatter(raw), body


def _mini_frontmatter(raw):
    """无 PyYAML 时的降级解析：只取校验要用的字段，不强求完整 YAML。

    覆盖两种团队写法：stages[].tasks[] 列表式，以及 stages[] 直接带 agent/prompt 的单任务式。
    与初版相比修掉了四类会静默失效的坑（2026-10-31 审查 S11~S14）：
      · tasks 的分隔只看「与首个任务同缩进」的 `- `，writePaths 的列表项不再被当成新任务；
      · 块标量（`prompt: |-`）内部的 `- xxx` 不再切断 prompt；
      · stages / tasks 的缩进基准从实际文件推导，不再硬编码 0~2 空格；
      · 内联 `writePaths: ["*"]` 也会被解析（否则通配检查可被绕过）。
    它是体检器不是 YAML 实现：其余字段（maxSteps 等）一律忽略。
    """
    fm = {"_degraded": True}
    top = re.search(r'^name:\s*"?([^"\n]+)"?\s*$', raw, re.M)
    if top:
        fm["name"] = top.group(1).strip()
    adm = re.search(r"^admin:\s*\n((?:\s+.*\n?)+)", raw, re.M)
    if adm:
        nm = re.search(r'^\s+name:\s*"?([^"\n]+)"?\s*$', adm.group(1), re.M)
        if nm:
            fm["admin"] = {"name": nm.group(1).strip()}

    lines = raw.split("\n")
    try:
        i0 = next(i for i, l in enumerate(lines) if re.match(r"^stages:\s*$", l))
    except StopIteration:
        return fm
    i1 = len(lines)
    for i in range(i0 + 1, len(lines)):
        if re.match(r"^[A-Za-z_][\w-]*:", lines[i]):
            i1 = i
            break
    seg = lines[i0 + 1:i1]

    def indent_of(line):
        return len(line) - len(line.lstrip(" "))

    # 块标量区间：从声明行到下一个缩进 <= 声明行的非空行为止
    block_start = {}
    k = 0
    while k < len(seg):
        m = re.match(r"^(\s*)(?:-\s+)?[A-Za-z_][\w-]*:\s*(\||>|[|>][-+]?)\s*$", seg[k])
        if m:
            base = len(m.group(1))
            j = k + 1
            while j < len(seg):
                if seg[j].strip() and indent_of(seg[j]) <= base:
                    break
                j += 1
            for x in range(k, j):
                block_start[x] = k
            k = j
            continue
        k += 1

    def is_sep(i, ind):
        """第 i 行是否是缩进为 ind 的列表项开头（且不在块标量内部）。"""
        if i in block_start:
            return False
        l = seg[i]
        if not re.match(r"^\s*-\s+", l):
            return False
        return indent_of(l) == ind

    stage_pos = [i for i in range(len(seg)) if re.match(r"^\s*-\s+title:", seg[i])]
    if not stage_pos:
        return fm

    def read_task(chunk):
        """解析单个任务块。块标量用局部缩进判定（不依赖 seg 的全局下标）。"""
        task = {}

        def ind_of(line):
            return len(line) - len(line.lstrip(" "))

        kk = 0
        while kk < len(chunk):
            l = chunk[kk]
            if not l.strip():
                kk += 1
                continue
            mm = re.match(r"^\s*(?:-\s+)?([A-Za-z_][\w-]*):\s*(.*)$", l)
            if not mm:
                kk += 1
                continue
            key, val = mm.group(1), mm.group(2).strip()
            ind = ind_of(l)
            if key == "writePaths":
                if val:
                    task[key] = _eval_list(val)
                    kk += 1
                    continue
                items = []
                j = kk + 1
                while j < len(chunk):
                    l2 = chunk[j]
                    if l2.strip() and ind_of(l2) <= ind:
                        break
                    m2 = re.match(r"^\s*-\s+(.+)$", l2)
                    if m2:
                        items.append(_eval_item(m2.group(1)))
                    j += 1
                task[key] = items
                kk = j
                continue
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                buf = []
                j = kk + 1
                while j < len(chunk) and (not chunk[j].strip() or ind_of(chunk[j]) > ind):
                    buf.append(chunk[j].strip())
                    j += 1
                task[key] = " ".join(x for x in buf if x)
                kk = j
                continue
            task[key] = val.strip('"')
            kk += 1
        return task

    stages = []
    for si, st in enumerate(stage_pos):
        en = stage_pos[si + 1] if si + 1 < len(stage_pos) else len(seg)
        chunk = seg[st:en]
        head = re.match(r"^\s*-\s+title:\s*(.*)$", chunk[0])
        title = head.group(1).strip().strip('"') if head else ""
        tpos = next((i for i, l in enumerate(chunk) if re.match(r"^\s*tasks:\s*$", l)), None)
        if tpos is None:
            tasks = [read_task(chunk)]
        else:
            cand = [i for i in range(tpos + 1, len(chunk))
                    if re.match(r"^\s*-\s+(agent|department|agentQuery|prompt|writePaths):", chunk[i])]
            tasks = []
            if cand:
                task_ind = len(chunk[cand[0]]) - len(chunk[cand[0]].lstrip(" "))
                starts = [i for i in cand
                          if (len(chunk[i]) - len(chunk[i].lstrip(" "))) == task_ind]
                for ti, ts in enumerate(starts):
                    te = starts[ti + 1] if ti + 1 < len(starts) else len(chunk)
                    tasks.append(read_task(chunk[ts:te]))
        stages.append({"title": title, "tasks": tasks})
    fm["stages"] = stages
    return fm


def _eval_item(text):
    t = text.strip().strip('"').strip("'")
    return t


def _eval_list(val):
    """把内联列表解析成 list[str]；解析不了就退化成整串（宁可误报也不放过 `["*"]`）。"""
    v = val.strip()
    try:
        import ast
        got = ast.literal_eval(v)
        if isinstance(got, (list, tuple)):
            return [str(x) for x in got]
        if isinstance(got, str):
            return [got]
    except Exception:
        pass
    inner = v.strip("[]")
    return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]


def load_roster():
    """返回 (enabled_names, all_names)。

    启用规则与 App 一致：定义文件里的 name 减去 disabled 名单；
    全局与项目级 disabled 取并集，任一作用域禁用即生效。
    """
    names = set()
    if os.path.isdir(AGENTS_DIR):
        for fn in os.listdir(AGENTS_DIR):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(AGENTS_DIR, fn), encoding="utf-8") as f:
                    fm, _ = _frontmatter(f.read())
            except OSError:
                continue
            n = fm.get("name")
            if isinstance(n, str) and n.strip():
                names.add(n.strip())
    disabled = set()
    for p in (
        os.path.join(HOME, ".aharou", "agents.json"),
        os.path.join(WS, ".aharou", "agents.json"),
    ):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            for x in d.get("disabled", []) or []:
                if isinstance(x, str):
                    disabled.add(x.strip())
        except (OSError, ValueError):
            pass
    return names - disabled, names


def iter_teams():
    """全局在前、项目级在后（同名时项目级优先，与运行时一致）。"""
    found = {}
    for d in (GLOBAL_TEAMS, PROJ_TEAMS):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                found[fn] = os.path.join(d, fn)
    return sorted(found.items())


def overlaps(a, b):
    a = a.strip("/")
    b = b.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def check_team(path, enabled, allnames):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return check_team_text(text, enabled, allnames)


def check_team_text(text, enabled, allnames):
    """对一段团队文件正文做全部结构校验（测试与单文件校验共用同一条路径）。"""
    errs, warns = [], []
    fm, body = _frontmatter(text)
    if fm.get("_parse_error"):
        return ["frontmatter YAML 解析失败（缩进/引号问题；PyYAML 已加载，是真解析错）"], []
    stages = fm.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["缺少 stages 或 stages 为空"], []
    if not body.strip():
        warns.append("没有团队公约（正文为空），全队共用约束无处安放")

    for i, st in enumerate(stages, 1):
        st = st if isinstance(st, dict) else {}
        title = st.get("title") or f"阶段{i}"
        tasks = st.get("tasks")
        if tasks is None:
            tasks = [{
                "agent": st.get("agent"),
                "department": st.get("department"),
                "agentQuery": st.get("agentQuery"),
                "prompt": st.get("prompt"),
                "writePaths": st.get("writePaths"),
            }]
        if not isinstance(tasks, list) or not tasks:
            errs.append(f"阶段{i}「{title}」没有任何任务")
            continue
        if len(tasks) > MAX_PARALLEL_PER_STAGE:
            warns.append(
                f"阶段{i}「{title}」并行 {len(tasks)} 个任务（> {MAX_PARALLEL_PER_STAGE}）："
                f"本项目实测并行过高易整体失败，建议拆阶段"
            )
        paths = []
        for j, t in enumerate(tasks, 1):
            t = t if isinstance(t, dict) else {}
            who = t.get("agent")
            dept = t.get("department")
            q = t.get("agentQuery")
            label = f"阶段{i}「{title}」任务{j}"
            if who:
                if who not in allnames:
                    errs.append(f"{label}: 角色「{who}」不存在（MISSING）")
                elif who not in enabled:
                    errs.append(f"{label}: 角色「{who}」已禁用，派单必然失败（DISABLED）")
            elif dept and q:
                warns.append(f"{label}: 用 department={dept} + agentQuery={q} 选角，只能运行时校验")
            else:
                errs.append(f"{label}: 既没有 agent，也没有 department+agentQuery")
            if not (t.get("prompt") or "").strip():
                errs.append(f"{label}: 缺少 prompt")
            elif "【角色】" not in t.get("prompt"):
                warns.append(
                    f"{label}: prompt 缺「内联角色卡前缀」（首行 `【角色】<名字>：<2~3 条执行标准>`；"
                    f"2026-10-27 22:00 起不再 readFile 角色文件，见 AGENTS.md 子代理协作第 11.1/22 条）"
                )
            elif "readFile 读 /root/.aharou/agents/" in t.get("prompt"):
                warns.append(
                    f"{label}: prompt 仍在使用旧的 readFile 角色卡注入（多 1 次请求 + 3k 字符，"
                    f"已改为内联角色卡，见 AGENTS.md 第 11.1 条）"
                )
            wp = t.get("writePaths")
            if isinstance(wp, list):
                if any(str(p).strip() in ("*", "/") for p in wp):
                    errs.append(f"{label}: writePaths 不允许 [\"*\"] 或根目录，请给精确路径")
                for p in wp:
                    paths.append((str(p), label))
        for a in range(len(paths)):
            for b in range(a + 1, len(paths)):
                if overlaps(paths[a][0], paths[b][0]):
                    errs.append(
                        f"阶段{i}「{title}」写入路径相交：{paths[a][0]}（{paths[a][1]}）"
                        f" vs {paths[b][0]}（{paths[b][1]}）"
                    )
    return errs, warns


def main():
    verbose = "-v" in sys.argv
    if not HAS_YAML:
        print("提示：容器内没有 PyYAML，已切到降级解析（只校角色名与 writePaths，"
              "并行数/路径相交按缩进近似判断）。要完整校验请装 pyyaml。")
    enabled, allnames = load_roster()
    print(f"启用名册：{len(enabled)} 个角色（定义总数 {len(allnames)}）")
    if verbose:
        for n in sorted(enabled):
            print(f"  · {n}")
    teams = iter_teams()
    if not teams:
        print("未找到任何团队预设（~/.aharou/teams 与 <ws>/.aharou/teams 都为空）")
        return 0
    bad = 0
    for fn, path in teams:
        errs, warns = check_team(path, enabled, allnames)
        fm, _ = _frontmatter(open(path, encoding="utf-8").read())
        name = fm.get("name") or fn[:-3]
        mark = "✗" if errs else ("!" if warns else "✓")
        print(f"\n{mark} {name}  ({path})")
        for e in errs:
            print(f"    ✗ {e}")
        for w in warns:
            print(f"    ! {w}")
        if not errs and not warns:
            print("    ✓ 全部角色可用，路径无相交")
        bad += 1 if errs else 0
    print(f"\n结论：{len(teams)} 个团队，{len(teams) - bad} 个可用，{bad} 个需修")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

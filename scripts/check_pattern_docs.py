#!/usr/bin/env python3
"""模式库文档结构完整性检查（22 吸收清单 #1）。

依据：skapp / aharou fork 双会话共同强调验收标准前置——文档层面同样需要：
每个会话蒸馏文档必须带「来源」「边界」「至少一条 SM/CC 证据」，
否则后续复用无法判断置信等级。

用法：check_pattern_docs.py [知识库目录]
  缺省检查 ~/.aharou/knowledge/eng-quality-gates
退出码：0 全过；1 有缺失项。
"""
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".aharou/knowledge/eng-quality-gates"

# 文档 -> 必需标记（正则，多行匹配）。
# 模式文档（20/21/23）要求 SM/CC 证据条目 + 使用边界章节 + 来源标记；
# 吸收文档（22）要求对照表与吸收清单。
REQUIRED = {
    "20-b-session-patterns.md": [
        (r"(?m)^#+ .*使用边界", "「使用边界」章节"),
        (r"来源", "「来源」标记"),
        (r"(?m)SM-\d+", "SM 思维条目"),
        (r"(?m)CC-\d+", "CC 代码能力条目"),
    ],
    "21-skapp-trading-patterns.md": [
        (r"(?m)^#+ .*使用边界", "「使用边界」章节"),
        (r"来源", "「来源」标记"),
        (r"(?m)SM-\d+", "SM 思维条目"),
        (r"(?m)CC-\d+", "CC 代码能力条目"),
    ],
    "22-dual-session-pattern-absorption.md": [
        (r"(?m)(^#+ .*边界|\*\*边界\*\*)", "「边界」标记"),
        (r"来源", "「来源」标记"),
        (r"核心模式对照", "双会话对照表"),
        (r"吸收清单", "吸收清单"),
    ],
    "23-ninth-round-distillation.md": [
        (r"(?m)^#+ .*使用边界", "「使用边界」章节"),
        (r"来源", "「来源」标记"),
        (r"(?m)SM-\d+", "SM 思维条目"),
        (r"(?m)CC-\d+", "CC 代码能力条目"),
    ],
}

def main() -> int:
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_ROOT
    if not root.is_dir():
        print(f"知识库目录不存在：{root}")
        return 1
    failures = 0
    for name, checks in REQUIRED.items():
        path = root / name
        if not path.is_file():
            print(f"  [缺失文档] {name}")
            failures += 1
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in checks:
            if re.search(pattern, text):
                continue
            print(f"  [不完整] {name}：缺{label}（{pattern!r} 未命中）")
            failures += 1
    # 编号连续性：最高编号存在则 1..max 必须全部连续
    numbers = sorted(int(m.group(1)) for m in map(
        lambda p: re.match(r"(\d+)-", p.name), root.glob("[0-9][0-9]-*.md")
    ) if m)
    if numbers:
        expected = list(range(1, max(numbers) + 1))
        gaps = [n for n in expected if n not in numbers]
        if gaps:
            print(f"  [编号断档] 缺少第 {gaps} 篇")
            failures += 1
    if failures:
        print(f"模式库文档结构检查 ✗ {failures} 项缺失")
        return 1
    print(f"模式库文档结构检查 ✓ {len(REQUIRED)} 篇完整")
    return 0

if __name__ == "__main__":
    sys.exit(main())

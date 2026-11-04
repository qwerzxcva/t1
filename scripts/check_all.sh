#!/usr/bin/env bash
# 注入面统一闸门：把散落的检查器串成一条命令，任一失败即整体失败。
#
# 用法：
#   ./scripts/check_all.sh            # 全量（6 项，约 1.5s）
#   ./scripts/check_all.sh --fast     # 只跑关键两项（约 1.1s），pre-commit 用
#   ./scripts/check_all.sh --list     # 只列出步骤
#
# 退出码：0 = 全部通过；1 = 有失败项
#
# 未纳入的脚本：
#   check_templates.py —— 一次性脚本（硬编码模板路径，缺文件即退出 1），不适合常驻闸门
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAST_STEPS=(validate_teams audit_inject)
FULL_STEPS=(validate_teams audit_inject check_prompts check_role_cards check_skills check_stage_artifacts check_pattern_docs)

case "${1:-}" in
  --fast) STEPS=("${FAST_STEPS[@]}"); LABEL="快速" ;;
  --list) printf '%s\n' "${FULL_STEPS[@]}"; exit 0 ;;
  *)      STEPS=("${FULL_STEPS[@]}"); LABEL="全量" ;;
esac

total=${#STEPS[@]}
i=0
fail=0
failed_names=()
start_all=$(date +%s%N)

echo "注入面闸门（${LABEL}）—— ${total} 项"
for s in "${STEPS[@]}"; do
  i=$((i + 1))
  if [ ! -f "scripts/$s.py" ]; then
    printf "  [%d/%d] %-26s SKIP（脚本缺失）\n" "$i" "$total" "$s"
    continue
  fi
  st=$(date +%s%N)
  out=$(python3 "scripts/$s.py" 2>&1)
  rc=$?
  en=$(date +%s%N)
  ms=$(( (en - st) / 1000000 ))
  if [ $rc -eq 0 ]; then
    printf "  [%d/%d] %-26s ✓  %sms\n" "$i" "$total" "$s" "$ms"
  else
    printf "  [%d/%d] %-26s ✗  %sms  (rc=%d)\n" "$i" "$total" "$s" "$ms" "$rc"
    echo "$out" | tail -12 | sed 's/^/         /'
    fail=$((fail + 1))
    failed_names+=("$s")
  fi
done

end_all=$(date +%s%N)
echo
if [ $fail -eq 0 ]; then
  printf "闸门通过 ✓ %d/%d，共 %sms\n" "$total" "$total" "$(( (end_all - start_all) / 1000000 ))"
  exit 0
else
  printf "闸门未通过 ✗ %d/%d 失败：%s\n" "$fail" "$total" "${failed_names[*]}"
  exit 1
fi

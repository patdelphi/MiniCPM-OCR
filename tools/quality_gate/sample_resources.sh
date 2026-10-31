#!/usr/bin/env bash
# 过程数据采样器：每 5 秒记录一次显存、内存、负载与相关进程数，写 JSONL。
# 用法：bash tools/quality_gate/sample_resources.sh <输出.jsonl> [采样秒数上限]
set -uo pipefail
OUT="${1:?输出 JSONL 路径 required}"
LIMIT="${2:-7200}"
END=$(( $(date +%s) + LIMIT ))
: > "$OUT"
while [ "$(date +%s)" -lt "$END" ]; do
  # 显存只能说明"服务在跑"，判断瓶颈要靠利用率：路线 C 的 OCR 侧要确认 GPU 到底被喂到几成
  gpu=$(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits | head -1)
  gpu_mem=${gpu%%,*}; gpu_util=${gpu##*,}
  ram=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo)
  load=$(cut -d' ' -f1 /proc/loadavg)
  # pgrep -c 找不到进程时既打印 0 又以 1 退出，接 `|| echo 0` 会多出一个 0 把整行 JSON 撑坏
  # （上一轮 378 行全部不可解析就是这个原因）。渲染进程名是 chrome-headless-shell，不是 chromium。
  render=$(pgrep -c -f chrome-headless-shell 2>/dev/null); render=${render:-0}
  printf '{"ts":%s,"gpu_used_mib":%s,"gpu_util_pct":%s,"ram_avail_mib":%s,"load1":%s,"render_procs":%s}\n' \
    "$(date +%s)" "${gpu_mem:-0}" "${gpu_util:-0}" "${ram:-0}" "${load:-0}" "${render}" >> "$OUT"
  sleep 5
done

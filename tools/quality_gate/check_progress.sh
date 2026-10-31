#!/usr/bin/env bash
# 路线 C 全量跑进度检查：完成页数、打分吞吐、内存/负载趋势、已用与预计剩余时间。
ROOT=/mnt/d/Github/MiniCPM-OCR
RUN=$ROOT/runs/route_c_full
NOW=$(date +%s)
START=$(cat /root/route_c_start.txt 2>/dev/null || echo "$NOW")
ELAPSED=$(( NOW - START ))

echo "=== 运行状态 ==="
pgrep -f 'minicpmvocr parse' > /dev/null && echo "管线进程：在跑" || echo "管线进程：已退出"
echo "已用 $(( ELAPSED / 60 )) 分 $(( ELAPSED % 60 )) 秒"

echo "=== 完成页数 ==="
DONE=$(ls "$RUN/output"/*.md 2>/dev/null | wc -l)
echo "已出 Markdown 页：$DONE / 57"

echo "=== CPU 打分 ==="
curl -s --noproxy '*' -m 5 http://127.0.0.1:18503/stats
echo
"$ROOT"/.venv-vllm/bin/python - "$ELAPSED" <<'EOF' 2>/dev/null
import json, statistics, sys
rows = [json.loads(l) for l in open('/root/jev_cpu_metrics.jsonl', encoding='utf-8') if l.strip()]
if not rows:
    print("尚无打分记录")
    sys.exit()
el = int(sys.argv[1]) or 1
f = [r["forward_sec"] for r in rows]
t = [r["total_sec"] for r in rows]
span = max(r["ts"] for r in rows) - min(r["ts"] for r in rows)
print(f"条数 {len(rows)}  前向中位 {statistics.median(f):.2f}s  p90 {sorted(f)[min(int(len(f)*0.9), len(f)-1)]:.2f}s  max {max(f):.2f}s")
print(f"含排队 total_sec 中位 {statistics.median(t):.1f}s  max {max(t):.1f}s")
print(f"有效吞吐 {len(rows)/el*60:.1f} 条/分钟"
      + (f"（按打分跨度 {len(rows)/span*60:.1f} 条/分钟）" if span > 30 else ""))
EOF

echo "=== 资源趋势（每 10 条取一个） ==="
awk 'NR % 10 == 1' /root/route_c_resources.jsonl 2>/dev/null | tail -6

echo "=== 最近 3 条打分 ==="
tail -3 /root/jev_cpu_metrics.jsonl 2>/dev/null

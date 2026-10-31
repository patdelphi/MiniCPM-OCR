#!/usr/bin/env bash
# 汇总 CPU shim 的前向指标：条数、单条前向耗时分布、含排队的总耗时、分数分布。
# 全量 57 页的路线 C 耗时预估只能建立在「单条前向到底多少秒」上，其它数都会骗人。
M=${1:-/root/jev_cpu_metrics.jsonl}
./.venv-vllm/bin/python - "$M" <<'EOF'
import json, sys, statistics
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
f = [r["forward_sec"] for r in rows]
t = [r["total_sec"] for r in rows]
q = [r["queue_depth"] for r in rows]
n = [r["n_tokens"] for r in rows]
yes = [1.0 / (1.0 + pow(2.718281828, -(r["probs"][1] - r["probs"][0]))) for r in rows]
print(f"条数 {len(rows)}  ok={sum(1 for r in rows)}")
print(f"n_tokens  min {min(n)}  中位 {statistics.median(n):.0f}  max {max(n)}")
print(f"forward_sec  min {min(f):.2f}  中位 {statistics.median(f):.2f}  p90 {sorted(f)[int(len(f)*0.9)] if len(f)>1 else 0:.2f}  max {max(f):.2f}  合计 {sum(f):.0f}s")
print(f"total_sec(含排队) 中位 {statistics.median(t):.2f}  max {max(t):.2f}  合计 {sum(t):.0f}s")
print(f"queue_depth 中位 {statistics.median(q):.1f}  max {max(q)}")
print(f"prob_yes   中位 {statistics.median(yes):.3f}  <0.9 {sum(1 for y in yes if y < 0.9)}  <0.5 {sum(1 for y in yes if y < 0.5)}")
# 单条前向与 token 数的关系，决定全量文档（平均块更长）该按几秒估
pairs = sorted(zip(n, f))
if len(pairs) > 2:
    import math
    xs = [math.log(a) for a, _ in pairs]; ys = [math.log(max(b, 1e-6)) for _, b in pairs]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    slope = sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / max(sum((x-mx)**2 for x in xs), 1e-9)
    print(f"log-log 斜率 {slope:.2f}（>1 说明注意力项占比高，长块成本超线性）")
EOF

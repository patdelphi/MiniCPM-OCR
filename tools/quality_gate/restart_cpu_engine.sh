#!/usr/bin/env bash
# 重启 CPU shim：换新进程清掉增长的 RSS 与客户端已断开但仍在排队的旧请求。
# MALLOC_ARENA_MAX=2 —— 16 个 OpenMP 线程各自建 glibc malloc arena 是 RSS 线性上涨的主因，
# 限成 2 个 arena 后由线程复用同一块堆，碎片增长受控。
set -uo pipefail
ROOT=/mnt/d/Github/MiniCPM-OCR
THREADS="${JEV_CPU_THREADS:-4}"
CONCURRENCY="${JEV_CPU_CONCURRENCY:-4}"

# 权重必须放在 ext4 上。放 /mnt/d 时 from_pretrained 是 mmap DrvFs，内存一紧这 4.2GB
# 的页就被驱逐，前向边缺页边算：实测同样一条 prompt 空闲 0.7 秒、和解析并发时 4.6~28 秒，
# 且线程全卡在缺页上（12 线程只吃到 1.5 个核）。
WEIGHTS="${JEV_WEIGHTS:-}"
if [ -z "$WEIGHTS" ]; then
    for c in /root/models/MiniCPM-OCR-Jev "$ROOT/models/MiniCPM-OCR-Jev"; do
        [ -f "$c/model.safetensors" ] && WEIGHTS="$c" && break
    done
fi
[ -n "$WEIGHTS" ] || { echo "[error] 找不到 Jev 权重目录" >&2; exit 1; }
echo "[cpu-engine] weights=$WEIGHTS threads=$THREADS"

pkill -f jev_cpu_engine 2>/dev/null
sleep 3
pgrep -f jev_cpu_engine > /dev/null && { echo "[warn] 旧进程未退出，强制"; pkill -9 -f jev_cpu_engine; sleep 2; }

mv /root/jev_cpu_metrics.jsonl "/root/jev_cpu_metrics_$(date +%H%M%S).jsonl" 2>/dev/null || true
: > /root/jev_cpu_metrics.jsonl

cd "$ROOT"
setsid nohup env MALLOC_ARENA_MAX=2 \
    JEV_WEIGHTS="$WEIGHTS" \
    JEV_PORT=18503 JEV_CPU_THREADS="$THREADS" JEV_CPU_CONCURRENCY="$CONCURRENCY" \
    JEV_CPU_METRICS=/root/jev_cpu_metrics.jsonl \
    NO_PROXY='*' no_proxy='*' \
    ./.venv-vllm/bin/python tools/quality_gate/jev_cpu_engine.py \
    > /root/jev_cpu_engine.log 2>&1 < /dev/null &

# 就绪窗口要留够：仓库和 venv 都在 /mnt/d（DrvFs，走 9P），主机重启后冷启动时
# 连 import 都要付 9P 往返的代价，实测能从 6 秒涨到几分钟。
for i in $(seq 1 100); do
    curl -s --noproxy '*' -m 3 http://127.0.0.1:18503/stats > /dev/null 2>&1 && break
    sleep 3
done
curl -s --noproxy '*' -m 5 http://127.0.0.1:18503/stats
echo
tail -3 /root/jev_cpu_engine.log

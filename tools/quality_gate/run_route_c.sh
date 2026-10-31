#!/usr/bin/env bash
# 路线 C 的入口：OCR 引擎在 GPU（18499），Jev 判别在 CPU shim（经 18500 渲染打分）。
#
#   usage: bash tools/quality_gate/run_route_c.sh <输入 PDF/图片> <输出 md> [额外 SDK 参数...]
#
# 例（先拿 2 页试跑）：
#   bash tools/quality_gate/run_route_c.sh in.pdf out.md --set input.limit=2
#
# 依赖两个已在跑的服务：
#   18499  vLLM 生成式 OCR 引擎
#   18500  渲染打分服务，JEV_BACKEND_URL 指向 18503 的 CPU shim
# 都检查过才继续，否则跑起来只会得到一堆 verdict=score_failed。
set -euo pipefail

INPUT="${1:?用法: run_route_c.sh <输入> <输出.md> [额外参数...]}"
OUTPUT="${2:?需要输出 md 路径}"
shift 2

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="$(cd "$HERE/../../sdk" && pwd)"
PROJECT_ROOT="$(cd "$HERE/../.." && pwd)"

VLLM_URL="${VLLM_URL:-http://127.0.0.1:18499}"
JEV_RM_URL="${JEV_RM_URL:-http://127.0.0.1:18500}"
PYTHON="${SDK_PYTHON:-$PROJECT_ROOT/.venv-sdk-gpu/bin/python}"

# 本机服务绝不能走 Clash：httpx 默认信任代理环境变量，会把 127.0.0.1 当成代理那头的地址
export NO_PROXY='*' no_proxy='*'
export OCR_ASSETS_DIR="${OCR_ASSETS_DIR:-$PROJECT_ROOT/models}"
export OCR_LAYOUT_MODEL_DIR="${OCR_LAYOUT_MODEL_DIR:-$OCR_ASSETS_DIR/PP-DocLayoutV3_infer}"
export OCR_UPRIGHT_DIR="${OCR_UPRIGHT_DIR:-$OCR_ASSETS_DIR/paddle_upright}"
export OCR_SERVED_NAME="${OCR_SERVED_NAME:-minicpm-ocr}"
export PYTHONPATH="$SDK_ROOT${PYTHONPATH:+:$PYTHONPATH}"

require_ok() {
    local name="$1" url="$2"
    if ! curl -sf --noproxy '*' -m 8 -o /dev/null "$url"; then
        echo "[error] $name 未就绪或不可达：$url" >&2
        exit 1
    fi
}
# vLLM 的 /health 回 200 空 body，只看状态码；打分服务的 /healthz 回 JSON，内容里必须带 served_model
require_ok "OCR 引擎" "${VLLM_URL%/v1}/health"
if ! curl -s --noproxy '*' -m 8 "$JEV_RM_URL/healthz" | grep -q '"served_model"'; then
    echo "[error] 渲染打分服务未挂上 Jev 引擎：$JEV_RM_URL/healthz" >&2
    exit 1
fi

[[ -x "$PYTHON" ]] || { echo "[error] 找不到 SDK 解释器：$PYTHON" >&2; exit 1; }

exec "$PYTHON" -m minicpmvocr parse "$INPUT" \
    --config "$HERE/configs/route_c.yaml" \
    -o "$OUTPUT" \
    --server-url "$VLLM_URL" \
    --model "$OCR_SERVED_NAME" \
    "$@"

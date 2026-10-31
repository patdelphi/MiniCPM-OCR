r"""Jev 的 CPU 前向引擎：对外表现得和一个 `vllm serve --runner pooling --convert classify` 一样。

用途：OCR 引擎独占 GPU 时把质量判别放到 CPU 上，省掉「停 OCR → 起 Jev」的 10~16 分钟切换。
它实现 `sdk/jev_score/backends/vllm_backend.py` 依赖的三个端点，打分服务只要把
`JEV_BACKEND_URL` 指过来，SDK 与 `sdk/backends/vllm.yaml` 都不用改：

    GET  /v1/models   -> {"data":[{"id": <served>}]}
    POST /classify    -> {"data":[{"probs":[p0,p1]}]}；use_activation=false 时给原始 logits
    POST /tokenize    -> {"tokens":[...]}，selfcheck 用它核对 prompt
    GET  /stats       -> 本进程自述（backend=pseudo，不是真 vLLM）

只用标准库 + torch/transformers：这个进程必须跑在 `.venv-vllm` 里（唯一同时有
torch 和 `Qwen3_5ForSequenceClassification` 的环境），而那个环境没有 fastapi，
装它属于改动别人建好的环境。所以这里用 `http.server`。

prompt 由本进程按权重自带的 chat_template.jinja 拼装（和 vLLM 一样属于「引擎拼 prompt」），
启动时把渲染结果与 `infer_vllm.build_prompt` 的指纹对齐，对不上就不起服务。

环境变量：
    JEV_WEIGHTS        权重目录，必填
    JEV_PORT           监听端口，默认 18503
    JEV_SERVED_NAME    对外模型名，默认 minicpm-ocr-jev-cpu
    JEV_CPU_THREADS    单条前向的 torch intra-op 线程数，默认 4
    JEV_CPU_CONCURRENCY 同时前向的条数，默认 4（两者相乘约等于占用的核数）
    JEV_CPU_METRICS    每次前向写一行的 JSONL，默认 /tmp/jev_cpu_metrics.jsonl

前向并发：信号量放行 CONCURRENCY 条同时前向，每条用 THREADS 个 intra-op 线程。
这么切是因为实测单条前向在线程数上早早饱和（bf16、187 token：4 线程 0.84 秒，8 线程 0.72 秒，
16 线程不再变快），把 16 个核拆成 4 条并行比 1 条独占 8 核划算得多。多进程试过——fork 出的子进程
继承已初始化的 OpenMP 状态会死锁，spawn 又要各进程重载 4.2GB 权重。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MATERIAL_ROOT = Path(os.environ.get("JEV_MATERIAL_ROOT",
                                    "/mnt/d/Github/MiniCPM-OCR/scripts/minicpm-ocr-jev"))
if str(MATERIAL_ROOT) not in sys.path:
    sys.path.insert(0, str(MATERIAL_ROOT))

WEIGHTS = os.environ.get("JEV_WEIGHTS", "").strip()
PORT = int(os.environ.get("JEV_PORT", "18503"))
SERVED = os.environ.get("JEV_SERVED_NAME", "minicpm-ocr-jev-cpu")
THREADS = int(os.environ.get("JEV_CPU_THREADS", "4"))
CONCURRENCY = int(os.environ.get("JEV_CPU_CONCURRENCY", "4"))
METRICS = os.environ.get("JEV_CPU_METRICS", "/tmp/jev_cpu_metrics.jsonl")

if not WEIGHTS or not Path(WEIGHTS).is_dir():
    raise SystemExit(f"JEV_WEIGHTS 必须指向存在的权重目录，实得 {WEIGHTS!r}")

import torch  # noqa: E402
from infer_vllm import (  # noqa: E402
    ASSISTANT_SUFFIX,
    IMAGE_PLACEHOLDER,
    IMAGES_PER_PROMPT,
    INSTRUCTION,
    PROMPT_TEMPLATE_MD5,
    build_prompt,
)
from transformers import (  # noqa: E402
    AutoModelForSequenceClassification,
    AutoProcessor,
)

_MODEL = None
_PROCESSOR = None
# 前向并发闸门：同时放行 CONCURRENCY 条，其余请求在信号量上排队。
# 计数只用于观测排队深度，不参与调度。
_GATE = threading.Semaphore(CONCURRENCY)
_QUEUE_DEPTH = 0
_DEPTH_LOCK = threading.Lock()
_PROMPT_MD5 = ""
_COUNTS = {"ok": 0, "error": 0}


def _b64_image(url: str):
    """data URL 或本地路径 -> RGB PIL.Image。"""
    from PIL import Image

    m = re.match(r"^data:image/[\w+-]+;base64,(.+)$", url, re.S)
    if not m:
        return Image.open(url).convert("RGB")
    return Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGB")


def _template_to_text(messages) -> str:
    """按权重自带模板渲染 messages，与 vLLM 的 /classify 走同一条拼装路径。"""
    return _PROCESSOR.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)


def _parts_to_messages(imgs, parts):
    """内容列表按 image, text, image, text 交错——顺序错了 prompt 就变了。"""
    return [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": imgs[0]}},
            {"type": "text", "text": parts[0]},
            {"type": "image_url", "image_url": {"url": imgs[1]}},
            {"type": "text", "text": parts[1]},
        ],
    }]


def _encode(text: str, images):
    return _PROCESSOR(text=[text], images=images, return_tensors="pt")


def _forward(text: str, imgs) -> dict:
    """一次前向，返回两个原始 logits 与过程数据。"""
    t0 = time.time()
    images = [_b64_image(u) for u in imgs]
    enc = _encode(text, images)
    n_tok = int(enc["input_ids"].shape[-1])
    with torch.inference_mode():
        out = _MODEL(**enc).logits[0]
    if out.shape[-1] != 2:
        raise RuntimeError(f"value head 应为 2 类，实得 {tuple(out.shape)}")
    return {"logits": [float(out[0]), float(out[1])], "n_tokens": n_tok,
            "forward_sec": time.time() - t0}


def _rss_gib() -> float:
    """本进程 RSS（GiB）。读 /proc，不用 ps：ps 要 fork，而这个进程 RSS 有好几个 GB，
    内存吃紧时 fork 会把 /stats 拖到几十秒不返回，而它正好是重启脚本的就绪探针。"""
    try:
        with open("/proc/self/statm", encoding="utf-8") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1073741824
    except (OSError, IndexError, ValueError):
        return -1.0


def _record(row: dict) -> None:
    try:
        with open(METRICS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[jev-cpu] 指标写入失败：{exc}", flush=True)


def _classify_logits(body: dict):
    """返回 vLLM 形态的 data 数组：use_activation 决定 probs 是概率还是原始 logits。"""
    messages = body["messages"]
    content = messages[0]["content"]
    imgs = [c["image_url"]["url"] for c in content if c.get("type") == "image_url"]
    if len(imgs) != IMAGES_PER_PROMPT:
        raise RuntimeError(f"该 checkpoint 每次需要 {IMAGES_PER_PROMPT} 张图，请求里有 {len(imgs)} 张")
    raw = _forward(_template_to_text(messages), imgs)
    logits = raw["logits"]
    # vLLM 的 /classify 里 use_activation 缺省走 pooler 默认（True，做 softmax），
    # 只有显式 false 才给原始 logits。这里必须一致，否则没写该字段的调用方会拿到
    # 两套不同量纲的数，且排序相同、机检发现不了。
    if body.get("use_activation", True):
        p = torch.softmax(torch.tensor(logits), dim=-1)
        probs = [float(p[0]), float(p[1])]
    else:
        probs = logits
    return probs, raw


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 默认把每条请求打到 stderr，压掉
        pass

    def _send(self, obj, code=200) -> None:
        buf = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(buf)))
        self.end_headers()
        self.wfile.write(buf)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n))

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 要求的命名
        global _PROMPT_MD5
        if self.path == "/v1/models":
            self._send({"object": "list",
                        "data": [{"id": SERVED, "object": "model", "created": 0,
                                  "owned_by": "jev-cpu"}]})
        elif self.path.startswith("/stats"):
            self._send({"backend": "pseudo", "true_vllm": False, "engine": "hf-cpu",
                        "served": SERVED, "prompt_md5": _PROMPT_MD5,
                        "threads": THREADS, "concurrency": CONCURRENCY,
                        "queue_depth": _QUEUE_DEPTH,
                        "ok": _COUNTS["ok"], "error": _COUNTS["error"],
                        "rss_gib": round(_rss_gib(), 2), "metrics": METRICS,
                        "weights": WEIGHTS})
        elif self.path.startswith("/health") or self.path == "/":
            self._send({"status": "ok", "backend": "pseudo", "engine": "hf-cpu"})
        else:
            self._send({"error": f"no route {self.path}"}, 404)

    def do_POST(self):  # noqa: N802
        global _QUEUE_DEPTH, _COUNTS
        try:
            body = self._read_body()
        except (ValueError, OSError) as exc:
            self._send({"error": f"请求体读取失败: {type(exc).__name__}: {exc}"}, 400)
            return

        if self.path == "/tokenize":
            try:
                content = body["messages"][0]["content"]
                imgs = [c["image_url"]["url"] for c in content
                        if c.get("type") == "image_url"]
                enc = _encode(_template_to_text(body["messages"]),
                              [_b64_image(u) for u in imgs])
                ids = [int(x) for x in enc["input_ids"][0]]
                self._send({"count": len(ids), "tokens": ids,
                            "last_token": {"id": ids[-1], "decoded": "<|im_end|>"}})
            except Exception as exc:
                self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if self.path != "/classify":
            self._send({"error": f"no route {self.path}"}, 404)
            return

        if body.get("model") and body["model"] != SERVED:
            self._send({"error": f"The model `{body['model']}` does not exist."}, 404)
            return

        with _DEPTH_LOCK:
            _QUEUE_DEPTH += 1
        t0 = time.time()
        try:
            with _GATE:  # 并发上限由信号量控制
                probs, raw = _classify_logits(body)
        except Exception as exc:
            with _DEPTH_LOCK:
                _QUEUE_DEPTH -= 1
                _COUNTS["error"] += 1
            # 失败也要落一行：崩之前那批 forward_error 回溯不到是哪条请求、等了多久，
            # 因为只累加了内存里的计数。
            _record({"ts": round(time.time(), 3), "error": f"{type(exc).__name__}: {exc}",
                     "total_sec": round(time.time() - t0, 3), "queue_depth": _QUEUE_DEPTH,
                     "served_model": SERVED, "prompt_md5": _PROMPT_MD5})
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        with _DEPTH_LOCK:
            _QUEUE_DEPTH -= 1
            _COUNTS["ok"] += 1

        total = time.time() - t0
        _record({"ts": round(time.time(), 3), "n_tokens": raw["n_tokens"],
                 "forward_sec": round(raw["forward_sec"], 3),
                 "total_sec": round(total, 3), "queue_depth": _QUEUE_DEPTH,
                 "served_model": SERVED, "prompt_md5": _PROMPT_MD5,
                 "use_activation": bool(body.get("use_activation", True)),
                 "probs": [round(x, 6) for x in probs]})
        self._send({"id": f"jev-cpu-{int(time.time() * 1000)}", "object": "list",
                    "created": int(time.time()), "model": SERVED,
                    "data": [{"index": 0, "label": None, "probs": probs,
                              "num_classes": len(probs)}],
                    "usage": {"prompt_tokens": raw["n_tokens"],
                              "total_tokens": raw["n_tokens"]}})


def _check_prompt() -> None:
    """把引擎渲染出的 prompt 与训练指纹对齐，不一致直接拒绝启动。"""
    global _PROMPT_MD5
    probe = "JEV_PROBE_TEXT"
    rendered = _template_to_text(_parts_to_messages(
        ["data:image/png;base64,", "data:image/png;base64,"],
        [f"\nOCR文本：\n{probe}\n", "\n" + INSTRUCTION]))
    mine = hashlib.md5(rendered.replace(probe, "{text}").encode("utf-8")).hexdigest()
    ref = hashlib.md5(build_prompt("{text}").encode("utf-8")).hexdigest()
    if mine != ref or mine != PROMPT_TEMPLATE_MD5:
        raise SystemExit(
            "引擎渲染的 prompt 与训练 prompt 指纹不一致，拒绝对外提供服务：\n"
            f"  template={mine}\n  build_prompt={ref}\n  发布指纹={PROMPT_TEMPLATE_MD5}")
    _PROMPT_MD5 = mine
    print(f"[jev-cpu] prompt md5 对齐 {mine} "
          f"tail={ASSISTANT_SUFFIX!r} tail_token=271", flush=True)


def main() -> None:
    global _MODEL, _PROCESSOR
    torch.set_num_threads(THREADS)
    t0 = time.time()
    _PROCESSOR = AutoProcessor.from_pretrained(WEIGHTS)
    _MODEL = AutoModelForSequenceClassification.from_pretrained(
        WEIGHTS, dtype=torch.bfloat16, num_labels=2, low_cpu_mem_usage=True)
    _MODEL.eval()
    print(f"[jev-cpu] 权重加载 {time.time() - t0:.1f}s threads={THREADS} "
          f"RSS={_rss_gib():.1f}GiB weights={WEIGHTS}", flush=True)
    _check_prompt()
    # 预热一次：首条前向要走算子惰性初始化，实测比稳态慢一个数量级（24 秒对 0.8 秒）。
    # 不预热的话第一条真实请求会顶住整条管线的并发闸门。
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, format="PNG")
    warm = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    tw = time.time()
    warm_text = _template_to_text(_parts_to_messages(
        [warm, warm], ["\nOCR文本：\nwarmup\n", "\n" + INSTRUCTION]))
    _forward(warm_text, [warm, warm])  # 串行一次：把 4.2GB 权重页全部缺页进来
    seq = time.time() - tw
    # 再按并发数各来一次：每个线程的 OpenMP 池和算子惰性初始化是各自的，
    # 不预热的话第一批真实请求会互相抢着付这份钱。
    ts = time.time()
    pool = [threading.Thread(target=_forward, args=(warm_text, [warm, warm]))
            for _ in range(CONCURRENCY)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    print(f"[jev-cpu] 预热 串行 {seq:.1f}s，{CONCURRENCY} 路并发 {time.time() - ts:.1f}s，"
          f"RSS={_rss_gib():.1f}GiB", flush=True)
    print(f"[jev-cpu] 监听 0.0.0.0:{PORT}，对外模型名 {SERVED}，指标写 {METRICS}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

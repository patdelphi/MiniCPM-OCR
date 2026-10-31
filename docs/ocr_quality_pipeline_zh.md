# 面向「合格质量」的 OCR 生产管线设计

适用：本机 WSL2 + RTX 5070 Ti 12GB，MiniCPM-OCR（解析）+ MiniCPM-OCR Jev（质量判别）。
数据依据：`runs/jev_pipeline_20261030/`，57 页 / 517 版面块 / 448 个含文本块逐块打分。

## 1. 「质量合格」的定义

不接受"看起来还行"。一条文档输出满足全部条件才算合格：

| 项 | 判据 | 依据 |
|---|---|---|
| 逐块可追溯 | 每个文本块都带 `prob_yes`、`verdict`、来源裁图路径 | Jev 输出即为此三元组 |
| 首轮通过率 | 可重试类块中 `prob_yes ≥ 0.90` 的比例 ≥ 95%（修正后统计） | 0.90 是这份 PDF 的分布拐点，需在带标注样本上重新标定 |
| 零静默丢弃 | 修正后仍 < 0.90 的块必须进复核清单，且不出现在最终 Markdown 的正文里（以 `> [待复核]` 引用块保留） | 目标，当前未生效：三次全量跑（`route_c_full4`、`route_c_demo1`、`route_c_tau050`）的成品 md 里 `待复核` 标记均为 0 处，低分块与合格块混排无区别。缺的是 P3 的 `build_review.py`；在此之前只有 `low_score_blocks.csv` 这类旁路清单可用 |
| 图形类不产文本 | `chart` / `image` / `header_image` / `footer_image` 只保留图片与图注 | 目标，当前未生效：SDK 默认 `markdown_assembly` 只丢页眉页脚页码脚注类，`chart_format` 是 `{content}`，所以实测 `chart` 6 块文本全部进了 merged.md（中位 0.000）。要落实这条，在配置里把 `chart` 加进 `prompt.markdown_assembly.ignore_labels`（该段支持增量合并），目前未做，因为同时会丢掉图注文本，需客户确认 |
| 严格不劣化 | 修正轮只在新候选分数**高于**原分数时替换 | 与 `_tts.py` 的 `_best_scored` 语义一致 |

## 2. 实测分流（448 块）

| label | n | 中位 | <0.5 | 处理 |
|---|---|---|---|---|
| text | 290 | 0.893 | 73 | 进修正环 |
| paragraph_title | 85 | 0.971 | 13 | 进修正环 |
| table | 15 | 0.890 | 5 | 进修正环（OTSL→HTML 后打分） |
| footer / header / figure_title / vision_footnote | 45 | 0.97~0.996 | 2 | 进修正环，命中少 |
| content（目录页） | 4 | 0.016 | 4 | 直送复核，属版面/阅读顺序问题 |
| chart | 6 | 0.000 | 6 | 文本仍写进成品（6/6 在 merged.md 里，实测），只能靠 0.000 分送审 |
| algorithm | 1 | 0.375 | 1 | 进修正环 |
| display_formula | 1 | 0.980 | 0 | KaTeX 预检 + 修正环 |

阈值扫描（同一批 448 块）：`τ=0.90` 首轮通过 258（57.6%），`τ=0.95` 通过 226（50.4%），`τ=0.98` 通过 179（40.0%）。

## 3. 管线阶段

```
L0 方向纠正   upright（当前 yaml: enabled=false，建议开，gate_min_side=500 / gate_min_lines=6）
L1 版面切分   PP-DocLayoutV3 → 块（label, bbox, order, crop）
L2 首轮识别   贪心单候选（temperature 0.01 / top_k 1）
L3 首轮判别   Jev ternary：原块图 + 候选文本 + KaTeX 渲染图 → prob_yes
L4 分流       ≥0.90 采纳；页眉页脚页码脚注类不写入成品；其余进修正队列
L5 修正识别   队列内每块最多 2 轮：轮1 = 裁图 LANCZOS scale 2.0 + 贪心；轮2 = 原尺度采样 n=4, temperature 0.7
L6 修正判别   重渲染 + 重打分，与新候选一并取最高分；仍 <0.90 → needs_human
L7 复核产物   review.html（裁图 / 渲染图 / 文本 三栏，按分数升序）+ queue.jsonl（人工改文本可回填）
L8 合成       merged.md（含图片链接、公式、表格 HTML）+ gate.json（门禁结果，机器可读）
```

L5/L6 只处理约 190 块，且每块最多 5 次识别请求，比全文档开 best-of-N 便宜得多。

写入成品时按 SDK 的 `markdown_assembly` 默认规则（`sdk/minicpmvocr/prompts.py:47-57`）：`number`/`footnote`/`header`/`header_image`/`footer`/`footer_image`/`aside_text` 整块丢弃，`chart`/`table`/`seal` 按 `{content}` 原样写入。所以 chart 被引擎读成表格时，那段文本会进 `merged.md`，L4 不做拦截，只能靠分数把它顶到送审队列（demo1 实测 6 块，分数 0.0001~0.0003）。

## 4. 使用模式与引擎调度

### 4.1 三种使用模式（都保留，可单独交付）

| 模式 | 入口 | 依赖服务 | 产物 | 状态 |
|---|---|---|---|---|
| 只用 OCR（解析，不判别） | `python -m minicpmvocr parse <pdf> --set tts.enabled=false` | 仅 :18499 | `results_json/*.json`、`merged.md`、裁图 | 已实测（`runs/minicpmvocr_parse_block_*`） |
| 只用 Jev（给已有识别结果打分/复核） | `scripts/minicpm-ocr-jev/rm_render.py`（预检+渲染）→ `eval/score.py --dataset <jsonl> --checkpoint <权重> --out <pred>` → `eval/evaluate.py`（AUROC） | 权重本地，可 CPU/GPU 单跑，不需要 OCR 在线 | `predictions.jsonl`、`scores.csv` | 已实测（`runs/jev_pipeline_20261030/scores.csv`） |
| 联合在线（解析 + 判别 + 定向修正一趟完成） | `tools/quality_gate/run_route_c.sh` / `run_route_a.sh` | :18499 + :18500（后端指 :18503 或 :18502） | 上述全部 + 逐块分数、轮次、低分清单 | 已实测 57 页两路线 |

单独用 Jev 的口径要注意：`eval/score.py` 的输入 jsonl 每行必须给 `image_path`（原块裁图）、`render_image_path`（候选文本的渲染图）、`ocr_text`、`category` 四项，渲染图必须由 `rm_render.py` 的 KaTeX + Chromium 产出，自己拼 HTML 或用其它字体环境出图，分数含义就变了（同一份权重对渲染漂移敏感，见第 7 节）。

三种模式的入口都是仓库里的 `.sh`。Windows 上 git 默认 `core.autocrlf=true`，检出即 CRLF，`bash` 会报 `$'\r': command not found`；跑任何一条之前先按 `docs/deployment-usage.md` 第 4 节整目录统一行尾。仓库里存的是 LF，坏的只是本机检出副本。

联合在线模式下 L1~L6 由 SDK 原生 `tts` + `tts.adaptive` 完成，不需要外置脚本；只用 OCR 时 L3~L6 整段不发生，产物里没有逐块分数，质量门禁无从判起。

### 4.2 三条路线

Jev 与 OCR 是两个前向实例，本机只有 12227 MiB 显存。当前 OCR 单实例 `--gpu-memory-utilization 0.60` 实测占 8353 MiB。权重下限：OCR 2.5GB + Jev 4.2GB = 6.7GB，再加两份 CUDA 上下文与各自 KV 池。

三条路线都保留，按机器条件选：

| | A：双 GPU 引擎共存 | B：批处理两趟 | C：OCR 在 GPU + Jev 在 CPU |
|---|---|---|---|
| 显存 | 两份 vLLM：OCR util 0.38 + Jev util 0.53，实测峰值 11620 / 12227 MiB | 一份，来回切换 | 一份（OCR 不动） |
| 内存 | 无新增常驻 | 无新增 | Jev 常驻 4.3GiB |
| 切换成本 | 0 | 每次 10~16 分钟 | 0 |
| 打分延迟 | 单块 score_best 中位 0.83s，其中 GPU 前向 0.08s、渲染 0.74s | 0.30s/块（独占时） | 单条前向中位 2.83s，含排队 10.5s（57 页实测） |
| 修正环 | SDK `tts` + `tts.adaptive` 在线跑 | SDK `tts` 用不上，需外置脚本 | 同 A，SDK 原生路径 |
| 适合 | 12GB 单卡即可，但要接受显存余量只剩 0.6GB | 显存不够、可离线批处理 | 12GB 显存 + ≥16 核 CPU |
| 状态 | **已实测 57 页 263 / 267 秒两次**（`runs/route_a_full1`、`route_a_full2`） | 已实测切换耗时 | 已实测 57 页 738 秒（`runs/route_c_full4`） |

**A（在线单趟）**：两引擎同卡共存，SDK 的 `tts` + `tts.adaptive` 在解析过程中直接完成 L4~L6，零切换开销。已在本机 12GB 上跑通，57 页 263 秒，比路线 C 的 738 秒快 2.8 倍，质量参数一位没动。

额度不能按设计里的 0.42 / 0.42 平分，两引擎的显存下限差一倍：

- Jev 下限 6.09 GiB（权重 4.18 GiB + 激活约 1.8 GiB）。util 0.42 时 KV 算出来是 **-1.17 GiB**，引擎在初始化阶段直接拒绝；关掉 CUDA graph（`--enforce-eager`）只省 0.2 GiB，`--max-num-batched-tokens 4096` 再把激活压下来。最终 Jev 用 util 0.53 + enforce-eager，实测常驻 5440 MiB，KV 0.39 GiB（19797 token）。
- OCR 下限 3.50 GiB（权重 2.47 GiB）。最终 util 0.38，KV 0.78 GiB（56941 token）。
- 判别前向只占单块打分的 0.08 秒，其余 0.74 秒是 Chromium 渲染；渲染才是 A 路线打分段的成本主体（57 页合计：渲染 1043s / 前向 191s）。

两个必须写进部署说明的坑：

- `--max-model-len 16384` 下 OCR 起不来：混合架构的 Mamba 状态缓存在 0.38 额度里只有 233 个 block，低于默认 `max_num_seqs 256`，引擎在捕 CUDA graph 阶段报 `max_num_seqs (256) exceeds available Mamba cache blocks (233)`。加 `--max-num-seqs 192` 解决，192 远在 16 个槽位的需求之上，不改识别行为。
- 上下文长度不要压到 8192：8 页冒烟里第 2 轮放大裁图（scale 2.0）有 1 块被 400 拒掉，那是参数降格造成的质量损失，不是路线 A 的固有代价。16384 与 8192 的 KV 池大小相同（按 token 分配），16384 只多要求单请求 KV 下限 262 MiB。

判据回看：无 OOM、无吞吐衰减——共存后 OCR 单块推理中位从路线 C 的 11.84 秒降到 3.90 秒（CPU 上的 Jev 前向撤走之后，vLLM 的多模态预处理不再抢核），路线 A 成立。

**B（批处理两趟）**：共存失败就用。实测每次切换 OCR→Jev 15.5 分钟、Jev→OCR 10 分钟。整条管线控制在两次切换内：解析+切 Jev+打分 → 切 OCR+修正 → 切 Jev+复评，固定成本约 30 分钟。

**C（OCR 留 GPU，Jev 走 CPU）**：判别是纯前向、只需尾部两个 logit，不走自回归解码，CPU 上量级可用。实现为 `tools/quality_gate/jev_cpu_engine.py`，一个暴露 `/classify`、`/tokenize`、`/v1/models` 的 HF transformers 前向服务，接口与 vLLM 版同构，所以 18500 打分服务和 SDK 的 `tts.rm_url` 一行配置就能指过去，不改 SDK。

- 分数忠实度：与本机 GPU 同输入对比，偏差在 1e-4 量级（`text` 0.00243 vs 0.00234、`table` 0.00099 vs 0.00096），排序不变。
- 打分延迟（57 页全量实测，`runs/route_c_full4`）：612 条前向，单条中位 2.83 秒（prompt 中位 236 token）、p90 4.71 秒、最长一条 93 秒。长度是主要成本：>1000 token 的 28 条只占 4.6% 条数，吃掉 30% 前向时间。
- 判别是路线 C 的硬约束：612 条前向合计 2594 秒，占满 738 秒墙钟里 4 路闸门的 88%。GPU 侧同期峰值只有 45% 利用率。已试过的加速都不成立——线程×并发全扫描下 4 线程×4 并发就是峰值（2.57 条/s 空闲基准），8×4 与 4×8 更差；把闸门提到 6 路，8 页冒烟墙钟 213→215 秒不变，单条反而从 2.77 涨到 3.45 秒；fp32 比 bf16 慢 2.3 倍；候选批处理在 611 行里只有 163 行能凑批。
- 与解析时间可重叠（SDK 在块级并发调用），12GB 上这是唯一让解析与判别同时在线的方向。反方向不行：OCR 是生成式、内存带宽受限，57 页 CPU 要约 20~55 分钟，GPU 48 秒。
- 其余 CPU 路线不可用：vLLM 的 `feat-ocr` 分支是 CUDA build；GGUF 转换脚本只在 NVIDIA/Metal 验过；本机 WSL 无 `ollama` CLI。HF transformers 里已有 `Qwen3_5ForSequenceClassification`，是唯一现成路径。

已测的互相干扰：同一批 187 token 的输入，判别服务独占时单条 0.75 秒，与解析、Chromium 渲染并发时中位 2.83 秒，慢 3.8 倍；抢的是内存带宽和核，不是显存。反方向影响很小——判别满载期间 GPU 利用率峰值 45%，OCR 推理没被拖慢。`RM_RENDER_BROWSERS`（当前 2 个浏览器 × 8 页）是渲染侧的并发上限。

## 5. 成本预估（57 页文档）

| 段 | 量 | A：双 GPU 共存（57 页全量实测） | C：Jev 走 CPU（57 页全量实测） |
|---|---|---|---|
| L1+L2 首轮解析 | 57 页 | 与打分同趟交错，OCR 推理合计 2770s / 448 块 | 与打分同趟进行，推理合计 10384s / 448 块 |
| L3 首轮打分 | 448 块 | 判别合计 1441s（渲染 1043 + 前向 191） | 判别合计 9687s（前向 6877） |
| L5 修正识别 | ~190 块 × ≤5 次 | 采样 808s，72 块走到多候选 | 采样 482s，同 72 块 |
| L6 复评 | ~190 块 | 与 L5 同趟，共 611 条前向 | 与 L5 同趟，共 612 条前向 |
| 合计 | | **263 秒 / 267 秒（两次）**，57 页全 ok | **738 秒（12.3 分钟）**，57 页全 ok、0 失败块 |

A 与 C 的质量口径相同（同一份配置，只差判别后端在 GPU 还是 CPU）：平均轮次 1.85 / 1.85，改写率 8.3% / 8.5%，首轮+修正达标率 58.5% / 58.5%，最终分中位 0.957 / 0.960，≥0.90 58.6% / 58.7%，<0.5 21.7% / 21.5%。A 自己两次复跑的差（≥0.90 58.6% vs 59.2%）与 A-C 之差同量级，说明差异来自第 2、3 轮采样抖动，不是路线。产物文本 49/57 页逐字相同，其余 8 页各 1 块不同。

B 路线在 A 的基础上再加约 30 分钟切换成本。

C 路线的关键点不是总时长，而是解析与判别同时在线：SDK 的 `tts`/`tts.adaptive` 原生路径可用，不需要外置批处理脚本，也不需要 15 分钟量级的引擎切换。738 秒里 CPU 判别占 88%（612 条前向合计 2594 秒 / 4 路闸门），所以再往下只有两条路——少打分（砍 `tts.adaptive` 的第 2、3 轮，实测这两轮把 38/448 块的答案改掉，其中第 3 轮只贡献 7 块）或把判别挪回 GPU。后者已实测就是路线 A，263 秒；前者不减质量所以不采用。

提速的两个前提改动（`tools/quality_gate/configs/route_c.yaml`，实测 880→738 秒，52/57 页产物逐字不变）：

- `pipeline.max_workers` 6→16：SDK 的块槽位是"推理→判别"串行的，6 槽时 GPU 平均占用两成、CPU 判别三成，96.7% 的块时间在排队，瓶颈是槽位数不是算力。
- `engine.request_ready_timeout_sec` 600→120：放大裁图被 vLLM 以 400 确定性拒绝，而 SDK 的重试闸门（`sdk/minicpmvocr/backends/client.py:214-226`）会一路重试到超时才放弃，两个块各白占 602 秒槽位。这是 SDK 缺陷，配置绕过，未改代码。

### 5.1 路线 C 在客户上传文档上的实测（demo1.pdf，13 页）

同一份 `configs/route_c.yaml`，质量参数一位未动，输入换成客户给的 `demo1.pdf`（13 页 / 337KB，水文学期刊论文，5 张表 + 6 个图 + 7 个公式）。产物 `runs/route_c_demo1/`。

| 指标 | demo1（13 页） | 57 页基准 full4 |
|---|---|---|
| 总耗时 | 386 秒，退出码 0 | 738 秒 |
| 页 / 块 | 13 页全 ok / 171 块 | 57 页全 ok / 448 块 |
| 出分覆盖 | 170/171（99.4%） | 448/448 |
| 分数中位 / ≥0.90 / <0.5 | 0.983 / 80.6% / 17 块 | 0.960 / 58.7% / 21.5% |
| 平均轮次 | 1.40（1 轮 136 块、3 轮 34 块） | 1.85 |
| 修正轮 | 抬分 4 块、降分 0 块 | 改写率 8.5%、降分 0 块 |
| CPU 前向 | 220 条，单条中位 3.11s，合计 1242s | 612 条，合计 2594s |
| 显存峰值 / load1 峰值 | 8389 MiB / 15.59 | 同量级 |
| 每页秒数 / 块密度 | 29.7 秒、13.2 块/页 | 12.9 秒、9.1 块/页 |

每页更慢是文档类型差异，不是路线退化：demo1 的图表块占比高，`chart`/`table` 块的 prompt 更长（p95 1794 token，最大 3009），第 2、3 轮触发更频繁。

门禁在这份文档上抓得比基准更干净：33 个 <0.90 块里 chart 6、vision_footnote 6、text 8、display_formula 4、reference_content 4、table 3、header 1、footnote 1；6 个 chart 块分数全落在 0.0001~0.0003，对应引擎把图的坐标轴读成等差数列的幻觉表格（例如 "Flow percentile" 表 10 行清一色 0.95）。正文与参考文献几乎全数 ≥0.90，即低分不是均匀散布的噪声，而是集中在图表/脚注/公式三类。

成品 markdown 的两处口径说明：表格以 HTML `<table>` 落盘（OTSL→HTML 是打分前的必要转换，成品沿用），不是 `| ---` 管道表；图片引用 `![image](path)` 只在块标签为 `image` 且带 `image_path` 时由 `sdk/minicpmvocr/postprocess/markdown.py:289-293` 输出，demo1 的图全部被判成 `chart`，所以 0 处引用，57 页基准同一逻辑产出 58 处。

### 5.2 tau 降档对照（0.90 → 0.50，57 页同输入）

结论：不降。省 33 秒，代价是把 4 块真实字符错留在产物里。

| 指标 | tau=0.90（`runs/route_c_full4`） | tau=0.50（`runs/route_c_tau050`） |
|---|---|---|
| 总耗时 | 738 秒 | 705 秒（−33 秒，−4.5%） |
| 平均轮次 | 1.84 | 1.46 |
| 轮次分布 | 1 轮 256、2 轮 5、3 轮 185 | 1 轮 341、2 轮 6、3 轮 99 |
| CPU 前向 | 612 条 / 2594 秒 | 585 条 / 2431 秒 |
| 分数中位 / ≥0.90 / <0.5 | 0.960 / 58.7% / 21.5% | 0.959 / 58.7% / 21.3% |
| 517 块文本差异 | 参照 | 12 块（2.3%） |

头条指标一位没动，因为修正大多落在 0.90 以下，达标率这个口径看不见它；逐块差集才是真实收益。首轮分在 [0.5,0.9) 的 85 块里，tau 降到 0.50 会少修 6 块，其中 4 块是真错。

| 块 | tau=0.90 终态 | tau=0.50 终态 | 分数 | 性质 |
|---|---|---|---|---|
| page_001::005 | `AI Scientist 的现在和未来` | `A Scientist 的现在和未来` | 0.820 / 0.755 | 真错：丢字母 |
| page_001::008 | `• AI framework accurately…` | `- AI framework…` | 0.878 / 0.611 | 真错：项目符号 |
| page_029::003 | `• Healthcare` | `- Healthcare` | 0.885 / 0.659 | 真错：项目符号 |
| page_029::024 | `• Observational Data` | `- Observational Data` | 0.858 / 0.596 | 真错：项目符号 |
| page_036::002 | `1,780 个工具间…` | `1,780个工具间…` | 0.992 / 0.831 | 空格 |
| page_023::000 | `a_n^{1/2^n}` | `a_{n}^{1/2^{n}}` | 0.828 / 0.754 | 风格，两种都对 |

`page_014::009` 的 0.886 → 0.071 与 tau 无关：该块首轮分 0.002，两边都跑满 3 轮 5 候选，差异是同一处错字（"空韵"/"空萌"）的采样抖动。对照跑日志里的 2 次失败是已知的放大裁图 400 死等（各 120 秒），基线同样存在。

省得少的原因：第 1 轮打分占判别墙钟 74.6%，而 [0.5,0.9) 带 85 块里有 72 块三轮跑完唯一候选数仍是 1（模型重采样给不出不同答案），这些块的第 2、3 轮只花 GPU 重采样、不产生前向调用，所以砍掉整个带的修正环，前向只从 612 降到 585。

## 6. 代码改动范围

不改 SDK。路线 A/C 走 SDK 原生 `tts` + `tts.adaptive`，两条都只需要配置：

- `configs/route_a.yaml` + `run_route_a.sh`（A 路线，已实测）：与 `route_c.yaml` 只差判别后端在 GPU，质量参数（三轮、tau 0.90、采样）逐项相同；入口在起跑前检查 18499、18502、18500 三个服务都在线
- 起服务的额度要手工指定，`sdk/webui/scripts/start_vllm.sh` 的默认值（util 0.60 / max-len 16384）放不下第二个引擎：OCR 用 `VLLM_GPU_UTIL=0.38` 另加 `--max-num-seqs 192`（该脚本没有这个开关，实测时直起 `vllm.entrypoints.openai.api_server`），Jev 用 `GPU_MEM_UTIL=0.53` 另加 `--enforce-eager --max-num-batched-tokens 4096`（同样直起 `vllm serve`，`sdk/start_engine.sh` 没有这两个开关）
- `jev_cpu_engine.py`（C 路线，已写）：HF transformers 前向 + `/classify` `/tokenize` `/v1/models`，接口与 vLLM 版同构；启动时按 `PROMPT_TEMPLATE_MD5` 校验 prompt，不一致拒服务；`JEV_CPU_WORKERS` 用 fork 池共享只读权重页

路线 B 下 `tts` 用不上，需要三个外置脚本，放在 `tools/quality_gate/`：

- `score_blocks.py`：读 WebUI/SDK 的 `results_json` + 裁图 → 调 `/v1/rm/score_best` → 出 `scores.jsonl` / `scores.csv`（已有原型，固化）
- `refine_blocks.py`：读低分队列 → 用 `sdk/minicpmvocr/backends/client.py:142 recognize()` 按轮次表重识别（复用 `prompts.get_element_cfg`，不复制 prompt）→ 重渲染重打分 → 严格不劣化替换
- `build_review.py`：出 `review.html`、`queue.jsonl`、`gate.json`，人工修正可回填进 `merged.md`

配置：`tools/quality_gate/configs/route_c.yaml`（`tts.adaptive` 三轮：贪心 → scale 2.0 采样 n=4 → 原尺度采样 n=4，`tau 0.90`、`n_max 3`）。

验证方式：examples 三 case 的 CPU/GPU 分数对比（断言偏差 <1e-3 且排序一致）+ 端到端断言（分数单调不降、`chart` 类不产文本、`gate.json` 与逐块分数一致）。

## 7. 已知未解问题

- 0.90 / 0.5 两个阈值来自单份文档的分布形状，没有人工标注做对照，存在把"识别正确但渲染字体不同"判成低质的可能。20 块分层人工核对支持这个判断：0.5~0.9 带 5 块抽样全部是文本正确的误报（含 1 块源文件本身重字、1 块裁图切断），真错只出现在 <0.5 带（`ls`→`Is`、少一个引号、孤字接错行）。同一次核对也测了 tau 降到 0.50 的代价，见第 5.2 节：省 33 秒、少修 4 块真字符错，不划算，所以 tau 维持 0.90。
- 两个考核口径本身有盲区，别拿它们当质量结论：`≥0.90 达标率` 看不见落在 0.90 以下的修正（tau 对照里 6 块被改对的块，只有 1 块跨过 0.90）；`content != raw_content` 不能当"改写次数"用，因为表格块的 `raw_content` 是引擎原文 OTSL、`content` 是打分前转成的 HTML，表示差异会同时掩盖真改写和制造假改写。要量修正收益，只能拿两次跑的终态文本做差集。
- 字体环境未 pin（`renderer/check_renderer.py` 报告 1 块渲染漂移 10.98%），分数在小数点后第二位可能浮动，影响 0.90 边界附近的分流数量。
- scale 2.0 轮的放大裁图会把 prompt 顶过 `--max-model-len`，vLLM 回 400，而这类拒绝重试多少次都不会成功；SDK 的重试闸门（`sdk/minicpmvocr/backends/client.py:214-226`）却会一路重试到 `request_ready_timeout_sec` 才放弃，full3 里两个块各白占 602 秒。现用 120 秒绕过（两个块改成各 122 秒），根治要区分"确定性拒绝"与"可恢复失败"，属于 SDK 代码改动，本管线范围内不动。
- `chart`/`content` 类块的正确处理属于版面理解问题，本管线不做修复；但也不丢弃——`chart`/`table` 的识别文本会按 `{content}` 写进成品（见第 3 节），低质只能靠分数顶到送审队列。
- 路线 A 的显存余量只剩 0.6~1.2 GB（57 页两次实测峰值 11008 / 11620 MiB，卡 12227）。这个余量要同时容纳布局模型（PaddleOCR `device=cuda` 实测约 0.8 GB）与图像更密集的文档，换材料前需要重测峰值；顶不满就得回退布局到 CPU 或走路线 C。
- 路线 A 的 OCR KV 池只有 0.78 GiB（56941 token），单条 16384 请求的并发上限 3.48 倍。57 页这份材料实测没有排队（单块推理中位 3.90 秒，比路线 C 的 11.84 秒还快，因为 CPU 上的判别前向撤走以后 vLLM 的多模态预处理不再抢核），但更长文档或更高槽位下的表现没有数据。
- Jev 用 `--enforce-eager` 省 0.2 GiB 显存，代价是没上 CUDA graph。实测单块前向 0.08 秒，不构成瓶颈，所以没有回头补测开启 graph 的版本（那样额度要提到 0.56 以上，OCR 就要再让显存）。

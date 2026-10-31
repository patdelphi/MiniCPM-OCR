# todo · OCR 质量管线落地

范围：把「解析 → 判别 → 定向修正 → 复核 → 合成」固化为可重复运行的管线，产出带逐块分数、通过质量门禁的 OCR 结果。
执行路线：给客户保留 A/B/C 三种选择（见 `docs/ocr_quality_pipeline_zh.md` 第 4 节）。**客户路线已定为 C**（OCR 在 GPU、Jev 判别在 CPU）；A 实测更快（263 秒 vs 738 秒），但显存余量只剩 0.6~1.2 GB，2026-10-31 用户决定放弃 A、固定 C，A 的数据作为对照留档。
不含：SDK 代码改动、字体 pin、git 操作、生产部署。

## 已完成

### PC 路线 C 落地（本机主路线）
- `tools/quality_gate/jev_cpu_engine.py`：CPU 上的 `/classify` 伪引擎，标准库 HTTP，不装新依赖
- `tools/quality_gate/run_route_c.sh`：带服务就绪检查的路线 C 入口
- `tools/quality_gate/configs/route_c.yaml`：`tts` + `tts.adaptive` 三轮（贪心 → scale 2.0 采样 n=4 → 原尺度采样 n=4），tau 0.90
- 校验：SDK 启动闸门 `run_selfcheck` 走 shim → `passed=true`、`token_equality=equal`、`last_token=271`、`forward_shape="2 logits"`
- 分数一致性（examples 三 case，CPU vs GPU 同输入）：偏差 0.00009 / 0.00116 / 0.00003，材料自带容差 0.02 → 通过
- 2 页试跑闭环：18 个文本块全部经 CPU 判别，adaptive 平均 1.78 轮、首轮+修正后通过率 61%（11/18），`rm_changed` 2 块
- 57 页全量（`runs/route_c_full4`，16 槽位 + 快速失败）：738 秒，57 页全 ok、0 失败块；448 块、612 条判别前向；分数中位 0.960、≥0.90 占 58.7%、改写率 8.5%；与 880 秒的 full3 相比 52/57 页产物逐字相同
- 天花板在 CPU 判别：612 条前向合计 2594 秒，占 4 路闸门 88% 时间，GPU 同期峰值才 45% 利用率。已排除的加速：线程×并发扫描（4×4 就是峰值 2.57 条/s）、闸门提到 6（8 页冒烟 213→215 秒，单条从 2.77 涨到 3.45 秒）、fp32（比 bf16 慢 2.3 倍）、候选批处理（只有 163/611 行能凑批，1.3 倍单行效率）
- 踩过的坑：fork 出的多进程 worker 继承 OpenMP 状态直接死锁（worker 零 CPU 时间、load 0.05），所以前向改为单飞；`http_proxy` 会让本机回环请求走 Clash，必须 `NO_PROXY='*'`

### P4 端到端全量（路线 C，57 页）已完成
- 基准产物 `runs/route_c_full4/`（738 秒）；`route_c_full3/` 是改动前的 880 秒对照，`route_c_full/`、`route_c_full2/` 为失败尝试留档
- 过程数据：`runs/route_c_full4/per_page_metrics.csv`、`runtime/{crop,page,layout}_timing.jsonl`、`runtime/tts_candidates.jsonl`（逐轮候选与耗时）、`/root/jev_cpu_metrics_full{3,4}.jsonl`（shim 逐条前向）、`/root/rc4_resources.jsonl`（每 5 秒显存/利用率/内存/负载采样）
- 对比脚本 `.tmp_compare_runs.py`（分数分布 + 逐页文本相似度）、瓶颈探针 `.tmp_probe_stall.py`、`.tmp_probe_slot_math.py`、`.tmp_probe_rounds.py`
- 完成标准已核对：57 页全出分、逐块 verdict 与分数落盘、资源采样与打分指标时间轴对齐、总耗时与设计文档第 5 节对比（预估 30~50 分钟，实测 12.3 分钟）

### P0 路线 A 共存可行性（57 页全量）已完成，结论：成立
- 额度不能平分。0.42/0.42 失败：Jev 显存下限实测 6.09 GiB（权重 4.18 + 激活 1.8），util 0.42 算出 KV **-1.17 GiB** 直接拒起；OCR 下限只有 3.50 GiB。最终 OCR util 0.38（KV 0.78 GiB / 56941 tok）+ Jev util 0.53（KV 0.39 GiB / 19797 tok）
- Jev 靠 `--enforce-eager`（省 0.2 GiB）+ `--max-num-batched-tokens 4096` 压进额度；OCR 在 16384 上下文下需要 `--max-num-seqs 192`，否则 Mamba 状态缓存只有 233 block，低于默认 256，捕 graph 阶段拒起
- 通路校验：18500 打分服务 `JEV_BACKEND_URL` 指回 18502，启动闸门 `passed=true`、`source=engine`、`last_token=271`、`forward_shape="2 logits"`；20 个真实块走通 `score_best`，单块中位 0.83 秒（GPU 前向 0.08 秒 + 渲染 0.74 秒），与 `route_c_full4` 同块同文本分数中位偏差 0.0035
- 57 页全量两次：`runs/route_a_full1` 263 秒、`route_a_full2` 267 秒（路线 C full4 为 738 秒，快 2.8 倍）；57 页全 ok、显存峰值 11008 / 11620 MiB
- 质量守住：三轮 adaptive 与 tau 0.90 一位未动。轮次 1.85 / 1.83、改写率 8.3% / 9.2%、达标率 58.5% / 59.2%、分数中位 0.957 / 0.959、≥0.90 58.6% / 59.2%，与 C 的 1.85 / 8.5% / 58.5% / 0.960 / 58.7% 同量级；产物文本 49/57 页逐字相同，其余 8 页各 1 块差异
- 吞吐没有衰减，反而更好：OCR 单块推理中位从 C 的 11.84 秒降到 3.90 秒——CPU 上的判别前向撤走后，vLLM 的多模态预处理不再抢核
- 新增 `tools/quality_gate/configs/route_a.yaml` + `run_route_a.sh`（起跑前检查 18499 / 18502 / 18500）；两引擎的额度与非默认开关无法用现成启动脚本表达，实测用直起命令，已写进设计文档第 6 节
- 判据回看：无 OOM、无 >40% 衰减 ⇒ A 成立，按约定不再重试其他切分

### P6 客户上传文档上的路线 C 验证（demo1.pdf，13 页）已完成
- 输入 `demo1.pdf`（13 页 / 337KB，水文学期刊论文，含 5 张真表 + 6 个图 + 7 个公式），产物 `runs/route_c_demo1/`，配置与 57 页基准逐项一致（`configs/route_c.yaml`，质量参数未动）
- 端到端 386 秒、退出码 0、SDK 启动闸门通过；13/13 页全出分、0 失败块、0 上下文长度拒绝、0 判别失败
- 质量：171 块 / 出分 170（99.4%）、分数中位 0.983、≥0.90 占 80.6%（137 块）、<0.90 33 块、<0.5 17 块；平均轮次 1.40（1 轮 136 块、3 轮 34 块）；修正轮抬分 4 块、降分 0 块
- CPU 判别：220 条前向、单条中位 3.11 秒、p90 9.28 秒、合计 1242 秒；prompt token 中位 318、p95 1794、最大 3009
- 资源：显存峰值 8389 MiB（额度内）、GPU 利用率峰值 97%、内存最低可用 10293 MiB、load1 峰值 15.59
- 每页 29.7 秒 vs 基准 12.9 秒，块密度 13.2/页 vs 基准 9.1/页；demo1 的 chart/table 占比高，正是 CPU 前向 prompt 更长、轮次更多的类型
- 门禁抓得准：6 个 chart 块分数全部在 0.0001~0.0003（引擎把图的坐标轴读成了等差数列表格，属幻觉），与 3 个 table、6 个 vision_footnote、4 个 display_formula 一起进入 33 块送审队列——低分块集中在图表/脚注/公式，正文与参考文献基本 ≥0.90
- 两个报表疑点已排除：`升/降分` 方向是 `.tmp_routeC_demo1_report.py:40-41` 变量名写反（`finals` 取 max，按定义不可能低于 `firsts`），真实为抬分 4、降分 0；成品 md 的表格是 HTML `<table>`（OTSL→HTML），不是 `| ---` 管道表，5 表 + 6 图转写全在；图片引用 0 处是因为本文档没有 `image` 标签块（图被判成 `chart`），`![image]()` 只在 `image_render=reference` 且块有 `image_path` 时输出，57 页基准里同一逻辑产出 58 处，非缺陷
- 低分块清单 `runs/route_c_demo1/low_score_blocks.csv`（33 块，UTF-8 BOM，含 page/label/首轮分/终分/轮次/裁图文件名/内容前 200 字），可直接作为 P3 review 队列的输入
- 产物：`runs/route_c_demo1/{results_json/*.json, output/demo1_page_*.md, route_c_demo1.md, summary.json, per_page_metrics.csv, runtime/{tts_candidates,crop_timing}.jsonl}`；过程数据 `/root/rcd_resources.jsonl`、`/root/jev_cpu_metrics_demo1.jsonl`；脚本 `.tmp_routeC_restore.sh`（切回 C 形态）、`.tmp_routeC_demo1.sh`、`.tmp_routeC_demo1_report.py`

### P7 tau 降档对照（0.90 → 0.50，57 页同输入）已完成，结论：不降
- 入口 `.tmp_routeC_tau050.sh`（`run_route_c.sh` + `--set tts.adaptive.tau=0.50`，其余质量参数一位未动），产物 `runs/route_c_tau050/`，对比脚本 `.tmp_compare_tau.py`
- 实测：705 秒 vs 基线 738 秒（省 33 秒 / 4.5%）；平均轮次 1.84→1.46（1 轮 256→341、3 轮 185→99）；CPU 前向 612→585 条 / 2594→2431 秒；分数中位 0.960→0.959、≥0.90 58.7% 不变、<0.5 21.5%→21.3%；517 块里文本差异 12 块
- 误伤确认：首轮分在 [0.5,0.9) 的 85 块少修 6 块，其中 4 块是真错——`AI Scientist` 丢字母成 `A Scientist`（0.755）、`•` 项目符号错成 `-` 三块（0.611/0.659/0.596）；另 2 块是空格与公式花括号风格
- `page_014::009` 的 0.886→0.071 与 tau 无关（首轮分 0.002，两边都跑满 3 轮 5 候选），是"空韵/空萌"同一处错字的采样抖动；日志 2 次失败是已知的放大裁图 400 死等各 120 秒，基线同样存在
- 省得少的原因：第 1 轮打分占判别墙钟 74.6%，而该带 85 块里 72 块三轮跑完唯一候选数仍是 1，第 2、3 轮只花 GPU 重采样、不产生前向调用
- 更正本轮早前先前的两个错判：（1）“0.5~0.9 带全是误报”不成立，当时用 `content != raw_content` 数改写，表格块 raw 是 OTSL、content 是 HTML，表示差异掩盖了真改写；（2）20 块抽样在 85 块里命中 6 个修正块概率太低。量修正收益只能拿两次跑的终态文本做差集
- tau 保持 0.90，配置未改

### P8 三种使用模式固化到文档已完成
- 设计文档 §4 改为「使用模式与引擎调度」，新增 §4.1 表：只用 OCR（`--set tts.enabled=false`，仅 :18499）、只用 Jev（`rm_render.py` 预检+渲染 → `eval/score.py` 批量出 prob_1 → `eval/evaluate.py` 出 AUROC，不需要 OCR 在线）、联合在线（`run_route_c.sh` / `run_route_a.sh`）
- 只用 Jev 的口径写进文档：输入 jsonl 每行必须给 `image_path`/`render_image_path`/`ocr_text`/`category`，渲染图必须由 `rm_render.py` 的 KaTeX+Chromium 产出，否则分数含义变了
- 同步修正文档三处与实测不符的描述：§2 chart 行原写“只留图不产文本”，实为 6/6 块文本进了 merged.md；§3 L4 原写“图形类丢弃文本”，实为 `markdown_assembly` 默认只丢页眉页脚页码脚注类（`sdk/minicpmvocr/prompts.py:47-57`）；§7 对应条目改写，并新增两条考核口径盲区的说明

## 待做

### P3 复核产物与门禁
- `tools/quality_gate/build_review.py`：`review.html`（裁图/渲染/文本三栏）、`queue.jsonl`、`gate.json`
- 完成标准：低分块 100% 出现在 review 中；`chart`/`image` 类无文本泄漏；`gate.json` 指标可由脚本重算校验
- 口径修正（demo1 实测）：`chart` 类块会带 HTML 表格文本（引擎把图读成表），这是送审内容不是泄漏；review 的判据应是"低分 + 类型标注"，`image` 类才要求无文本

### P1 判别脚本固化
- `tools/quality_gate/score_blocks.py`：由 `results_json` + 裁图产出 `scores.jsonl` / `scores.csv`
- 完成标准：在全量跑的产物上复现逐块分数，与 `metrics.jsonl` 一致

### P2 分流与修正的独立脚本化
- 现状：路线 C 下修正环由 SDK 的 `tts.adaptive` 在线完成，不需要外置脚本
- 待做：仅当客户选路线 B（两趟批处理）时才需要 `refine_blocks.py`
- 完成标准：修正后 ≥0.90 比例相对首轮提升，逐块记录替换前后分数，无一块下降

### P5（可选，需另行确认）
- 阈值在带标注样本上标定；路线对比写入 `docs/deployment-usage.md` 供客户选择
- tau 一侧已有数据：0.50 对照实测省 33 秒、少修 4 块真错（见 P7），0.90 与 0.5 两个阈值的标定仍需要标注集
- 新增待确认项：是否落实「图形类不产文本」——把 `chart` 加进 `prompt.markdown_assembly.ignore_labels` 即可生效，但会同时丢掉图注文本，需客户定

## 风险
- 阈值未经标注校准，修正提升幅度只能作相对比较
- 路线 C 的 CPU 前向与 Chromium 渲染抢核：独占时 0.75 秒的输入，并发下中位 2.83 秒（3.8 倍），抢的是内存带宽与核；闸门从 4 提到 6 只会让每条更慢，聚合吞吐不变
- 路线 A 的显存余量只剩 0.6~1.2 GB（峰值 11008 / 11620，卡 12227），里面还要放布局模型约 0.8 GB。换更长或图像更密的材料前必须重测峰值，顶不满就把布局降到 CPU 或回退路线 C
- 路线 A 的 OCR KV 池 0.78 GiB，16384 单请求并发上限 3.48 倍；这份 57 页没有排队，更高槽位或更长文档没有数据

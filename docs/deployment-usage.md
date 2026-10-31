# MiniCPM-OCR 部署与使用说明

本机（Windows 11 + WSL2 + RTX 5070 12GB）已完成的部署，命令与路径均实测通过。
项目路径：Windows `D:\Github\MiniCPM-OCR`，WSL 内 `/mnt/d/Github/MiniCPM-OCR`。

## 1. 组成

| 部件 | 地址 | 说明 |
|---|---|---|
| vLLM 推理服务 | `http://127.0.0.1:18499` | feat-ocr 源码编译版，OpenAI 兼容接口 |
| WebUI | `http://127.0.0.1:8950` | 纯标准库 http.server，浏览器直接访问 |
| 版面分析 | 进程内 | PP-DocLayoutV3（PaddleX，占用 GPU） |
| 旋转摆正 | 进程内 | PP-LCNet 文档方向 + 行方向 + 检测模型 |

调用链：上传文件 → 版面检测切块 → 每个块裁图送 vLLM 识别 → 汇总为 Markdown。
PDF 由 `pypdfium2` 先渲染成页面图片，再走同一条链路。

## 2. 环境要求

- WSL2 Ubuntu 24.04，Python 3.12.3，uv（`/root/.local/bin/uv`）
- NVIDIA 驱动 617.42（torch cu130 要求 ≥ 580）
- WSL 内存：`C:\Users\patde\.wslconfig`

  ```ini
  [wsl2]
  memory=24GB
  swap=16GB
  networkingMode=mirrored
  ```

  默认 7GB 会在编译 vLLM 时把进程杀掉，且表现为所有 `wsl` 命令超时。改完执行 `wsl --shutdown` 生效。
- 编译依赖：`apt-get install -y libdw-dev libssl-dev gcc-13 g++-13`
- 磁盘：模型 2.7GB + 两套虚拟环境 17GB + `sources/` 源码
- 代理：mirrored 网络下用 `http://127.0.0.1:7890`；模型权重走 ModelScope 时可关掉代理

## 3. 目录清单

```
models/MiniCPM-OCR/            主模型 BF16 2.5GB（ModelScope OpenBMB/MiniCPM-OCR-1.0）
models/PP-DocLayoutV3_infer/   版面模型 inference.pdiparams/.json/.yml
models/paddle_upright/         3 个摆正子模型，各 config.json/inference.json/inference.pdiparams/inference.yml
sources/vllm/                  feat-ocr vLLM 源码（pin 18343393b），editable 安装指向这里
sources/transformers/          feat-ocr transformers 源码（pin 549e5b41），同上
.venv-vllm/                    推理服务环境
.venv-sdk-gpu/                 SDK + 版面 + WebUI 环境
sdk/webui/data/                vllm.log / webui.log / jobs/ 解析历史
```

`sources/` 不能删，两个虚拟环境里是 editable 安装，删了就等于服务报废。

## 4. 启动

```bash
wsl -d Ubuntu
cd /mnt/d/Github/MiniCPM-OCR
bash sdk/webui/scripts/start_vllm.sh      # 冷启动约 15 分钟
bash sdk/webui/scripts/start_webui.sh     # 秒级
```

判断服务是否就绪：

```bash
curl http://127.0.0.1:18499/v1/models          # 返回 minicpm-ocr 即就绪
curl http://127.0.0.1:8950/api/health          # engine.reachable=true 且 models.layout=true
```

日志：`sdk/webui/data/vllm.log`、`sdk/webui/data/webui.log`。
冷启动慢的原因：权重在 `/mnt/d`（9P 文件系统）上，且首次要跑 torch.compile；第二次启动会快很多。

## 5. 停止

```bash
wsl -d Ubuntu pkill -f vllm.entrypoints
wsl -d Ubuntu pkill -f ocrweb-server
```

## 6. 局域网访问

两个服务都绑定 `0.0.0.0`（WSL 内 `ss -tlnp` 可见 `0.0.0.0:18499`、`0.0.0.0:8950`），mirrored 网络下对外就是 Windows 宿主机端口，同网段设备直接访问：

```
WebUI   http://192.168.71.158:8950
API     http://192.168.71.158:18499/v1/...
```

Windows 防火墙默认阻止入站，需要放行（管理员终端，已在本机执行并验证）：

```bat
netsh advfirewall firewall add rule name="MiniCPM-OCR WebUI" dir=in action=allow protocol=TCP localport=8950 profile=private
netsh advfirewall firewall add rule name="MiniCPM-OCR vLLM" dir=in action=allow protocol=TCP localport=18499 profile=private
```

用完删除：

```bat
netsh advfirewall firewall delete rule name="MiniCPM-OCR WebUI"
netsh advfirewall firewall delete rule name="MiniCPM-OCR vLLM"
```

排查提示：

- 在 Windows 上用本机局域网 IP 自测会失败（mirrored 模式不做回环发卡），必须从另一台设备验证
- 规则只放行 Private；网络切到 Public 后需改 `profile=any` 或重新连接为私有网络
- 手机连不上而电脑能通，查路由器是否开了 AP 隔离/访客网络
- 若放行后仍不通，检查 Hyper-V 防火墙：`Get-NetFirewallHyperVVMSetting | Select-Object Name,DefaultInboundAction`

两个服务都没有鉴权，vLLM 也没有 API key。局域网内任何人都能上传文件触发解析并占用 GPU，建议只在可信网络开放。

## 7. 使用方式

### 7.1 WebUI

浏览器打开 http://127.0.0.1:8950（本机），或按第 6 节从局域网设备访问。

- 左侧：拖拽或点击上传，支持 PDF（多页）/ PNG / JPG / JPEG / BMP / WEBP / TIF；勾选「旋转摆正预处理」
- 中间：页面缩略图、版面框、块序号、仅显示 VLM 块、缩放
- 右侧：渲染 / 源码 / 逐块三种视图，可「合并跨页表格」，复制或下载 `.md`
- 历史：左侧列表保存每次解析结果，点击可回看，`×` 删除

接口（供脚本调用）：

```
GET    /api/health                     引擎与模型状态
POST   /api/jobs                       multipart 上传，字段 files、upright、name
GET    /api/jobs                       任务列表
GET    /api/jobs/{id}                  任务状态：queued/loading_models/running/done/failed
GET    /api/jobs/{id}/result           结构化结果（版面块 + 文本）
GET    /api/jobs/{id}/markdown.md      合并后的 Markdown
GET    /api/jobs/{id}/page/{i}.png     页面图
GET    /api/jobs/{id}/crop/{i}/{name}  单块裁图
```

### 7.2 直接调 vLLM 接口

单图识别（表格例）：

```bash
curl http://127.0.0.1:18499/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "minicpm-ocr",
  "messages": [{"role":"user","content":[
    {"type":"image_url","image_url":{"url":"data:image/png;base64,<B64>"}},
    {"type":"text","text":"请识别图中的表格并输出 OTSL"}]}],
  "max_tokens": 512,
  "temperature": 0,
  "chat_template_kwargs": {"enable_thinking": false}
}'
```

两点注意：

- `enable_thinking: false` 必须带，否则输出可能为空或乱码
- 直接调接口拿到的是 `<otsl>` 原始表格标记，转成 `<table>` 要走 SDK 或 WebUI

### 7.3 SDK 命令行

```bash
cd /mnt/d/Github/MiniCPM-OCR
source sdk/scripts/demo_env.sh webui
.venv-sdk-gpu/bin/minicpmvocr parse 输入.png -o 输出.md \
  --server-url http://127.0.0.1:18499 --model minicpm-ocr
```

实测公式与文本混排页输出正确保留了 LaTeX：

```
(a) $0 \in S;$
(b) If $x \in S$ then $x + 1 \in S$ and $x - 1 \in S$; and
```

其他子命令：`parse-layout`、`prepare`、`infer`、`run`、`show-config`、`list-backends`、`serve-layout`。
除 `parse` 外都需要 `--config` 并用 `--set input.file_path=...` 指定输入。

## 8. 可调参数

| 变量 | 默认 | 作用 |
|---|---|---|
| `VLLM_PORT` | 18499 | 推理服务端口 |
| `VLLM_GPU_UTIL` | 0.60 | 显存占比，12GB 卡上 0.60≈7.2GB |
| `VLLM_MAX_MODEL_LEN` | 16384 | 上下文长度，调高会多吃显存 |
| `VLLM_LIMIT_MM` | `{"image":36,"video":0}` | 单请求图片上限，`video:0` 不可去掉 |
| `PORT` | 8950 | WebUI 端口 |
| `VLLM_URL` | `http://127.0.0.1:18499` | WebUI 连接的推理地址 |
| `OCR_SERVED_NAME` | minicpm-ocr | 需与 `--served-model-name` 一致 |
| `OCR_MODEL_PATH` | `models/MiniCPM-OCR` | 主模型目录 |
| `OCR_LAYOUT_MODEL_DIR` | `models/PP-DocLayoutV3_infer` | 版面模型目录 |
| `OCR_UPRIGHT_DIR` | `models/paddle_upright` | 摆正模型目录 |

多卡时版面检测默认用 GPU 0，可 `--set layout.device=cuda:N` 指定。

## 9. 本机实测数据

| 项目 | 数值 |
|---|---|
| vLLM 冷启动 | 约 15 分钟（含权重加载与编译预热） |
| 运行显存 | 8.3GB / 12GB |
| 单张表格图 API | 6.4s，74 tokens |
| 单页图片完整解析（版面+摆正+识别） | 11.4s |
| vLLM 源码编译 | 146 分钟，`MAX_JOBS=4`，峰值内存约 6GB |

## 10. 常见问题

**脚本报 `set: pipefail: invalid option name`**
Windows 检出带 CRLF。`sed -i 's/\r$//' sdk/webui/scripts/*.sh sdk/scripts/demo_env.sh`

**启动报 `unexpected keyword argument 'adaptive_tile_upsize'`**
固定版本 vLLM 在视频预热时传了配套 transformers 不接受的参数。用 `--limit-mm-per-prompt '{"image":36,"video":0}'` 关掉视频输入，无需重编译。已在 `start_vllm.sh` 里设为默认。

**带图请求返回 400**
缺 `--limit-mm-per-prompt` 或 `--trust-request-mm-kwargs`；后者缺失时 SDK 会静默重试到 600 秒超时，看起来像卡死。

**任务停在 `loading_models` 后失败，报 `paddle_upright/... is not exists`**
摆正模型没下载。补 `models/paddle_upright/` 三个模型目录，或上传时取消「旋转摆正预处理」。

**编译到后期报 `elfutils/libdwfl.h: No such file or directory`**
缺 `libdw-dev`。uv 失败时会清空构建目录，重装依赖后要重编全部约 600 个目标。

**WSL 所有命令 `WSAETIMEDOUT`，但 `wsl -l -v` 显示 Running**
内存耗尽，虚拟机卡死。按第 2 节配 `.wslconfig`，`wsl --shutdown` 后重启。

**`transformers serve` 起不来**
进程在但不监听端口、日志为空。`minicpm_ocr` 的 serve 入口当前不可用，推理服务只能用 vLLM。

**想用 Docker 或 Ollama**
都不通。Ollama 库里没有该模型；预构建 vLLM 镜像不含 `minicpm_ocr` 架构，且国内镜像 `docker.1ms.run` 返回 Cloudflare 验证页导致拉取失败。

## 11. 重新编译 vLLM

仅在升级 `sources/vllm` 或换 torch 版本时需要。

```bash
wsl -d Ubuntu
/opt/vllm-build/run.sh          # 后台持久运行，日志 /opt/vllm-build/build.log
```

脚本内容：设代理、`OCR_LIBCUDA=/usr/lib/wsl/lib/libcuda.so`、`MAX_JOBS=4`，然后 `bash scripts/install.sh vllm`。
用 `setsid nohup` 启动，否则会话结束进程会被带走。

## 12. 备份与恢复要点

- 换机或重装 WSL 后，`models/` 和 `sources/` 保留即可免下载，但两个 `.venv-*` 需重建
- 重建顺序：`bash scripts/install.sh sdk-gpu` → `bash scripts/install.sh vllm`（约 2.5 小时）
- 不要手工用 tarball 替换 `sources/`，`install.sh` 会校验它必须是独立 git 检出且 commit 与 `pyproject.toml` 一致

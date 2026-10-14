# 本地 AI 视频工程化指南

> slug: `ai-video-guide`

> 用开源模型 + 自己的算力，把 AI 视频生产做成**能批量跑的流水线**，而不是在 ComfyUI 里一个镜头一个镜头手动点。

这是一套面向「要规模化、要自动化、要可控」的 AI 视频工程化工具与笔记。
配套视频讲解见 👉 **[B站主页 / 视频链接]**（发布后替换）。

---

## 这是给谁的

- ✅ 想**自建 / 私有化**本地 AI 视频流水线的技术人（程序员、折腾党）
- ✅ 需要**批量产出**几十上百个镜头、受不了手动点的人
- ❌ 只偶尔做三五条片子的——直接用可灵 / 即梦这类付费工具更快，这套不适合你

付费工具解决「做一条」；这套解决「做一千条还不用手动」。

---

## 目录

| 工具 / 文档 | 说明 |
|---|---|
| [docs/comfyui-batch-runner.md](docs/comfyui-batch-runner.md) | **CSV 驱动的 ComfyUI 批量出片脚本**：一行一个镜头，自动注入工作流节点、排队、下载 |
| scripts/batch_h3.py | 上面文档对应的脚本本体（MiniMax H3 T2V+R2V 混跑） |
| examples/shots_example.csv | 一个可参考的镜头 CSV 样例 |

> 后续会陆续补充：配音批量（中英文 TTS 分流）、提示词结构化写法等。

---

## 快速开始

```bash
pip install requests

# 可选：同机跑 ComfyUI 时可配本地直拷目录加速；跨机留空即可（走 HTTP 下载）
export COMFY_HOST="http://127.0.0.1:8188"
# export COMFY_OUTPUT_DIR="/your/ComfyUI/output"

python3 scripts/batch_h3.py --csv examples/shots_example.csv --dry-run   # 先空跑验证
python3 scripts/batch_h3.py --csv examples/shots_example.csv             # 实跑
```

详细说明见 [docs/comfyui-batch-runner.md](docs/comfyui-batch-runner.md)。

---

## 说明

- 模型全部为开源模型（本仓库不含任何模型权重）。
- 跑在你自己的 / 租用的算力上，**不是**普通笔记本能直接跑的，请自行评估显卡。
- 工作流模板 JSON 与各人的 ComfyUI 搭建强绑定，请按文档**导出你自己的** workflow（API 格式）并对照节点映射填好节点号。

MIT License · 欢迎按需修改。

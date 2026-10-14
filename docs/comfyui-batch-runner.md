# ComfyUI 批量出片脚本（CSV 驱动）

把「在 ComfyUI 界面上一个镜头一个镜头手动点」变成「写一张 CSV 表、一行命令、过夜自动跑完」。

对应脚本：[../scripts/batch_h3.py](../scripts/batch_h3.py)
示例 CSV：[../examples/shots_example.csv](../examples/shots_example.csv)

---

## 一、核心原理：ComfyUI 的工作流其实是一张 JSON 图

你平时在 ComfyUI 界面上拖的节点，底层是一个 JSON（叫 **API 格式的 workflow**），每个节点有一个编号（key），比如 `"92"`、`"136"`。每个节点的参数都挂在 `inputs` 里。

所以「批量」的本质就是：

```
读 CSV 的每一行  →  把这一行的参数（提示词/尺寸/种子/定妆照…）
                    塞进 workflow JSON 对应节点的 inputs 里
                 →  POST 给 ComfyUI 的 /prompt 排队
                 →  轮询 /history 等它跑完
                 →  从 /view 把生成的视频下载下来
```

一句话：**CSV 一行 → 自动注入 ComfyUI 节点 → 排队出片。** 不用开界面，不用手点。

---

## 二、第一步：导出你自己的 workflow（API 格式）

节点编号**因人而异**，取决于你自己搭的工作流，所以不能直接用别人的。你要导出自己的：

1. 在 ComfyUI 里把你的出片工作流搭好、能正常出一个镜头
2. 打开设置，启用 **Enable Dev mode Options**
3. 菜单里点 **Save (API Format)**，得到一个 `xxx.json`
4. 这个 JSON 就是脚本要注入的模板

本脚本用了两个模板：
- `h3_t2v.json`——文生视频（无固定角色的空镜）
- `h3_r2v.json`——参考图生视频（挂定妆照锁人物）

把你导出的 JSON 命名好，放到 `scripts/templates/` 下（或用 `--t2v-template` / `--r2v-template` 指定路径）。

---

## 三、第二步：找到并核对节点编号

打开导出的 JSON，找到你要注入参数的节点，把编号填到脚本顶部的「节点 KEY」区。脚本里当前的映射是（**你需要按自己的 JSON 改**）：

**T2V（`h3_t2v.json`）**

| 脚本常量 | 节点号 | 节点类型 | 注入什么 |
|---|---|---|---|
| `T_SAVE` | `92` | SaveVideo | `filename_prefix` |
| `T_MAIN` | `105:104` | MiniMaxH3ImageToVideo | `prompt` / `width` / `height` |
| `T_DURATION` | `105:111` | PrimitiveFloat | 时长 `value` |
| `T_NOISE` | `105:15` | RandomNoise | `noise_seed` |

**R2V（`h3_r2v.json`）**

| 脚本常量 | 节点号 | 节点类型 | 注入什么 |
|---|---|---|---|
| `R_SAVE` | `92` | SaveVideo | `filename_prefix` |
| `R_R2V` | `136` | MiniMaxH3ReferenceToVideo | 尺寸 / `length` / `ref_image_size` |
| `R_PROMPT` | `138` | PrimitiveStringMultiline | 提示词 |
| `R_IMAGE` | `137` | LoadImage | 定妆照 `<Picture 1>` |
| `R_SCHEDULER` | `124` | BasicScheduler | `scheduler` |
| `R_NOISE` | `129` | RandomNoise | `noise_seed` |
| `R_DURATION` | `132` | PrimitiveFloat | 时长 |

> 怎么确认节点号？在 JSON 里搜 `class_type`（如 `"SaveVideo"`），它外层的那个 key 就是节点号。

---

## 四、第三步：写镜头 CSV

一行 = 一个镜头。列：

```
shot_id, type, ref_image, filename_prefix, seed, duration, width, height, ref_image_size, scheduler, positive_prompt
```

| 列 | 说明 |
|---|---|
| `shot_id` | 镜头编号，如 `S01`（`--only` 按它筛选） |
| `type` | `t2v`（空镜）或 `r2v`（有角色，挂定妆照） |
| `ref_image` | 仅 r2v：定妆照文件名（放 ComfyUI 的 `input/` 下）；支持 `a.png,b.png` 双图 |
| `filename_prefix` | 输出文件名前缀 |
| `seed` | 随机种子（失败重试时脚本会自动换） |
| `duration` | 秒，默认 5 |
| `width` / `height` | 默认 720 × 1280（竖屏） |
| `ref_image_size` | 仅 r2v：`max`（角色更稳）/ `match`（更快） |
| `scheduler` | 仅 r2v：推荐 `beta` |
| `positive_prompt` | 提示词正文 |

样例见 [../examples/shots_example.csv](../examples/shots_example.csv)。

---

## 五、运行

```bash
pip install requests

# 空跑：只生成 workflow JSON、不提交，先验证 CSV 和节点映射对不对
python3 scripts/batch_h3.py --csv examples/shots_example.csv --dry-run

# 实跑全部
python3 scripts/batch_h3.py --csv examples/shots_example.csv

# 只跑指定镜头
python3 scripts/batch_h3.py --csv examples/shots_example.csv --only S01,S03

# 只跑某一类
python3 scripts/batch_h3.py --csv examples/shots_example.csv --type t2v

# 指定远程 ComfyUI / GPU（--gpu N 自动映射端口 8188+N）
export COMFY_HOST="http://你的服务器:8188"
python3 scripts/batch_h3.py --csv examples/shots_example.csv --gpu 1
```

产物：
- `out_h3/`——每个镜头实际提交的 workflow JSON（排错用）
- `downloaded_videos_h3/`——下载好的成片

两个目录都建在 CSV 同级。

---

## 六、脚本替你处理掉的几件麻烦事

- **失败自动重试**：出错自动换 seed 重试（`--max-retries`，默认 2 次）
- **显存管理**：t2v / r2v 模型切换时自动 `POST /free` 卸载，避免两套模型同时占显存爆掉
- **断点续跑**：失败的镜头会在结尾打印出来，直接 `--only` 重跑那几个
- **本地直拷加速**：同机跑时设 `COMFY_OUTPUT_DIR` 可跳过 HTTP 直接拷贝文件

---

## 七、把它当成一个"调度器"，而不是出片工具

这套脚本本身不懂 AI、不出图——它只是个**调度器**：把结构化的表格喂给 ComfyUI 的 API。
所以同样的思路可以套到任何 ComfyUI 工作流上（出图、放大、换脸…），只要：导出 API JSON → 找到要改的节点号 → 从 CSV 注入。这才是「把 AI 工作流工程化」的核心。

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMax H3 通用批量脚本 — T2V + R2V 混跑

CSV 格式（shots_mixed_h3.csv）：
  shot_id, type, ref_image, filename_prefix, seed, duration,
  width, height, ref_image_size, scheduler, positive_prompt

  type = "t2v" → 使用 h3_t2v.json 模板，fl2va 模型
  type = "r2v" → 使用 h3_r2v.json 模板，ref2va 模型

节点映射：
  T2V（h3_t2v.json）
    92        SaveVideo                → inputs["filename_prefix"]
    105:104   MiniMaxH3ImageToVideo   → inputs["prompt"] / width / height
    105:111   PrimitiveFloat(duration)→ inputs["value"]
    105:15    RandomNoise             → inputs["noise_seed"]

  R2V（h3_r2v.json）
    92    SaveVideo                   → inputs["filename_prefix"]
    136   MiniMaxH3ReferenceToVideo   → ref_image_size / width / height / length
    138   PrimitiveStringMultiline    → inputs["value"] = prompt
    137   LoadImage (ref_image_0)     → inputs["image"] = <Picture 1>
    124   BasicScheduler              → inputs["scheduler"]
    129   RandomNoise                 → inputs["noise_seed"]
    132   PrimitiveFloat (duration)   → inputs["value"]

用法：
  python3 batch_h3.py --csv /path/to/shots_mixed_h3.csv
  python3 batch_h3.py --csv /path/to/shots_mixed_h3.csv --gpu 1
  python3 batch_h3.py --csv /path/to/shots_mixed_h3.csv --only S01,S06,S21
  python3 batch_h3.py --csv /path/to/shots_mixed_h3.csv --type t2v
  python3 batch_h3.py --csv /path/to/shots_mixed_h3.csv --dry-run

  out_h3/ 和 downloaded_videos_h3/ 会创建在 CSV 同目录下。

依赖：pip install requests
"""

import argparse
import csv
import copy
import json
import os
import random
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ 缺少 requests：pip install requests"); sys.exit(1)

# ── 路径配置 ──────────────────────────────────────────────────────────────────
SCRIPT_DIR            = Path(__file__).resolve().parent
TEMPLATE_DIR          = SCRIPT_DIR / "templates"
DEFAULT_T2V_TEMPLATE  = TEMPLATE_DIR / "h3_t2v.json"
DEFAULT_R2V_TEMPLATE  = TEMPLATE_DIR / "h3_r2v.json"
# 可选：若脚本与 ComfyUI 跑在同一台机器，填 ComfyUI 的 output 目录可走本地直拷（更快）；
# 留空（或不设 COMFY_OUTPUT_DIR 环境变量）则统一走 HTTP /view 下载。
DEFAULT_COMFY_OUTPUT  = os.environ.get("COMFY_OUTPUT_DIR", "")
DEFAULT_HOST          = os.environ.get("COMFY_HOST", "http://127.0.0.1:8188")

# ── 节点 KEY ──────────────────────────────────────────────────────────────────
# T2V
T_SAVE     = "92"
T_MAIN     = "105:104"
T_DURATION = "105:111"
T_NOISE    = "105:15"

# R2V
R_SAVE      = "92"
R_R2V       = "136"
R_PROMPT    = "138"
R_IMAGE     = "137"   # ref_image_0 = <Picture 1>
R_IMAGE2    = "139"   # ref_image_1 = <Picture 2>
R_SCHEDULER = "124"
R_NOISE     = "129"
R_DURATION  = "132"
R_AUDIO     = "140"   # LoadAudio → ref_audios.ref_audio_0


# ── 帧数换算 ──────────────────────────────────────────────────────────────────
def duration_to_length(d: float) -> int:
    base = max(5, round(d * 24))
    return base + (5 - base % 17) % 17


# ── ComfyUI HTTP 客户端 ────────────────────────────────────────────────────────
class ComfyUIClient:
    def __init__(self, host):
        self.host = host.rstrip("/")
        self.client_id = f"h3_mixed_{int(time.time())}"

    def queue_prompt(self, wf):
        resp = requests.post(f"{self.host}/prompt",
                             json={"prompt": wf, "client_id": self.client_id}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "prompt_id" not in data:
            raise RuntimeError(f"ComfyUI 返回异常: {data}")
        return data["prompt_id"]

    def wait_for_completion(self, prompt_id, timeout_min=120, poll_interval=15):
        deadline = time.time() + timeout_min * 60
        started  = time.time()
        last_hb  = started
        timeout_count = 0
        while time.time() < deadline:
            try:
                resp = requests.get(f"{self.host}/history/{prompt_id}", timeout=(5, 30))
                if resp.status_code == 200:
                    hist = resp.json()
                    if prompt_id in hist:
                        return hist[prompt_id]
            except requests.exceptions.ReadTimeout:
                timeout_count += 1
            except Exception as e:
                print(f"    ⚠ 轮询错误（继续等）: {e}")
            now = time.time()
            if now - last_hb >= 300:
                print(f"    💓 已等 {(now-started)/60:.1f} min（timeout {timeout_count} 次，正常）")
                last_hb = now
            time.sleep(poll_interval)
        raise TimeoutError(f"超时 {timeout_min} min: {prompt_id}")

    def download_output(self, filename, subfolder, kind, save_to, comfy_output_dir=None):
        save_to.parent.mkdir(parents=True, exist_ok=True)
        if comfy_output_dir:
            src = comfy_output_dir / subfolder / filename if subfolder else comfy_output_dir / filename
            if src.exists():
                shutil.copy2(src, save_to); return save_to
        url = f"{self.host}/view?" + urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": kind})
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(save_to, "wb") as f:
            for chunk in resp.iter_content(8192): f.write(chunk)
        return save_to

    def check_alive(self):
        try: return requests.get(f"{self.host}/system_stats", timeout=5).status_code == 200
        except: return False

    def get_queue_depth(self):
        try:
            d = requests.get(f"{self.host}/queue", timeout=5).json()
            return len(d.get("queue_running", [])), len(d.get("queue_pending", []))
        except: return -1, -1

    def get_vram_info(self):
        try:
            d   = requests.get(f"{self.host}/system_stats", timeout=5).json()
            dev = (d.get("devices") or [{}])[0]
            tot = dev.get("vram_total", 0) / 1024**3
            free= dev.get("vram_free",  0) / 1024**3
            return {"total": tot, "used": tot - free, "free": free,
                    "free_pct": free / tot if tot else 0, "name": dev.get("name", "?")}
        except: return None

    def free_memory(self):
        try:
            resp = requests.post(f"{self.host}/free",
                                 json={"unload_models": True, "free_memory": True}, timeout=30)
            return resp.status_code in (200, 204)
        except: return False


# ── workflow 构建 ──────────────────────────────────────────────────────────────
def load_template(path):
    if not path.exists():
        raise FileNotFoundError(f"找不到模板：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_t2v_workflow(template, shot):
    wf = copy.deepcopy(template)
    wf[T_MAIN]["inputs"]["prompt"]  = shot["positive_prompt"]
    wf[T_MAIN]["inputs"]["width"]   = int(shot.get("width", 720))
    wf[T_MAIN]["inputs"]["height"]  = int(shot.get("height", 1280))
    wf[T_DURATION]["inputs"]["value"] = float(shot.get("duration", 5))
    wf[T_NOISE]["inputs"]["noise_seed"] = int(shot["seed"])
    wf[T_SAVE]["inputs"]["filename_prefix"] = shot["filename_prefix"]
    return wf


def build_r2v_workflow(template, shot):
    wf       = copy.deepcopy(template)
    duration = float(shot.get("duration", 5))
    length   = duration_to_length(duration)

    # ref_image 支持单张（"a.png"）或双张（"a.png,b.png"）
    ref_images = [s.strip() for s in shot["ref_image"].split(",") if s.strip()]

    wf[R_PROMPT]["inputs"]["value"]          = shot["positive_prompt"]
    wf[R_R2V]["inputs"]["width"]             = int(shot.get("width", 720))
    wf[R_R2V]["inputs"]["height"]            = int(shot.get("height", 1280))
    wf[R_R2V]["inputs"]["length"]            = length
    wf[R_R2V]["inputs"]["ref_image_size"]    = shot.get("ref_image_size") or "match"
    wf[R_DURATION]["inputs"]["value"]        = duration
    wf[R_IMAGE]["inputs"]["image"]           = ref_images[0]
    if len(ref_images) >= 2:
        wf[R_IMAGE2]["inputs"]["image"]      = ref_images[1]
    wf[R_SCHEDULER]["inputs"]["scheduler"]  = shot.get("scheduler") or "simple"
    wf[R_NOISE]["inputs"]["noise_seed"]      = int(shot["seed"])
    wf[R_SAVE]["inputs"]["filename_prefix"]  = shot["filename_prefix"]

    # 音频参考（可选）：ref_audio 列有值时注入，空则断开连接
    ref_audio = shot.get("ref_audio", "").strip()
    if R_AUDIO not in wf:
        wf[R_AUDIO] = {
            "inputs": {"audio": "placeholder.wav"},
            "class_type": "LoadAudio",
            "_meta": {"title": "Load Audio (ref_audio_0)"}
        }
        wf["136"]["inputs"]["ref_audios.ref_audio_0"] = [R_AUDIO, 0]
    if ref_audio:
        wf[R_AUDIO]["inputs"]["audio"] = ref_audio
    else:
        wf["136"]["inputs"].pop("ref_audios.ref_audio_0", None)

    return wf


# ── CSV 加载 ──────────────────────────────────────────────────────────────────
def load_shots(csv_path, only=None, type_filter=None):
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到 CSV: {csv_path}")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if type_filter:
        rows = [r for r in rows if r.get("type") == type_filter]
    all_ids = [r["shot_id"] for r in rows]
    if only:
        miss = [s for s in only if s not in all_ids]
        if miss:
            raise ValueError(f"未找到 shot_id: {miss}，可选: {all_ids}")
        order = {s: i for i, s in enumerate(only)}
        return sorted([r for r in rows if r["shot_id"] in only],
                      key=lambda r: order[r["shot_id"]])
    return rows


# ── 显存检查 ──────────────────────────────────────────────────────────────────
def vram_check(client, min_free_gb=20.0, force_free=False):
    vram = client.get_vram_info()
    if not vram: return
    print(f"   💾 {vram['name']}  free={vram['free']:.1f}GB / total={vram['total']:.1f}GB")
    if force_free or vram["free"] < min_free_gb:
        label = "强制卸载（模型切换）" if force_free else f"free < {min_free_gb}GB"
        print(f"   🧹 {label} → POST /free...")
        if client.free_memory():
            time.sleep(5)
            v2 = client.get_vram_info()
            if v2: print(f"   ✅ /free 后: free={v2['free']:.1f}GB")


# ── 单镜处理 ──────────────────────────────────────────────────────────────────
def process_shot(client, templates, shot, dry_run, max_retries, comfy_output_dir, min_free_gb,
                 out_dir, download_dir):
    shot_id   = shot["shot_id"]
    shot_type = shot.get("type", "t2v")
    template  = templates[shot_type]

    if shot_type == "r2v":
        wf = build_r2v_workflow(template, shot)
        length = duration_to_length(float(shot.get("duration", 5)))
    else:
        wf = build_t2v_workflow(template, shot)
        length = None

    print(f"\n{'='*60}")
    print(f"🎬 [{shot_type.upper()}] {shot_id}  {shot['filename_prefix']}")
    print(f"   size={shot.get('width',720)}×{shot.get('height',1280)}"
          f"  duration={shot.get('duration',5)}s"
          + (f" → length={length} frames" if length else ""))
    if shot_type == "r2v":
        print(f"   ref_image  = {shot['ref_image']}")
        print(f"   ref_size   = {shot.get('ref_image_size','max')}"
              f"  scheduler  = {shot.get('scheduler','beta')}")
    print(f"   seed       = {shot['seed']}")
    print(f"   prompt[:80]= {shot['positive_prompt'][:80]}...")
    print(f"{'='*60}")

    out_dir.mkdir(exist_ok=True)
    wf_path = out_dir / f"{shot['filename_prefix']}_workflow.json"
    wf_path.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   ✅ workflow → {wf_path}")

    if dry_run:
        print("   ⏭ dry-run: 跳过 API 提交")
        return True

    vram_check(client, min_free_gb)

    current_seed = int(shot["seed"])
    K_NOISE_KEY  = R_NOISE if shot_type == "r2v" else T_NOISE
    for attempt in range(max_retries + 1):
        try:
            wf[K_NOISE_KEY]["inputs"]["noise_seed"] = current_seed
            print(f"   🚀 attempt {attempt+1}/{max_retries+1}  seed={current_seed}")

            pid = client.queue_prompt(wf)
            print(f"   📋 prompt_id={pid}")

            r, p = client.get_queue_depth()
            if r >= 0 and r + p > 1:
                print(f"   ⏸ 队列: running={r} pending={p}")

            est = "15-25 min" if shot_type == "r2v" else "10-20 min"
            print(f"   ⏳ 等待完成（H3 {shot_type.upper()} {shot.get('duration',5)}s 约 {est}）...")
            t0 = time.time()
            hist = client.wait_for_completion(pid, timeout_min=120)
            elapsed = time.time() - t0
            print(f"   ✅ 完成 {elapsed:.0f}s ({elapsed/60:.1f} min)")

            v2 = client.get_vram_info()
            if v2: print(f"   💾 完成后: used={v2['used']:.1f}GB free={v2['free']:.1f}GB")

            status = (hist.get("status") or {}).get("status_str", "")
            if status and status != "success":
                raise RuntimeError(f"ComfyUI status={status}")

            outputs  = hist.get("outputs", {})
            node_out = outputs.get(R_SAVE if shot_type == "r2v" else T_SAVE, {})
            videos   = node_out.get("images", []) or node_out.get("videos", []) or node_out.get("gifs", [])
            if not videos:
                print(f"   ⚠ 未找到视频输出，outputs.keys={list(outputs.keys())}")
                raise RuntimeError("no video output")

            download_dir.mkdir(exist_ok=True)
            for v in videos:
                save_to = download_dir / v["filename"]
                client.download_output(v["filename"], v.get("subfolder", ""),
                                       v.get("type", "output"), save_to, comfy_output_dir)
                print(f"   💾 {save_to}")
            return True

        except Exception as e:
            print(f"   ❌ {e}")
            if attempt < max_retries:
                current_seed = random.randint(1, 999_999_999)
                print(f"   🔄 换 seed={current_seed} 重试")
            else:
                print(f"   ❌ 达到最大重试次数，跳过 {shot_id}")
                return False
    return False


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="MiniMax H3 T2V+R2V 通用批量脚本")
    ap.add_argument("--csv",           required=True, help="shots_mixed_h3.csv 路径")
    ap.add_argument("--host",          default=DEFAULT_HOST)
    ap.add_argument("--gpu",           type=int, default=None, help="GPU 编号，自动映射端口 8188+N")
    ap.add_argument("gpu_pos",         nargs="?", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--only",          default=None, help="逗号分隔 shot_id，如 S01,S06")
    ap.add_argument("--type",          default=None, choices=["t2v","r2v"], help="只跑指定类型")
    ap.add_argument("--dry-run",       action="store_true")
    ap.add_argument("--max-retries",   type=int, default=2)
    ap.add_argument("--t2v-template",  default=str(DEFAULT_T2V_TEMPLATE))
    ap.add_argument("--r2v-template",  default=str(DEFAULT_R2V_TEMPLATE))
    ap.add_argument("--comfy-output",  default=str(DEFAULT_COMFY_OUTPUT))
    ap.add_argument("--min-free-gb",   type=float, default=20.0)
    args = ap.parse_args()

    if args.gpu_pos is not None and args.gpu is None:
        args.gpu = args.gpu_pos
    if args.gpu is not None and args.host == DEFAULT_HOST:
        args.host = f"http://127.0.0.1:{8188 + args.gpu}"
        print(f"🔗 GPU {args.gpu} → host = {args.host}")

    csv_path          = Path(args.csv).resolve()
    csv_dir           = csv_path.parent
    out_dir           = csv_dir / "out_h3"
    download_dir      = csv_dir / "downloaded_videos_h3"
    t2v_template_path = Path(args.t2v_template)
    r2v_template_path = Path(args.r2v_template)
    comfy_output_dir  = Path(args.comfy_output) if args.comfy_output else None
    if comfy_output_dir and not comfy_output_dir.exists():
        print(f"⚠ comfy-output 不存在，改用 HTTP 下载: {comfy_output_dir}")
        comfy_output_dir = None

    only_list = [s.strip() for s in args.only.split(",")] if args.only else None
    shots     = load_shots(csv_path, only_list, args.type)
    templates = {
        "t2v": load_template(t2v_template_path),
        "r2v": load_template(r2v_template_path),
    }

    t2v_shots = [s for s in shots if s.get("type") == "t2v"]
    r2v_shots = [s for s in shots if s.get("type") == "r2v"]

    print(f"📂 CSV:          {csv_path}")
    print(f"📂 t2v template: {t2v_template_path}")
    print(f"📂 r2v template: {r2v_template_path}")
    print(f"📂 out:          {out_dir}")
    print(f"📂 videos:       {download_dir}")
    print(f"✅ {len(shots)} 镜头（t2v={len(t2v_shots)} r2v={len(r2v_shots)}）")
    print(f"   {[s['shot_id'] for s in shots]}")

    client = ComfyUIClient(args.host)
    if not args.dry_run:
        print(f"\n🔌 检查 ComfyUI: {args.host}")
        if not client.check_alive():
            print("❌ ComfyUI 无法连接"); sys.exit(1)
        print("✅ ComfyUI 在线")
        r, p = client.get_queue_depth()
        if r >= 0: print(f"   队列: running={r} pending={p}")
        print(f"\n── 启动前显存检查 ──")
        vram_check(client, args.min_free_gb)

    results   = {}
    t_total   = time.time()
    prev_type = None

    for i, shot in enumerate(shots, 1):
        shot_type = shot.get("type", "t2v")

        # 模型切换时卸载 VRAM，避免 t2v/r2v 模型同时占用显存
        if not args.dry_run and prev_type and prev_type != shot_type:
            print(f"\n🔀 模型切换: {prev_type.upper()} → {shot_type.upper()}")
            vram_check(client, args.min_free_gb, force_free=True)

        print(f"\n\n{'#'*60}\n# 进度 [{i}/{len(shots)}]  {shot_type.upper()}\n{'#'*60}")
        results[shot["shot_id"]] = process_shot(
            client, templates, shot,
            args.dry_run, args.max_retries, comfy_output_dir, args.min_free_gb,
            out_dir, download_dir)
        prev_type = shot_type

    print(f"\n\n{'='*60}")
    print(f"🏁 完成 · 总用时 {(time.time()-t_total)/60:.1f} 分钟")
    print(f"{'='*60}")
    for sid, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {sid}")

    failed = [k for k, v in results.items() if not v]
    if failed:
        print(f"\n❌ 失败: {failed}")
        print(f"   重试: python3 batch_h3.py --csv {args.csv} --only {','.join(failed)}")
        sys.exit(1)
    else:
        print(f"\n🎉 全部 {len(shots)} 段成功 → {download_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠ 中断 (Ctrl+C)"); sys.exit(130)
    except Exception as e:
        import traceback; traceback.print_exc(); sys.exit(1)

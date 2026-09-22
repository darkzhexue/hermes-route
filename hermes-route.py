#!/usr/bin/env python3
"""hermes-route — 一条命令切换 Hermes 的默认模型与路由。

为什么需要这个工具
------------------
Hermes v0.21 的 `model.default` 只接受**字面模型 id**，不解析别名、也不携带路由。
实测：把 `model.default` 设成别名名 `glm53flash` → `HTTP 400: Unsupported model glm53flash`
（别名名被原样当成模型名发出去）。而跨渠道切换必须同时改
`model.default` / `model.provider` / `model.base_url` / `model.api_key` 四个键，
并且必须重钉所有 cron 任务（带旧 model_snapshot 的任务下次会 fail-closed 拒跑）。
本脚本把这几步合成一条命令，并自带验证与失败回滚。

用法
----
  hermes-route --list                列出可用别名及其路由
  hermes-route --show                显示当前默认模型/路由/别名/cron 钉选
  hermes-route <别名>                切换（默认会重钉 cron 并做端到端验证）
  hermes-route <别名> --dry-run      只打印将要执行的动作，不写任何文件
  hermes-route <别名> --no-cron      不重钉 cron
  hermes-route <别名> --no-verify    跳过端到端验证
  hermes-route <别名> --force        允许在验证失败时保留改动（默认回滚）

名字的解析顺序（与 Hermes 内部一致）
------------------------------------
  1. `model_aliases.<名>`  dict 条目  → 带完整路由（model/provider/base_url/key_env），跨渠道切换
  2. `model.aliases.<名>`  字符串条目 → 含 `/` 时拆成 provider/model；否则沿用当前 provider
  3. 都不是                            → 当作字面模型 id，**保持当前路由**（仅同端点内换模型）

注意：切换后**新会话**立即生效（配置按会话创建时快照），无需重启网关；
但已存在的会话会保持各自冻结的旧路由，需要在该会话里 `/new` 或 `/model <名>`。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path.home() / ".local/bin/hermes"
# HERMES_HOME 会被 hermes CLI 与本脚本同时尊重，便于隔离测试
HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CONFIG = HOME / "config.yaml"
JOBS = HOME / "cron" / "jobs.json"
STAMP = time.strftime("%Y%m%d-%H%M%S")


def hermes(*args, timeout=180):
    """Run the hermes CLI. argv list = no shell, so '${VAR}' is never expanded."""
    return subprocess.run([str(HERMES), *args], capture_output=True, text=True, timeout=timeout)


def load_yaml(path: Path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def current_route(cfg: dict) -> dict:
    m = cfg.get("model") or {}
    return {
        "model": m.get("default", ""),
        "provider": m.get("provider", ""),
        "base_url": m.get("base_url", ""),
        "api_key": m.get("api_key", ""),
    }


def resolve(cfg: dict, name: str):
    """Resolve a user-supplied name into a target route."""
    key = name.strip().lower()
    route = current_route(cfg)

    for aname, entry in ((cfg.get("model_aliases") or {})).items():
        if aname.strip().lower() != key or not isinstance(entry, dict):
            continue
        return {
            "model": entry.get("model", ""),
            "provider": entry.get("provider", route["provider"]),
            "base_url": entry.get("base_url", "") or route["base_url"],
            "api_key": entry.get("api_key", "")
            or (f"${{{entry['key_env']}}}" if entry.get("key_env") else route["api_key"]),
            "source": f"model_aliases.{aname}",
            "cross_channel": bool(entry.get("base_url")),
        }

    for aname, value in ((cfg.get("model") or {}).get("aliases") or {}).items():
        if aname.strip().lower() != key or not isinstance(value, str):
            continue
        if "/" in value:
            prov, model = value.split("/", 1)
        else:
            prov, model = route["provider"], value
        return {
            "model": model.strip(),
            "provider": prov.strip() or route["provider"],
            "base_url": route["base_url"],
            "api_key": route["api_key"],
            "source": f"model.aliases.{aname} (字符串别名，不换渠道)",
            "cross_channel": False,
        }

    return {
        "model": name.strip(),
        "provider": route["provider"],
        "base_url": route["base_url"],
        "api_key": route["api_key"],
        "source": "字面模型 id（保持当前路由）",
        "cross_channel": False,
    }


def load_jobs() -> list:
    if not JOBS.exists():
        return []
    try:
        data = json.loads(JOBS.read_text(encoding="utf-8"))
    except Exception:
        return []
    jobs = data if isinstance(data, list) else data.get("jobs", data)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [job for job in jobs if isinstance(job, dict)]


def enabled_jobs():
    out = []
    for job in load_jobs():
        if job.get("enabled", True) is False:
            continue
        jid = job.get("id") or job.get("job_id")
        if jid:
            out.append((jid, job.get("name", ""), job))
    return out


def cmd_list(cfg: dict) -> int:
    print("可用于 hermes-route 的名字（model_aliases，带完整路由，推荐）：")
    for aname, entry in ((cfg.get("model_aliases") or {})).items():
        if isinstance(entry, dict):
            print(f"  {aname:<24} → {entry.get('model'):<28} {entry.get('base_url') or '(沿用当前 base_url)'}")
    simple = (cfg.get("model") or {}).get("aliases") or {}
    if simple:
        print("\nmodel.aliases（字符串别名，只换模型名、不换渠道）：")
        for aname, value in simple.items():
            print(f"  {aname:<24} → {value}")
    print("\n也可以直接给字面模型 id（仅同端点内换模型，保持当前路由）：")
    print("  hermes-route mimo-v2.6-pro")
    return 0


def cmd_show(cfg: dict) -> int:
    r = current_route(cfg)
    print("当前默认路由（~/.hermes/config.yaml 的 model 段）：")
    for label, key in (
        ("model.default", "model"),
        ("model.provider", "provider"),
        ("model.base_url", "base_url"),
        ("model.api_key", "api_key"),
    ):
        print(f"  {label:<18} {r[key]}")
    print("\ncron 任务钉选：")
    for jid, name, job in enabled_jobs():
        print(f"  {jid}  {name:<28} provider={job.get('provider')} model={job.get('model')}")
    return 0


def switch(cfg: dict, target: dict, do_cron: bool, dry: bool, force: bool = False) -> int:
    before = current_route(cfg)
    commands = [
        ("model.default", target["model"]),
        ("model.provider", target["provider"]),
        ("model.base_url", target["base_url"]),
        ("model.api_key", target["api_key"]),
    ]

    print(f"解析来源：{target['source']}")
    print(f"  改动前：{before['model']}  @ {before['provider']} / {before['base_url']}")
    print(f"  改动后：{target['model']}  @ {target['provider']} / {target['base_url']}  (key={target['api_key']})")
    if target["cross_channel"]:
        print("  ⚠ 跨渠道：provider/base_url/api_key 将一并改写")
    else:
        print("  同渠道：只改 model.default")

    planned = [["config", "set", k, v] for k, v in commands]
    if do_cron:
        for jid, _name, _job in enabled_jobs():
            planned.append(["cron", "edit", jid, "--provider", target["provider"], "--model", target["model"]])

    if dry:
        print("\n[dry-run] 将执行：")
        for c in planned:
            print("  hermes " + " ".join(f"'{a}'" if " " in a or "$" in a else a for a in c))
        return 0

    backups = {}
    for path in (CONFIG, JOBS):
        if path.exists():
            bak = path.with_name(path.name + f".bak-route-{STAMP}")
            shutil.copy2(path, bak)
            backups[path] = bak
    print(f"\n已备份：{', '.join(b.name for b in backups.values())}")

    failures = []
    for c in planned:
        res = hermes(*c)
        if res.returncode != 0:
            failures.append((c, (res.stderr or res.stdout or "").strip()[:200]))

    if failures:
        print("\n写入失败，开始回滚：")
        for c, err in failures:
            print(f"  ✗ {' '.join(c)}: {err}")
        for path, bak in backups.items():
            shutil.copy2(bak, path)
        print("已回滚到改动前状态。")
        return 1

    for c in planned:
        print(f"  ✓ hermes {' '.join(c)}")

    if do_cron:
        print("\n校验 cron：")
        res = hermes("cron", "doctor")
        print("  " + " / ".join((res.stdout or "").strip().splitlines()[:3]))

    print("\n端到端验证（hermes -z）：")
    res = hermes("-z", "只回复:OK", timeout=240)
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    ok = "OK" in out
    print(f"  {'✓' if ok else '✗'} {out.splitlines()[-1] if out else '(无输出)'}")
    if not ok:
        if force:
            print("\n--force：验证虽未通过，保留改动。")
            return 1
        print("\n验证失败，回滚：")
        for path, bak in backups.items():
            shutil.copy2(bak, path)
        print("已回滚到改动前状态。")
        return 1

    print("\n完成。新会话立即生效；已存在的会话需 /new 或 /model 才会跟到新路由。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="一条命令切换 Hermes 默认模型与路由")
    ap.add_argument("name", nargs="?", help="model_aliases 别名 / model.aliases 别名 / 字面模型 id")
    ap.add_argument("--list", action="store_true", help="列出可用别名")
    ap.add_argument("--show", action="store_true", help="显示当前路由与 cron 钉选")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    ap.add_argument("--no-cron", action="store_true", help="不重钉 cron 任务")
    ap.add_argument("--force", action="store_true", help="验证失败也保留改动（默认回滚）")
    args = ap.parse_args()

    if not CONFIG.exists():
        print(f"找不到 {CONFIG}", file=sys.stderr)
        return 2
    cfg = load_yaml(CONFIG)

    if args.list:
        return cmd_list(cfg)
    if args.show or not args.name:
        return cmd_show(cfg)

    target = resolve(cfg, args.name)
    if not target["model"]:
        print(f"无法解析 '{args.name}' 为目标模型", file=sys.stderr)
        return 2
    return switch(cfg, target, do_cron=not args.no_cron, dry=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

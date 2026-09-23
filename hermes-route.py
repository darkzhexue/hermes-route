#!/usr/bin/env python3
"""hermes-route — 一条命令切换 Hermes 的默认模型、路由、上下文窗口与最大输出。

为什么需要这个工具
------------------
Hermes v0.21 的 `model.default` 只接受**字面模型 id**，不解析别名、也不携带路由。
实测：把 `model.default` 设成别名名 `glm53flash` → `HTTP 400: Unsupported model glm53flash`
（别名名被原样当成模型名发出去）。而跨渠道切换必须同时改
`model.default` / `model.provider` / `model.base_url` / `model.api_key` 四个键，
并且必须重钉所有 cron 任务（带旧 model_snapshot 的任务下次会 fail-closed 拒跑）。

`model.context_length` 与 `model.max_tokens` 在本版本里**是全局单值、没有 per-model 写法**
（优先级：ephemeral > model.max_tokens > provider profile）。所以切到上限不同的模型时
它们不会跟着变：切到上限更小的模型会 400，切到上限更大的模型会白白砍输出；
窗口小于当前值时更隐蔽——压缩按 threshold × context_length 触发，
窗口写大了压缩就永远来不及。本脚本把模型表和这两个键一起切。

用法
----
  hermes-route --list                       列出别名、各自路由与已知的窗口/上限
  hermes-route --show                       显示当前模型/路由/窗口/上限/cron 钉选
  hermes-route <别名>                       切换（写全部相关键 + 重钉 cron + 端到端验证）
  hermes-route <别名> --dry-run             只打印将要执行的动作，不写任何文件
  hermes-route <别名> --no-cron             不重钉 cron
  hermes-route <别名> --no-limits           本次不动 context_length / max_tokens
  hermes-route <别名> --force               验证失败也保留改动（默认回滚）
  hermes-route --set-limits <别名> --context N --max-tokens N    记录该模型的窗口/上限
  hermes-route --forget-limits <别名>       删除记录

名字的解析顺序（与 Hermes 内部一致）
------------------------------------
  1. `model_aliases.<名>`  dict 条目  → 带完整路由（model/provider/base_url/key_env），跨渠道切换
  2. `model.aliases.<名>`  字符串条目 → 含 `/` 时拆成 provider/model；否则沿用当前 provider
  3. 都不是                            → 当作字面模型 id，**保持当前路由**（仅同端点内换模型）

窗口/上限记在哪
--------------
`~/.hermes/hermes-route.json`（工具私有，Hermes 不读），**按模型 id 索引**而不是别名名——
数值属于"模型+端点"，两个别名指向同一模型时自然共享，字面 id 也能查到。
查不到的模型**不猜**：保持现有值并明确提示，切换末尾的真实请求会撞出 400 并触发自动回滚。

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
LIMITS_FILE = HOME / "hermes-route.json"
STAMP = time.strftime("%Y%m%d-%H%M%S")

CTX_RANGE = (1_000, 10_000_000)
OUT_RANGE = (256, 1_000_000)


def hermes(*args, timeout=180):
    """Run the hermes CLI. argv list = no shell, so '${VAR}' is never expanded."""
    return subprocess.run([str(HERMES), *args], capture_output=True, text=True, timeout=timeout)


def load_yaml(path: Path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --- 工具私有的窗口/上限表 -------------------------------------------------

def load_limits() -> dict:
    """Read the limits table. A missing or corrupt file is an empty table, never an error."""
    if not LIMITS_FILE.exists():
        return {}
    try:
        data = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    table = data.get("limits") if isinstance(data, dict) else None
    return table if isinstance(table, dict) else {}


def save_limits(table: dict) -> None:
    tmp = LIMITS_FILE.with_name(LIMITS_FILE.name + ".tmp")
    tmp.write_text(
        json.dumps({"limits": table}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(LIMITS_FILE)


def limits_for(model_id: str, table: dict) -> dict:
    entry = table.get(model_id)
    if not isinstance(entry, dict):
        return {"context_length": None, "max_tokens": None, "known": False}
    ctx = entry.get("context_length")
    out = entry.get("max_tokens")
    ctx = ctx if isinstance(ctx, int) else None
    out = out if isinstance(out, int) else None
    return {"context_length": ctx, "max_tokens": out, "known": ctx is not None and out is not None}


def validate_limits(ctx, out) -> str | None:
    """Return an error message, or None when the values are sane."""
    if ctx is None and out is None:
        return "至少要给一个：--context 或 --max-tokens"
    if ctx is not None and not (CTX_RANGE[0] <= ctx <= CTX_RANGE[1]):
        return f"--context 超出合理范围 {CTX_RANGE[0]}..{CTX_RANGE[1]}"
    if out is not None and not (OUT_RANGE[0] <= out <= OUT_RANGE[1]):
        return f"--max-tokens 超出合理范围 {OUT_RANGE[0]}..{OUT_RANGE[1]}"
    if ctx is not None and out is not None and out > ctx:
        return "--max-tokens 不能大于 --context"
    return None


# --- 路由解析 -------------------------------------------------------------

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


def build_commands(target: dict, use_limits: bool) -> list:
    """The single place that decides WHICH config keys get written."""
    cmds = [
        ("model.default", target["model"]),
        ("model.provider", target["provider"]),
        ("model.base_url", target["base_url"]),
        ("model.api_key", target["api_key"]),
    ]
    if use_limits:
        if target.get("context_length") is not None:
            cmds.append(("model.context_length", str(target["context_length"])))
        if target.get("max_tokens") is not None:
            cmds.append(("model.max_tokens", str(target["max_tokens"])))
    return cmds


# --- cron -----------------------------------------------------------------

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


def show_limits(model_id: str, table: dict) -> str:
    lim = limits_for(model_id, table)
    if not lim["known"]:
        return "未记录（切换时保持当前值并提示）"
    return f"窗口 {lim['context_length']:,} / 上限 {lim['max_tokens']:,}"


# --- 子命令 ---------------------------------------------------------------

def cmd_list(cfg: dict, table: dict) -> int:
    print("可用于 hermes-route 的名字（model_aliases，带完整路由，推荐）：")
    for aname, entry in ((cfg.get("model_aliases") or {})).items():
        if not isinstance(entry, dict):
            continue
        model = entry.get("model") or ""
        print(f"  {aname:<22} → {model:<26} {entry.get('base_url') or '(沿用当前 base_url)'}")
        print(f"  {'':<22}   {show_limits(model, table)}")
    simple = (cfg.get("model") or {}).get("aliases") or {}
    if simple:
        print("\nmodel.aliases（字符串别名，只换模型名、不换渠道）：")
        for aname, value in simple.items():
            print(f"  {aname:<22} → {value}")
    print("\n也可以直接给字面模型 id（仅同端点内换模型，保持当前路由）：")
    print("  hermes-route mimo-v2.6-pro")
    return 0


def cmd_show(cfg: dict, table: dict) -> int:
    r = current_route(cfg)
    print("当前默认路由（~/.hermes/config.yaml 的 model 段）：")
    m = cfg.get("model") or {}
    for label, value in (
        ("model.default", r["model"]),
        ("model.provider", r["provider"]),
        ("model.base_url", r["base_url"]),
        ("model.api_key", r["api_key"]),
        ("model.context_length", m.get("context_length")),
        ("model.max_tokens", m.get("max_tokens")),
    ):
        print(f"  {label:<22} {value}")
    print(f"  {'表的记录':<22} {show_limits(r['model'], table)}")
    print("\ncron 任务钉选：")
    for jid, name, job in enabled_jobs():
        print(f"  {jid}  {name:<28} provider={job.get('provider')} model={job.get('model')}")
    return 0


def cmd_set_limits(cfg: dict, table: dict, name: str, ctx, out) -> int:
    err = validate_limits(ctx, out)
    if err:
        print(f"参数不合法：{err}", file=sys.stderr)
        return 2
    target = resolve(cfg, name)
    model_id = target["model"]
    if not model_id:
        print(f"无法解析 '{name}'", file=sys.stderr)
        return 2
    entry = dict(table.get(model_id) or {})
    if ctx is not None:
        entry["context_length"] = ctx
    if out is not None:
        entry["max_tokens"] = out
    table[model_id] = entry
    save_limits(table)
    print(f"✓ 已记录 {model_id}: {show_limits(model_id, table)}")
    print(f"  （表文件 {LIMITS_FILE}；按模型 id 索引，共用该模型的所有别名都会生效）")
    return 0


def cmd_forget_limits(table: dict, name: str, cfg: dict) -> int:
    model_id = resolve(cfg, name)["model"]
    if model_id in table:
        table.pop(model_id)
        save_limits(table)
        print(f"✓ 已删除 {model_id} 的记录")
        return 0
    print(f"表里没有 {model_id} 的记录（未改动）")
    return 0


def switch(cfg: dict, target: dict, do_cron: bool, dry: bool, force: bool = False,
           use_limits: bool = True) -> int:
    before = current_route(cfg)
    m = cfg.get("model") or {}
    lim = limits_for(target["model"], load_limits())
    target["context_length"] = lim["context_length"]
    target["max_tokens"] = lim["max_tokens"]

    commands = build_commands(target, use_limits)
    limits_applied = [c for c in commands if c[0].startswith("model.context") or c[0] == "model.max_tokens"]

    print(f"解析来源：{target['source']}")
    print(f"  改动前：{before['model']}  @ {before['provider']} / {before['base_url']}")
    print(f"  改动后：{target['model']}  @ {target['provider']} / {target['base_url']}  (key={target['api_key']})")
    if target["cross_channel"]:
        print("  ⚠ 跨渠道：provider/base_url/api_key 将一并改写")
    else:
        print("  同渠道：只改 model.default")

    if not use_limits:
        print("  --no-limits：本次不动 context_length / max_tokens")
    elif limits_applied:
        for key, value in limits_applied:
            cur = m.get(key.split(".")[1])
            print(f"  {key}: {cur} → {value}")
    else:
        print(f"  ⚠ 该模型无窗口/上限记录：保持现有 context_length={m.get('context_length')} / "
              f"max_tokens={m.get('max_tokens')}")
        print(f"     补记录：hermes-route --set-limits {target['model']} --context N --max-tokens N")

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
    ap = argparse.ArgumentParser(add_help=True, description="一条命令切换 Hermes 默认模型、路由、窗口与最大输出")
    ap.add_argument("name", nargs="?", help="model_aliases 别名 / model.aliases 别名 / 字面模型 id")
    ap.add_argument("--list", action="store_true", help="列出别名、路由与已知的窗口/上限")
    ap.add_argument("--show", action="store_true", help="显示当前模型/路由/窗口/上限/cron 钉选")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    ap.add_argument("--no-cron", action="store_true", help="不重钉 cron 任务")
    ap.add_argument("--no-limits", action="store_true", help="本次不动 context_length / max_tokens")
    ap.add_argument("--force", action="store_true", help="验证失败也保留改动（默认回滚）")
    ap.add_argument("--set-limits", action="store_true", help="把 NAME 的窗口/上限记入表")
    ap.add_argument("--forget-limits", action="store_true", help="删除 NAME 的窗口/上限记录")
    ap.add_argument("--context", type=int, help="配合 --set-limits：上下文窗口 token 数")
    ap.add_argument("--max-tokens", type=int, help="配合 --set-limits：最大输出 token 数")
    args = ap.parse_args()

    if not CONFIG.exists():
        print(f"找不到 {CONFIG}", file=sys.stderr)
        return 2
    cfg = load_yaml(CONFIG)
    table = load_limits()

    if args.set_limits:
        if not args.name:
            print("--set-limits 需要 NAME：hermes-route --set-limits <别名> --context N --max-tokens N", file=sys.stderr)
            return 2
        return cmd_set_limits(cfg, table, args.name, args.context, args.max_tokens)

    if args.forget_limits:
        if not args.name:
            print("--forget-limits 需要 NAME", file=sys.stderr)
            return 2
        return cmd_forget_limits(table, args.name, cfg)

    if args.list:
        return cmd_list(cfg, table)
    if args.show or not args.name:
        return cmd_show(cfg, table)

    target = resolve(cfg, args.name)
    if not target["model"]:
        print(f"无法解析 '{args.name}' 为目标模型", file=sys.stderr)
        return 2
    return switch(cfg, target, do_cron=not args.no_cron, dry=args.dry_run,
                  force=args.force, use_limits=not args.no_limits)


if __name__ == "__main__":
    sys.exit(main())

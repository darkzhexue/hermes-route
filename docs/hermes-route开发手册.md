# hermes-route 开发手册

> 对象：`~/.local/bin/hermes-route`（Python 单文件，约 440 行）
> 用法：见 [README](../README.md)
> 首次实现：2026-09-23
> 说明：本文档是本地《服务器运维手册》「附录 A」的**脱敏发布版**（服务器地址已替换为 `<server-host>`、去掉了本机路径与运维侧引用）。两边内容应保持一致，改动时同步。

## A.1 问题背景：为什么必须自己写

### A.1.1 Hermes 没有内置的一行切换（实测结论）

直觉上 `hermes config set model.default <别名>` 应该就能切，但**不行**。2026-09-23 用隔离 `HERMES_HOME` 实测：

| 配置 | 结果 |
|---|---|
| `model.default: glm53flash`（别名名） | `HTTP 400: Unsupported model glm53flash` |
| `model.default: glm-5.3-flash`（真实 id，但路由指向 MiMo） | `HTTP 400: Unsupported model glm-5.3-flash` |

第一行是决定性的：**别名名被原样当成模型 id 发出去**，说明 `model.default` 不解析别名。源码印证在 `hermes_cli/oneshot.py:409` 的注释——别名只在模型由**参数或环境变量显式指定**时解析：

> Only auto-detect when the model was explicitly requested via arg or env var
> (**not when it came from config** — that's the "use my defaults" path and the
> configured provider is already correct).

所以 `model.default` 的语义是"字面模型 id + 让 config 里的 provider/base_url 决定路由"。它**既不解析别名，也不携带路由**。

### A.1.2 跨渠道 = 四个键 + 窗口/上限 + 重钉 cron

既然路由由 `model.provider`/`model.base_url`/`model.api_key` 决定，跨渠道切换就要写齐四个键：

```
model.default   ← 目标模型 id
model.provider  ← 目标 provider 名
model.base_url  ← 目标端点
model.api_key   ← '${目标KEY_ENV}'      ← 单引号！见 A.8 坑 1
```

另外两个键**在 v0.21 里是全局单值、没有 per-model 写法**，所以不会跟着模型走，必须由工具显式改写：

```
model.context_length   ← 上下文窗口（token）
model.max_tokens       ← 最大输出（token）；优先级 ephemeral > 本键 > provider profile
```

不改它们的后果是双向的：切到**上限更小**的模型会每轮 400（Hermes 自愈但白跑一次、还误触发压缩）；
切到**上限更大**的模型会白砍输出；而**窗口写大了最隐蔽**——压缩按 `compression.threshold × context_length`
触发，窗口虚高就意味着压缩永远来不及，会话会悄悄超出模型的真实容量。

这两个数值必须**逐条查证**，不能按系列猜：GLM-5 / 5.1 / 5.2 是 200K 窗口，而 **GLM-5.3 这一代是 1M**，
同一个系列差 5 倍。2026-09-23 三方核对 glm-5.3-flash = 1M / 128K（Z.AI 官方文档、腾讯云 TokenHub
模型清单 `cloud.tencent.com/document/product/1823/130051`、以及服务器上 `models_dev_cache.json`
的 `limit: {context: 1000000, output: 131072}`）。

且必须**逐个重钉所有 cron 任务**（`hermes cron edit <job_id> --provider <p> --model <m>`）：带旧 `model_snapshot` 的任务下次运行会 **fail-closed 拒跑**。这是最容易漏的一步，也是最值得自动化的部分。

### A.1.3 会话级路由冻结（决定"要不要重启网关"）

模型路由**按会话冻结**，存在 `~/.hermes/state.db` → `sessions.model` + `sessions.model_config.gateway_runtime`（`{provider, base_url, api_mode, fallback_active}`），在会话**创建/重置**时从当前 config 快照一次。

推论（2026-09-23 在服务器上验证）：
- **新会话立即用上新路由，无需重启网关。** 证据：网关进程自 2026-09-18 起 `NRestarts=0`（MiMo 切换前后都没重启），而 09-22 切换后新建的 QQ 会话已经是 `custom / api.xiaomimimo.com` 且 `agent.log` 显示其在跑 mimo。
- **已存在的会话保持各自冻结的旧路由**，需要在那个会话里 `/new` 或 `/model <名>` 才会跟随。
- 副作用：切换后会出现"混血会话"（`model` 列已是新名、`billing_provider/base_url` 还是旧路由），一旦接话就会把新模型名打到旧端点。

### A.1.4 为什么不直接用现成的开源项目

GitHub 搜索（`hermes agent model switch`）返回 28 个仓库，最贴题的 `zhaotianxi/hermes-model-switch`（"一键切换，预验证+自动回退"）评估后放弃：

- **0 star、最后更新 2026-05-17**，而本机 Hermes 是 v0.21.0（2026-08-31）/ `_config_version: 40`，5 个月前的工具大概率不认识当前 schema；
- 这类工具必须读写 `~/.hermes/config.yaml` 与 `.env`，**API key 全在那两处**，未 review 的第三方代码触碰它们是供应链风险；
- 它们都不知道本机 3 个 cron 任务的 ID，处理不了 A.1.2 的 fail-closed 陷阱。

结论：自己写一份约 440 行、可审计、且已经知道本机 cron 布局的实现更划算。

## A.2 设计

### A.2.1 职责边界

**只做一件事**：把"切换默认模型 + 路由 + 窗口/上限"从 6 条命令、2 个易漏步骤，压缩成 1 条命令，并附带验证与回滚。

明确**不做**：多 provider 池化、按额度/任务自动路由、成本优化——那些属于编排层（GitHub 上 `hermes-cli-orchestrator` 之类在做）。

### A.2.2 名字解析：三层，与 Hermes 内部一致

| 顺序 | 来源 | 是否换路由 |
|---|---|---|
| 1 | `model_aliases.<名>` **dict** 条目 | **是**（带 model/provider/base_url/key_env，完整路由） |
| 2 | `model.aliases.<名>` **字符串**条目 | 否（含 `/` 时拆成 provider/model；否则沿用当前 provider；base_url 留空→沿用） |
| 3 | 字面模型 id | 否（保持当前路由，仅同端点内换模型） |

复刻自 `hermes_cli/model_switch.py` 的 `_load_direct_aliases()`：dict 先加载，字符串形式同名条目会被 `if key in merged: continue` 跳过（所以 dict 优先）。

### A.2.3 写入策略：只用官方 CLI，绝不手改 YAML

所有配置写入走 `hermes config set`，cron 走 `hermes cron edit`。原因：官方 skill 明令——手改 `config.yaml` 的一个缩进错误会破坏 live gateway；且 `hermes config set` 会做规范化与校验。

**唯一例外**是回滚：失败时用 `shutil.copy2` 把时间戳备份覆盖回去。这是官方 skill 记载的 rollback 手法（`cp config.yaml.bak config.yaml`），因为此时需要的是"恢复到已知良好快照"，而不是再走一遍 set 语义。

### A.2.4 验证与回滚

每次切换的顺序：

```
解析目标路由 → 打印 before/after → 备份两处 → 逐条写入 → cron doctor
   → hermes -z 端到端验证 → 失败则回滚两处并 exit 1
```

验证用真实 API 调用（`hermes -z "只回复:OK"`），**不看配置回读**——配置回读只能证明"写进去了"，证明不了"这条路走得通"。代价是每次切换多一次极小请求，并会在 `state.db` 留一条微型 cli 会话。

### A.2.5 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（或 dry-run 完成） |
| 1 | 写入失败已回滚 / 验证失败已回滚 / `--force` 下验证未过但保留 |
| 2 | 找不到 config.yaml 或名字无法解析 |

### A.2.6 窗口/上限表：为什么不塞进别名条目

`model.context_length` / `model.max_tokens` 的目标值来自一张**工具私有的表**：`~/.hermes/hermes-route.json`，
**按模型 id 索引**（不是别名名）。

- 数值属于"模型 + 端点"，不属于"你给它起的绰号"；两个别名指向同一模型时自然共享，而且
  `hermes-route <字面模型id>` 这种用法也能查到（按别名名 keying 就查不到）。
- 放在工具自己的文件里，还顺带免疫 Hermes 对 `config.yaml` 的规范化与 `_config_version` 迁移。

不塞进 `model_aliases.<别名>` 条目的理由是**一个具体的维护风险**：那是 Hermes 的 schema，
而"加一条别名"的常规配方是按固定字段写 `hermes config set model_aliases.X '{...}'`（A.7.1 就是
这么教的，服务器上那份官方 skill 也是）——谁照配方编辑一次，额外字段就被整个覆盖。
何况 `hermes config set` 对不认识的键只会"存下来但警告"，长期看不是可靠的落脚点。

**查不到时明确不猜**：保持现有值不动 + 醒目提示 + 给出补记录的命令。不自动取"所有已知路线的最小值"，
是因为静默砍输出、提前压缩这类代价**末端的验证兜不住**（验证只能撞出 400 那类硬错误）；
而"窗口/上限写错导致 400"恰好落在验证能撞出并自动回滚的范围内。

## A.3 代码结构（`hermes-route.py`）

```
常量
  HERMES       ~/.local/bin/hermes          CLI 入口
  HOME         HERMES_HOME 或 ~/.hermes      ← 尊重 HERMES_HOME，便于隔离测试
  CONFIG       $HOME/config.yaml
  JOBS         $HOME/cron/jobs.json
  LIMITS_FILE  $HOME/hermes-route.json      ← 工具私有的窗口/上限表
  STAMP        时间戳，用于备份名
  CTX_RANGE / OUT_RANGE                     写入前的合理性区间

工具函数
  hermes(*args)                 subprocess 调用 CLI（argv 列表 = 无 shell，'${VAR}' 不会被展开）
  load_yaml(path)               yaml.safe_load
  load_jobs() / enabled_jobs()  读 jobs.json，返回 (id, name, job) 且过滤 enabled=False
  load_limits() / save_limits() 读写窗口/上限表（文件缺失或损坏 → 当空表，绝不抛错；写入走临时文件+原子替换）
  limits_for(model_id, table)   查表 → {context_length, max_tokens, known}
  validate_limits(ctx, out)     --set-limits 的入参校验，返回错误信息或 None
  show_limits(model_id, table)  人类可读的一行（给 --list / --show 用）

核心
  current_route(cfg)      读 model 段的四个键
  resolve(cfg, name)      三层解析 → {model, provider, base_url, api_key, source, cross_channel}
  build_commands(target, use_limits)   ★ 唯一决定"哪些 config 键会被写"的地方（4 键 + 条件性 2 键）
  cmd_list(cfg, table)    别名 + 各自路由 + 已知的窗口/上限
  cmd_show(cfg, table)    当前路由（含 context_length / max_tokens）+ 表的记录 + cron 钉选
  cmd_set_limits(..) / cmd_forget_limits(..)   维护那张表（只动自己的 json，不进 config.yaml）
  switch(...)             备份→写入→cron doctor→验证→回滚

入口
  main()  argparse：name / --list / --show / --dry-run / --no-cron / --no-limits / --force
                    / --set-limits / --forget-limits / --context / --max-tokens
```

**`build_commands()` 与 `switch()` 的 planned 列表**是设计核心：`build_commands()` 是"哪些键会被写"的
唯一决策点；`switch()` 把它的结果建成"要执行的全部命令"列表，dry-run 直接打印这份列表、真实执行遍历
同一份、任一失败即整体回滚。所以 dry-run 与实际执行**同源**，不会出现"dry-run 显示的和我实际做的不一样"；
而加窗口/上限时只改了 `build_commands()` 一处，备份/重钉/验证/回滚那套机制一行没动。

## A.4 依赖的 Hermes 内部契约

改这个工具前需要知道的、已验证的 Hermes 行为（沿用前请复核版本，本机为 v0.21.0）：

| 契约 | 位置 | 说明 |
|---|---|---|
| 别名解析规则 | `hermes_cli/model_switch.py` `_load_direct_aliases()` | dict 优先于字符串；字符串无 `/` → 取当前 provider 且 base_url 留空 |
| 别名缓存失效 | 同文件 `_ensure_direct_aliases()` / `_direct_alias_source_identity()` | 缓存按 `(config_path, mtime_ns, size)` 做键 → **改 config 即失效，无需重启进程** |
| 别名不参与 config 路径 | `hermes_cli/oneshot.py:409` | 仅 `-m`/env 显式指定时解析别名 |
| 会话内 `/model` | `gateway/slash_commands.py:1764` 起 | 网关侧唯一会**连 provider/base_url 一起重解析**的入口 |
| `DirectAlias` 字段 | `model_switch.py:515` | `model/provider/base_url/api_key/key_env`；api_key 缺失时按宿主解析，关联上游 #83612（避免把默认 provider 的 key 发到无关端点） |
| URL 型别名强制 custom | `model_switch.py` `direct_alias_runtime_request()` | 带 base_url 的别名会把 provider 强制成裸 `custom`（host-gated，#28660） |
| `context_length` / `max_tokens` 读点 | `gateway/run.py` / `agent/agent_init.py` | 都在 **agent init** 时读取 → 新会话或网关重启才生效 |
| cron 钉选 | `hermes cron edit <job_id> --provider <p> --model <m>` | 改 `model.default` 后必须重钉，否则 fail-closed |
| CLI vs 会话差异 | 实测 | CLI `hermes -m <id>` **不换路由**，跨渠道必须加 `--provider` |
| 官方流程文档 | `~/.hermes/skills/autonomous-ai-agents/hermes-model-config/SKILL.md` | 含 `scripts/wire_tap.py`（抓真实请求体）、`probe_*.py` |

## A.5 开发与测试（隔离 HERMES_HOME，不碰线上）

本工具的全部验证都在临时 `HERMES_HOME` 里完成——`hermes` CLI 与本脚本都尊重该变量，所以 config/cron/session 全部隔离。

```bash
T=$(mktemp -d)
cp ~/.hermes/config.yaml $T/          # 配置
cp ~/.hermes/.env $T/                 # 密钥（验证步骤需要）
cp ~/.hermes/hermes-route.json $T/    # 窗口/上限表
mkdir -p $T/cron && cp ~/.hermes/cron/jobs.json $T/cron/
export HERMES_HOME=$T
hermes-route --list
hermes-route --show
hermes-route glm53flash --dry-run     # 先看将执行什么
hermes-route glm53flash               # 真实切换（会真调 API 验证）
hermes-route --show                   # 确认路由键 + 窗口/上限 + 3 个 cron 都变了
```

**必测五场景**：

1. **往返**：MiMo → glm53flash → MiMo，两个方向都要路由键正确、3 个 cron 跟随、`cron doctor` 干净、`-z` 返回 OK。
2. **窗口/上限跟着切**：切到一个上限不同的模型（`deepseek-v4.1-flash`，131072 → 393216），dry-run 里要能看到 `model.max_tokens` 的变化，真实执行后 `--show` 要反映出来，切回来要变回去。
3. **无记录时不猜**：切到表里没有的模型（如 `mimo-v2.6-pro`），必须打印"保持现有值"的提示、**不写**那两个键，且切换本身仍然成功。
4. **回滚**：在当前路由下切到一个该校不存在的 id（如 MiMo 路由下切 `deepseek/deepseek-flash`），应验证失败 → `config.yaml` 与 `cron/jobs.json` **双双**恢复 → exit 1 → 无半成品残留。注意此时 `max_tokens` 已被表里的值改过，必须一并回滚回去。
5. **dry-run**：与实际执行命令一致，且不产生任何文件。

**2026-09-23 实测记录**：五场景全部通过——往返两方向 OK；`max_tokens` 131072 ↔ 393216 双向生效；
无记录模型给出提示且不写 limit 键；回滚测试两文件均干净恢复（含 limit 键）、回滚 exit 1、非法参数 exit 2；
dry-run 与真实执行一致（6 条 `config set` + 3 条 `cron edit`）。

> 注意本工具不在**非交互** SSH（paramiko）的 PATH 上，测试脚本里要用绝对路径 `~/.local/bin/hermes-route`。

## A.6 部署

脚本经由 paramiko + base64 上传（避免多引号嵌套；本机无 scp/sshpass）：

```python
import base64, paramiko
src = open("hermes-route.py", "rb").read()
b64 = base64.b64encode(src).decode()
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("<server-host>", username="ubuntu", password="<pw>", timeout=25)
c.exec_command(f"echo '{b64}' | base64 -d > ~/.local/bin/hermes-route && chmod +x ~/.local/bin/hermes-route")
```

上传后先 `python3 -c "import ast; ast.parse(open(...).read())"` 做语法校验。

> **PATH 坑**：`~/.local/bin` 只对**登录/交互** shell 生效（`.profile`/`.bashrc`）。非交互 SSH（paramiko）里必须用绝对路径 `~/.local/bin/hermes-route`，否则 `command not found`。

## A.7 扩展指南

### A.7.1 加一个新渠道（推荐路径）

只要多一条 **dict 形式**别名，工具立刻认得：

```bash
hermes config set model_aliases.<别名> \
  '{"model":"<模型id>","provider":"<provider>","base_url":"<端点>","key_env":"<KEY_ENV>"}'
```

注意 `provider` 用目标端点在 Hermes 里的既有 provider 名（如 tokenhub 是 `tencent-tokenhub`），`key_env` 是 `~/.hermes/.env` 里的键名。写完 `hermes-route --list` 应能看到它，再用

```bash
hermes-route --set-limits <别名> --context <窗口> --max-tokens <上限>
```

把该模型的窗口/上限记进表（数值必须查证，见 A.1.2）。

> 别用 `model.aliases` 字符串形式：它不能表达带 `/` 的目标、且无 `key_env` 时会沿用当前 provider 的 key（A.4 的 #83612）。`glm53flash` 就是这么坏掉的（原因见 A.2.2 与 A.4）。

### A.7.2 候选增强（未实现）

- `--snapshot <名>`：把当前路由反向固化成一条 dict 别名（换出去之前先记下"现在这条路"）。
- `--rollback-latest`：手动回滚到最近一次 `.bak-route-*`。
- 别名创建子命令（`hermes-route --add-alias`），把 A.7.1 那两条命令也包进去。
- 窗口/上限的自动探测：`max_tokens` 可以二分试出来，但 `context_length` **无法从 API 探测**（网关 `/v1/models` 不返回窗口大小），仍需查文档或人工确认。
- 多机支持：把 host/路径参数化。

## A.8 已知限制与坑

1. **`api_key` 必须保持字符串 `'${VAR}'` 而非展开值**。本工具通过 subprocess argv 传参（不经 shell），所以不会踩 shell 展开的坑；但**手工**执行时必须加单引号，否则 `${TOKENHUB_API_KEY}` 被 shell 展开成空串、存成空 key → 401（手册记载过的老教训）。
2. **`hermes config set model_aliases.*` 会报 "not a recognized config key"** —— cosmetic，该键不在 schema 白名单里；但 `_load_direct_aliases()` 确实读它。**不要据此判断失败，要用实测（`-z` 或 `--list`）验证。**
3. **验证会真调 API**：每次切换多一次极小请求，并在 `state.db` 留一条微型 cli 会话。频繁切换会积累这类会话记录。
4. **不重启网关是本工具的结论，不是它的动作**：它不做 restart。若某天发现新会话没跟上，先查 `state.db` 的 `gateway_runtime` 与 config 的 mtime，再考虑重启（重启网关需用户明确授权，手册红线）。
5. **回滚是整文件覆盖**：若在"备份之后、失败之前"有别的进程也改了 config，回滚会把那份改动一起抹掉。手工并行改配置时不要同时跑本工具。
6. **cron 重钉范围**：只处理 `jobs.json` 里 `enabled != False` 的任务；已暂停的任务不会被改（这是有意的）。
7. **窗口/上限表按模型 id 索引，不区分端点**：同一个模型 id 若在两个不同端点上限额不同（罕见），表里只能记一组。真遇到就改用字面 id 切换并临时 `--no-limits`。
8. **校验只防手滑，不证真伪**：`--set-limits` 会校验取值范围与 `max_tokens ≤ context_length`，但数值对不对仍要靠查证——GLM 5.x 与 5.3 的窗口差 5 倍就是例子（A.1.2）。而 `context_length` **无法从 API 探测**（网关 `/v1/models` 不返回窗口大小），只能查文档或已知值。
9. **曾经的 `--no-verify` 是历史残留**：早期文档写过这个参数但脚本从未实现，已于 2026-09-23 从文档中移除。验证是安全网的一部分，不提供跳过开关。

## A.9 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-23 | 首版。定位 `~/.local/bin/hermes-route`；三层名字解析；4 键写入 + cron 重钉 + `-z` 验证 + 双文件回滚；`--list/--show/--dry-run/--no-cron/--force`。同日新增 `mimo` dict 别名（custom / api.xiaomimimo.com/v1 / MIMO_API_KEY）使反向切换可用。修掉两处首版 bug：`--show` 的键名映射（`KeyError: 'default'`）、`--force` 未接线。隔离 `HERMES_HOME` 下三场景实测通过。 |
| 2026-09-23 | **加窗口/上限切换**（v0.21 里这两个键是全局单值、无 per-model 写法，切模型时必须显式改写，否则 400 或白白砍输出/提前压缩）。新增工具私有表 `~/.hermes/hermes-route.json`（按模型 id 索引）、`load_limits/save_limits/limits_for/validate_limits/show_limits`、`build_commands()`（唯一决定写哪些键）、`--set-limits/--forget-limits/--no-limits/--context/--max-tokens`；`--show --list` 显示窗口/上限。未知记录时**不猜**：保持原值 + 提示。查证并录入三条路线：mimo / glm-5.3-flash 均 1M·131072，deepseek/deepseek-flash 1M·393216（glm 的窗口经 Z.AI 文档 + 腾讯 TokenHub 清单 + 本机 models_dev_cache 三方核对；曾误以为 200K，那是 GLM-5/5.1/5.2 的值）。五场景实测通过。同步移除文档里从未实现的 `--no-verify`。 |

---

*本文档与实现同源；改代码请同步更新 README 与本文件。*

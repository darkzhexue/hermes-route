# hermes-route

> One-command switch for your Hermes Agent's default model **and** route —
> with cron re-pinning, live verification and automatic rollback.

Hermes Agent 没有内置的"一行切换模型"。`model.default` 只接受**字面模型 id**，
既不解析别名、也不携带路由——把 `model.default` 设成别名名会得到
`HTTP 400: Unsupported model <别名名>`（实测）。跨渠道切换必须同时改
`model.default` / `provider` / `base_url` / `api_key` 四个键，**并且**重新钉选
所有 cron 任务，否则带旧 `model_snapshot` 的任务下次会 fail-closed 拒跑。

`hermes-route` 把这些步骤合成一条命令。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/hermes-route/main/hermes-route.py \
  -o ~/.local/bin/hermes-route
chmod +x ~/.local/bin/hermes-route
```

要求：`python3`（含 `PyYAML`，Hermes 环境自带）、Hermes Agent v0.21+ 装在
`~/.hermes`、CLI 在 `~/.local/bin/hermes`。

## 用法

```bash
hermes-route --list                  # 列出可用别名及各自路由
hermes-route --show                  # 当前 model 段 + 各 cron 任务的钉选
hermes-route glm53flash              # 切换（改 4 键 + 重钉 cron + 端到端验证）
hermes-route mimo                    # 切回
hermes-route mimo-v2.6-pro           # 字面 id：同端点内换模型，保持当前路由
hermes-route glm53flash --dry-run    # 只打印将执行的动作，不写任何文件
hermes-route <名> [--no-cron] [--no-verify] [--force]
```

一条命令会做四件事：

1. 从 `model_aliases:` 的 **dict 形式**别名解析出完整路由（model/provider/base_url/key_env）；
2. 用 `hermes config set` 改写四个键（**不手改 YAML**）；
3. 用 `hermes cron edit` 重新钉选每个启用的 cron 任务；
4. 跑一次真实的 `hermes -z` 端到端验证。

**验证失败会自动回滚** `config.yaml` 与 `cron/jobs.json` 两处并返回退出码 1。

## 准备工作：一条 dict 别名

工具按别名解析路由，所以先为每个渠道建一条 **dict 形式**别名：

```bash
hermes config set model_aliases.<别名> \
  '{"model":"<模型id>","provider":"<provider>","base_url":"<端点>","key_env":"<KEY_ENV>"}'
```

例（腾讯 TokenHub 上的 glm）：

```bash
hermes config set model_aliases.glm53flash \
  '{"model":"glm-5.3-flash","provider":"tencent-tokenhub","base_url":"https://tokenhub.tencentmaas.com/v1","key_env":"TOKENHUB_API_KEY"}'
```

> 别用 `model.aliases` 的**字符串**形式：它无法表达带 `/` 的模型 id
> （值会被按第一个 `/` 拆成 provider/model），且没有 `key_env` 时会沿用当前
> provider 的密钥，可能把密钥发往无关端点（上游 issue #83612）。

## 生效范围

- **新会话立即生效，无需重启网关**（配置在会话创建时快照）。
- 已存在的会话保持各自冻结的旧路由，需在该会话里 `/new` 或 `/model <名>`。

## 名字解析顺序（与 Hermes 内部一致）

| 顺序 | 来源 | 是否换路由 |
|---|---|---|
| 1 | `model_aliases.<名>` **dict** | 是（完整路由） |
| 2 | `model.aliases.<名>` 字符串 | 否（只换模型名） |
| 3 | 字面模型 id | 否（保持当前路由） |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（或 dry-run 完成） |
| 1 | 写入失败已回滚 / 验证失败已回滚 / `--force` 下验证未过但保留 |
| 2 | 找不到 config.yaml 或名字无法解析 |

## 文档

- [开发手册](docs/hermes-route开发手册.md) —— 问题背景与实测证据、设计、代码结构、
  依赖的 Hermes 内部契约、隔离测试方法、部署、扩展指南、已知限制。

## 许可

未附许可。如需开源请自行添加（例如 MIT）。

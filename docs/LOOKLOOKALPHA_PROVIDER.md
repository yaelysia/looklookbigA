# looklookAlpha Provider Contract

本文件说明 `looklookbigA` 提供给 `looklookAlpha` / 其他 Agent consumer 的稳定 provider contract。该层只提供事实、来源元数据以及确定性计算，不负责投资判断、买卖建议、仓位或其他决策逻辑。

机器可读合同以 `contracts/looklookalpha-provider-v1.json` 为准；本文解释它的使用和发布边界。

## 稳定身份

生产 consumer 不应依赖可变的 `master` 或“最新 artifact”。稳定接入需要同时固定并验证：

- stable ref：`v1`；
- 对应精确 commit SHA；
- snapshot schema / capability manifest；
- reusable / realtime workflow identity；
- frozen fixtures；
- 实际 provider operation 与 primary artifact identity。

在实现只存在于 `master` 而尚未通过 `master → v1` 发布 PR 晋升之前，它只能作为开发版本验证，不能视为 Alpha 的 production stable contract。

## Snapshot capability contract

`contracts/looklookalpha-provider-v1.json` 当前要求 snapshot schema 至少为 16，并要求以下能力存在：

- realtime quote + live/current-price guard；
- intraday structure；
- daily K context；
- market environment；
- provenance / Source Trust / freshness / quality；
- `changes_since_previous`；
- company events；
- capital flow；
- fundamentals；
- `provider_contract`。

`scripts/alpha_capability_contract.py` 负责验证 snapshot 是否满足稳定 capability profile。consumer 不应只比较 schema number；schema 相同但能力清单缺失仍应判为不兼容。

## Semantic command identity 与 dispatch identity

两类身份必须严格分离。

### Semantic command identity

`command_digest` 代表 Alpha 的完整语义命令，不由 `looklookbigA` 根据自己能看到的少量 workflow input 重新定义。其语义等价于 Alpha 的 `RefreshCommandCanonicalBody`，至少覆盖：

```text
command_schema_version
provider/workflow identity + version/SHA
requirements                      # deterministic order
subjects                          # deterministic order
temporal mode + fixed cutoff when applicable
execution mode
material provider/workflow inputs
requested payload versions
```

规范公式为：

```text
domain = "looklookAlpha.refresh-command.v1"
SHA256(
  domain
  || 0x00
  || command_schema_version
  || 0x00
  || RFC8785_JCS(canonical_command_body)
)
```

生产路径中 Alpha / caller 是 RFC8785/JCS canonicalization 与 semantic digest 的权威来源。`looklookbigA` **不需要也不应**根据 `workflow_identity + mode` 等局部信息重新计算该 digest。`scripts/alpha_refresh_contract.py` 中的 digest helper 只用于固定测试向量/跨合同 fixture；provider 的 workflow、registry 和 snapshot runtime 都只接受 caller 提供的 digest。

provider runtime 只做：

1. 校验 `command_digest` 是 64 位小写 hex；
2. 原样持久化并用于 correlation / idempotency；
3. 独立记录 provider 实际可见的 `workflow_identity` 与 `material_execution_inputs`；
4. 如果同一 idempotency/digest 被再次提交但 provider 可见 material inputs 与已保存记录矛盾，fail closed，而不是静默复用。

下列 dispatch / persistence metadata **不属于 semantic command identity**，改变它们不得改变 semantic digest：

```text
dispatch_correlation_id
idempotency_key
deadline / submitted time
caller-local Job id
retry attempt
local persistence object id / revision / created_at
```

### Dispatch identity

Provider dispatch/recovery 使用：

```text
caller-supplied semantic command_digest
+ idempotency_key
+ dispatch_correlation_id
+ exact workflow_identity
+ auditable material_execution_inputs
→ exact logical provider operation/run
```

`workflow_identity` 和 `material_execution_inputs` 会在 provider registry 中单独审计，但它们不会被 provider 再次压缩成另一种“semantic digest”。

## start_or_recover

入口：

```text
scripts/alpha_start_or_recover.py
```

持久 registry：

```text
branch: provider-operations
path:   .provider-operations/refresh-registry-v1.json
```

协议顺序：

```text
consumer durable canonical command + dispatch intent
→ provider durable reservation
→ dispatch workflow
→ workflow claims its exact run id
→ resolve exact run
→ resolve exact primary artifact
```

核心语义：

- 相同 `idempotency_key` + 相同 semantic `command_digest` + 一致 provider-visible material inputs：恢复同一个逻辑 operation；
- 相同 key + 不同 semantic digest：冲突并拒绝；
- 相同 correlation + 不同 semantic digest：correlation mismatch 并拒绝；
- 相同 digest 但实际 provider-visible material execution inputs 与已保存记录矛盾：视为调用方合同错误并拒绝；
- 已有 reservation 但尚未绑定 run 时，lease 到期后也不能直接 redispatch；
- 必须先按 correlation 对 GitHub Actions workflow runs 做 fresh scan；
- scan 找到唯一精确 run 时恢复它；
- 多个匹配 run 时视为 ambiguous，禁止猜测；
- 只有 fresh scan 确认没有匹配 run，才允许重新获得 dispatch permit。

因此 provider 明确禁止：

```text
refresh → read latest workflow run / latest artifact
```

这条规则用于覆盖“GitHub dispatch 已成功，但调用方在保存 run id 前崩溃或网络响应不明确”的场景。

## GitHub workflow dispatch inputs

`.github/workflows/realtime-quotes.yml` 的 Alpha refresh path 接受：

```text
mode
dispatch_correlation_id
idempotency_key
command_digest
workflow_identity
```

四个 identity 字段必须成组提供。workflow 会：

- 验证 correlation / idempotency identifier 格式；
- 验证 `command_digest` 为合法 64-hex semantic digest；
- 验证 `workflow_identity` 指向当前 workflow 的精确 SHA；
- **不会**从 `workflow_identity + mode` 重新计算或替换 caller 的 semantic digest；
- 把实际 `mode` 作为 provider-visible material execution input 交给 registry 审计；
- 由 `claim-alpha-refresh` 把本次 `GITHUB_RUN_ID` 绑定到 durable reservation。

普通非 Alpha 的 realtime 执行没有这些 identity 字段时仍走原有行为，不能因此改变现有实时行情语义。

## Operation status

对外状态统一为：

```text
ACCEPTED
QUEUED
RUNNING
SUCCEEDED
FAILED
CANCELLED
```

terminal 状态来自 exact GitHub Actions run，而不是另一个“最新成功 run”。调用方 timeout 与 provider 的 FAILED / CANCELLED 必须保持为不同语义。

## Primary artifact contract

成功 operation 必须解析到恰好一个 primary artifact：

```text
artifact name: realtime-snapshot
files:
  snapshot.json
  alpha-artifact-manifest.json
```

`alpha-artifact-manifest.json` 由 provider 生成，至少记录：

- `primary_artifact_count = 1`；
- artifact name；
- `snapshot.json` path / size；
- SHA-256 digest；
- digest source；
- workflow run id / attempt；
- producing ref / commit SHA；
- workflow ref / workflow SHA；
- produced timestamp。

consumer 应下载 snapshot 后重新计算 digest 并与 manifest 对比。以下情况都必须视为 invalid，而不是猜测：

- 没有 primary snapshot；
- 有多个冲突 primary artifact；
- manifest 缺失或身份不一致；
- digest mismatch / corruption；
- artifact 来自错误 workflow run。

## Frozen fixtures

兼容性 fixture：

```text
fixtures/looklookalpha/provider-v1-snapshot.json
fixtures/looklookalpha/provider-v1-metadata.json
```

fixture 用于冻结 schema/capability、Trust tier、Freshness SLA、lag dimensions、current-price guard 和 operation semantics。更新稳定合同必须同步更新 fixture 和回归测试，不能只改文档或 schema number。

## 验证

核心回归入口：

```bash
python3 scripts/test_alpha_provider_contract.py
python3 scripts/test_alpha_semantic_command_identity.py
```

其中 semantic-command 回归明确验证：

- domain + command schema version + canonical body 的 digest test vector；
- dispatch correlation / idempotency / deadline / retry / persistence metadata 变化不改变 semantic digest；
- workflow identity、requirements、subjects、temporal semantics、execution mode、material provider inputs、requested payload versions、command schema version任一变化都会改变 digest；
- provider registry 原样保存 caller digest，不以局部 workflow input 重定义它；
- 同一 digest 与矛盾 provider-visible material inputs 同时出现时 fail closed；
- snapshot audit metadata 原样携带 caller semantic digest。

这些回归进入 pre-merge security gate、reusable selftest 和 v1 smoke。稳定发布还必须保留 live-price guard、provenance / trust / freshness 与原有完整 smoke。

## 发布到 v1

provider contract 的稳定发布严格分两阶段：

1. 功能 PR 先进入 `master`，在 `master` 上完成完整 CI / smoke / provider-contract 验证；
2. 再创建独立 `master → v1` 发布 PR，检查 stable diff 后使用普通 merge commit 晋升。

对于 Issue #54，master 实现 PR 合并并不代表 Issue 完成。只有 `master → v1` promotion 合并、稳定 ref 与精确 SHA 已确认、v1 smoke / provider contract regression 通过后，才能认为 stable provider contract 已发布并关闭该 Issue。

生产 consumer 在此之前不得依赖 `master` 独有字段或 `latest` 语义。

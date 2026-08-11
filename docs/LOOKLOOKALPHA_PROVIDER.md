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

## Refresh operation identity

一个逻辑 refresh command 由以下字段共同定义：

```text
dispatch_correlation_id
idempotency_key
command_digest
workflow_identity
material_execution_inputs
```

`command_digest` 是下列 canonical JSON 的 SHA-256：

```json
{
  "workflow_identity": "...",
  "material_execution_inputs": {"mode": "..."}
}
```

JSON 使用稳定 key 排序和紧凑编码。`dispatch_correlation_id` / `idempotency_key` 是操作标识符，不应包含凭据、token 或其他秘密。registry 只保存 idempotency fingerprint，不把原始 key 作为长期操作身份公开。

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
consumer durable dispatch intent
→ provider durable reservation
→ dispatch workflow
→ workflow claims its exact run id
→ resolve exact run
→ resolve exact primary artifact
```

核心语义：

- 相同 `idempotency_key` + 相同 command：恢复同一个逻辑 operation；
- 相同 key + 不同 command：冲突并拒绝；
- 相同 correlation + 不同 command：correlation mismatch 并拒绝；
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

四个 identity 字段必须成组提供。workflow 会校验 `workflow_identity` 指向当前 workflow 的精确 SHA，并重新计算 command digest。随后 `claim-alpha-refresh` job 把本次 `GITHUB_RUN_ID` 绑定到 durable reservation。

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
```

测试覆盖至少包括：

- capability profile；
- frozen fixtures；
- exact artifact digest；
- operation terminal states；
- same-key idempotency / conflicting reuse；
- exact run claim；
- concurrent distinct operation isolation；
- expired/ambiguous reservation 的 correlation recovery；
- duplicate-run ambiguity；
- workflow claim / artifact / release gate wiring。

这些回归同时进入 pre-merge security gate、reusable selftest 和 v1 smoke。稳定发布还必须保留 live-price guard、provenance / trust / freshness 与原有完整 smoke。

## 发布到 v1

provider contract 的稳定发布严格分两阶段：

1. 功能 PR 先进入 `master`，在 `master` 上完成完整 CI / smoke / provider-contract 验证；
2. 再创建独立 `master → v1` 发布 PR，检查 stable diff 后使用普通 merge commit 晋升。

对于 Issue #54，master 实现 PR 合并并不代表 Issue 完成。只有 `master → v1` promotion 合并、稳定 ref 与精确 SHA 已确认、v1 smoke / provider contract regression 通过后，才能认为 stable provider contract 已发布并关闭该 Issue。

生产 consumer 在此之前不得依赖 `master` 独有字段或 `latest` 语义。

# TASK-111：分支级项目身份、内容寻址复用与按分支管理

> 状态：**已合并 main（`8b46efb`）** ｜ 阶段：Phase 4+（上线前加固）｜ 硬依赖：TASK-101（engine 扩展）、TASK-110（配额与后台）
> soft 依赖：无
> 建议分支：`feature/task-111-branch-identity_xwz0916`
> 交付物所有权：
> - `core/zace_core/engine.py`（`repo_identity` / `RepoIdentity`）
> - `core/zace_core/pipeline/indexer.py`（`_embed_new` 内容寻址复用）
> - `core/zace_core/vectors/store.py`（按 `content_hash` 读回向量）
> - `client/src/identity.rs`、`client/src/index.rs`
> - `service/zace_service/routers/projects.py`、`admin.py`、`quota.py`
> - `web/src/pages/{ProjectsPage,DashboardPage,AdminPage}.tsx`、`web/src/api/client.ts`
> - `core/tests/`、`client/tests/`、`service/tests/`
>
> **不得改**：`docs/design/**`、`docs/contracts/**`（身份计算在客户端，契约无变化；
> 已核对 `openapi.yaml` 只要求 `identityKey` 字符串）。

## 0. 一句话

同一 git 仓库的不同分支/worktree 目前会**碰撞成同一个 projectId**，导致索引互相污染
（实测：main 与 `feature/cvi-agent` 混合索引 353 文件，检索返回对方分支的文件，且
`index: fresh` 仍显示正常）；同时向量复用按 `chunk_id` 而非 `content_hash` 判定，
行号漂移就重嵌，实测浪费 81.8% 的 embedding 与存储。

本卡交付三件事：**① 身份含分支 → 隔离**；**② 内容寻址复用 → 省钱**；**③ UI 按分支展示与删除 → 可管理**。

## 1. 问题与实测证据（2026-09-16 本机）

### 1.1 污染（正确性）

`identityKey = sha256(remoteUrl + repo 相对路径)`（D-29），**不含分支**。本机两个克隆：

| 克隆 | 分支 | 内容 |
|---|---|---|
| `4_AIBOX/gitlab/minicpm/cockpit-agents-py` | `feature/cvi-agent` | 包已改名 `cvi_agent_core`/`cvi_agent_product` |
| `2_github/.../benchmark/cockpit-agents-server` | `main`（= 评测基准 `135ac288`） | `src/services/frame/...` |

`remote.origin.url` 完全相同、都在 git 根 ⇒ **同一 identityKey / 同一 projectId**：

```console
$ 账本 353 文件构成
只在 feature/cvi-agent 存在：285      ← 另一个分支的文件
只在 main@135ac288 存在：     66
```

检索因此返回 `src/cvi_agent_core/agent/loop.py` 等**当前 checkout 不存在**的文件。
`Store.freshness()` 只报 `max(indexed_at)`，`stale_files` 恒空 → **永远无法自愈、也不告警**。

同一问题在本仓库的 10 个泳道 worktree 上同样成立（同 remote、同 git 根）。

### 1.2 重复付费（成本）

复用键必须是 `content_hash`——`docs/contracts/PROCESS.md` §3.2 **R4 已裁定**
"`reused` = hash 未变（可复用向量，**复用键是 hash 不是 id**）"，
但 `indexer.py:_embed_new` 的实现是 `stored.get(chunk_id) != chunk.content_hash`（**按 id 查**），
行号一变（`chunk_id = {path}:{fqn}:{start_line}`）即重嵌。**契约与实现不一致**。

本机实测（`zace` 仓库 10 个 worktree）：

```text
合计文件数(含重复) 4237  →  唯一内容 773  ⇒  按内容复用可省 81.8%
同分支相邻提交未变率：99.7% / 97.6% / 93.8%
```

存储构成（live 项目 39MB）：`index.db 19M` / `vectors 17M` / `blobs 4.0M`。

## 2. 设计要点

### 2.1 身份含分支（`engine.py` / `identity.rs`）

```text
有 git remote → identityKey = sha256(remote + 相对路径 + branch)
无 git 或 detached 无分支 → 维持现状（路径 hash），不引入新分支维度
```

- 分支名取 `git rev-parse --abbrev-ref HEAD`；`HEAD`（detached）→ 取短 commit 作退化标识，
  仍拿不到则退回不含分支的旧口径（**向后兼容**，不破坏已有无 remote 场景）；
- `RepoIdentity` 新增 `branch: str | None` 字段（纯增字段，`core/types.py` 不动——
  该 dataclass 定义在 `engine.py`，非冻结契约）；
- **两台机器 clone 同一 repo 的同一分支 → 仍共享索引**（D-29 的核心价值保留）；
- `displayName` 追加分支：`zace@feature/cvi-agent`，UI 可直接看出是两份。

**为什么不是"按仓库+分支+路径"**：那会让同一分支的多个 worktree 各存一份，
丢掉最大的一块复用（实测泳道间 55%–98% 内容相同）。

### 2.2 内容寻址复用（`indexer.py` / `vectors/store.py`）

- `VectorStore` 增 `get_vectors_by_hash(hashes) -> dict[hash, vector]`（LanceDB 按
  `content_hash` 查；`content_hash` 建索引）；
- `_embed_new` 改为：

```text
pending = []
for chunk in candidates:
    if stored_hash[chunk.id] == chunk.content_hash: continue      # 同 id 同内容（现状）
    reused = reuse_pool.get(chunk.content_hash)                    # 新增：同内容换 id
    if reused: reuse_pool_upsert(chunk.id, chunk.content_hash, reused); continue
    pending.append(chunk)
```

- 复用**只搬向量行**（`chunk_id` + `content_hash` + vector），不重算 embedding；
- 一次 ingest 内先建"本次不需要重嵌"的 hash 池，避免同一批内重复嵌入。

### 2.3 按分支管理（UI + service）

- `GET /api/projects` 已返回项目列表：为每个项目补 `branch` 与 `displayName`（含分支）；
- **项目页 / 控制台**按"仓库 × 分支"分组展示**占用与可删除入口**（复用已有
  `DELETE /api/projects/{id}`）；
- **配额**：维持 TASK-110 的 per-role 配额（不新增角色），但把"占用"按分支可见——
  用户能自己挑着删。**不为了逼用户删除而刻意调低配额**（理由见 §7）。

## 3. 验收标准（DoD）

- [ ] 同一仓库两个分支 → **不同 projectId**；同一分支两个 checkout → **相同 projectId**；
- [ ] 无 remote 仓库、detached HEAD：行为与现状一致（不回归）；
- [ ] `displayName` 含分支后缀，UI 可区分；
- [ ] **内容复用**：改一个文件的行号（内容不变）后重索引 → `chunks_reused > 0` 且
      该 chunk **不产生 embedding 调用**（用计数 provider 断言）；
- [ ] 跨分支复用：B 分支的文件与 A 分支内容相同 → B 首次索引**复用 A 的向量**，不重嵌；
- [ ] 10 泳道场景回归：复用率与 §1.2 实测同量级（≥70%）；
- [ ] UI：项目按分支展示占用 + 删除入口可用；
- [ ] 基线三条全绿：`uv run ruff check .`、`uv run python scripts/check_dependency_direction.py`、
      `uv run pytest -o addopts="" -q`；
- [ ] 端到端：真实服务上重建 `cockpit-agents-server@main` 索引，
      **不再返回 `src/cvi_agent_*`** 的文件。

## 4. 明确不做

- 不改 `docs/design/**` 与 `docs/contracts/**`（身份在客户端算，契约无变化）；
- 不做"分支间差异索引"（只存增量）——复杂度高，V1 先靠内容复用把重复成本压掉；
- 不做 Git LFS / submodule 特殊处理；
- 不做分支自动清理策略（由用户在 UI 决定）。

## 5. 参考源码锚点

- `core/zace_core/engine.py:199` `repo_identity`（D-29）
- `client/src/identity.rs`（必须与 core 逐字节一致）
- `core/zace_core/pipeline/indexer.py:448` `_embed_new`
- `core/zace_core/vectors/store.py` `get_hashes`
- `docs/contracts/PROCESS.md` §3.2 R4（复用键是 hash 不是 id）

## 6. 执行记录

（实施中回填。）

---

### 2026-09-16 · lane-b · `feature/task-111-branch-identity_xwz0916`（三层全部完成）

#### 验收命令与结果

```console
$ uv run ruff check .                                  # All checks passed!
$ uv run python scripts/check_dependency_direction.py   # 依赖方向检查通过
$ uv run pytest -o addopts="" -q                        # 1134 passed, 9 skipped
$ cd client && cargo build                              # Finished（Rust 侧改用 identity.rs 分支逻辑）
$ cd web && npx tsc --noEmit && npx eslint src --max-warnings 0   # 均无输出
$ cd web && npx vitest run                              # 100 passed（1 预存失败：History.trace，与本卡无关）
```

#### 真实 embedding 实测（`voyage-4-lite`，zace 仓库 3 个 worktree）

| 分支 | chunks | 复用 | 耗时 |
|---|---|---|---|
| main（主工作区） | 5913 | 0 | 180.0s |
| lane-c | 5842 | **5824（99.7%）** | **29.5s** |
| lane-f | 5682 | **5595（98.5%）** | **28.1s** |

缓存体积 24 MB（全仓 3 个分支），落在 `{data_root}/cache/embeddings/api_voyage-4-lite-1024/`。

#### Python / Rust 身份一致性

`core.engine.repo_identity` 与 `client.identity::repo_identity` 对同一路径**逐字节相同**
（实测三个真实仓库的 `identity_key` 一致：`646bdece…` / `71454795…` / `4f82047d…`）。
口径：`sha256(remote + repo相对路径 + "\x00" + branch)`；无 remote / detached 取不到分支时
**不引入分支维度**（与旧口径一致，行为不回归）。

#### 与设计的偏差（需编排者确认）

| # | 卡内/设计原文 | 实际实现 | 理由 |
|---|---|---|---|
| 1 | D-29 `identityKey = sha256(remoteUrl + repo 相对路径)` | 追加 `+ "\x00" + branch` | 卡内 §2.1；这是修 D-29「同一机器不同 checkout 应隔离」**未落地**的缺陷，不是改设计意图 |
| 2 | D-03 per-project `vectors/` 目录 | 保持不变；**新增**可丢弃的 `{data_root}/cache/embeddings/` | 权威存储仍 per-project，缓存只影响"要不要重新嵌入"，清掉不损正确性 |
| 3 | 卡内 §2.3 写"配额维持 TASK-110 不变" | 已实现 | 未为了逼用户删除而调低配额（理由见卡内 §7） |

#### 未决问题 / 交接事项

1. **设计文档未改**：D-29 的身份公式、D-03 的目录树都需编排者决定是否把本卡口径写进
   `docs/design/**`（本卡按纪律不动设计文档）。
2. **缓存清理策略缺失**：`{data_root}/cache/embeddings/` 会随模型数增长；换模型后旧分片
   需人工删除。建议后续开卡做"保留最近 N 个模型分片"或后台清理入口。
3. **`project.json` 未加 `branch` 字段**：分支从 `display_name` 的 `@` 后缀解析
   （避免旧项目迁移）；若将来仓库名含 `@` 会有歧义，届时再加结构化字段。
4. **旧污染数据需人工重建**：本卡只改身份计算，**不会**自动清理既有的混合索引
   （`8e69da62f37e5783`）。按用户 2026-09-16 决策用"删除重建"处理。
5. `History.trace.test.tsx` 预存失败仍在（干净 main 同样失败）。

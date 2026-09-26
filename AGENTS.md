# zace 仓库协作规则（实施 AI 必读）

本文件约束所有在本仓库工作的 AI 会话（含子 AI）。与 `docs/design/INDEX.md` §3 决策登记表冲突时，以决策登记表为准；本文件只讲"怎么干活"，不复述"设计成什么样"。

## 0. 工作区隔离（2026-09-13 新增，**开工前第一件事**）

**一个 AI 会话 = 一个独占工作区（git worktree）= 一个分支；主工作区只用于集成。**

```bash
cd /home/xuwenzheng/github/ACE/zace
bash scripts/lane-worktrees.sh status          # 看哪个 lane 空闲
bash scripts/lane-worktrees.sh claim <lane> TASK-xxx   # 认领（写 .lane-owner）
cd /home/xuwenzheng/github/ACE/zace-lane-<lane>       # 切到自己的工作区
```

- **绝不在主工作区（`/home/xuwenzheng/github/ACE/zace`）里改代码或提交**；
  多个会话共用同一目录会导致 `git commit` 落到“别人最后切到的分支”上（实测已造成提交错位与一次重做）；
- **本机（VPS）实际路径**：仓库根 = `/root/xuwenzheng/ace/zace`（上面示例里的 `/home/...` 属另一台机器）；
  `scripts/lane-worktrees.sh` 的 lane 目录可能指向已失效的旧路径，**以 `git worktree list` 为准**；
  本轮实践：`git worktree add -b <branch> /root/xuwenzheng/ace/zace-<用途> main`，收工 `git worktree remove` + `git branch -d`；
- 收工：回填记录 → 提交 → **默认不 push**（推送由编排者决定；本仓当前由用户授权 AI 直接 `git push origin main`，禁止 `--force`）→ `bash scripts/lane-worktrees.sh release <lane>`；
- 完整规程与事故记录：`docs/plan/multi-ai-worktrees.md`。

## 1. 开工前必读

1. `docs/design/INDEX.md`（文档地图 + 决策登记表 + 术语表）；
2. 你认领的任务卡 `docs/tasks/TASK-xxx.md`（含依赖、冻结接口、验收命令、参考锚点）；
3. 任务卡"输入文档"节列出的具体 Module 章节（只读所需章节，不全量读）。

参考项目源码在仓库外部工作区 `../source/`（ACE 工作区，只读）：
- 若本机存在 `../source/`，按任务卡"参考锚点"查阅；**禁止修改**；
- 若无（例如只 clone 了 zace 仓库），按任务卡内摘录与 `docs/design/Background/` 的源码路径锚点理解。

## 2. 硬性边界

- **不改设计文档**（`docs/design/**`）：发现设计问题在 PR 中标注并提出，由编排者裁决；只有"修订记录/状态"行随决策更新。
- **不改冻结契约**（`docs/contracts/**`、`core/zace_core/types.py`、`core/zace_core/interfaces.py`）：契约变更必须走 `docs/plan/orchestration.md` §4 流程。
- **不越界改文件**：每张任务卡有"交付物（文件所有权）"清单；清单外的文件不要改（公共文件改动先在任务卡的"执行记录"里提出）。
- **不修改 `source/` 下任何参考项目代码**。
- **改性能旋钮要留档**：`EMBED_BATCH_SIZE` / `EMBED_CONCURRENCY` / `ZACE_EMBED_WORKERS` 等随硬件变化的取值，
  改完必须同步更新 `configs/profiles/<机器标识>.env` 并回填实测（口径与"窗口不能按消费者切小"的告警见
  `configs/profiles/README.md`）。
- 不引入与任务无关的新依赖；依赖加入 `core/pyproject.toml` / `service/pyproject.toml` 时注释说明对应任务与决策编号。

## 3. Git 纪律

- **工作区分高**（§0）：一个会话一个 worktree，禁止共用一个目录。
- 每张任务卡一个分支：`feature/task-<编号>_<缩写><MMDD>`（如 `feature/task-001_xwz0910`）；泳道模式下第一张卡从 `main` 开分支，后续卡从上一张卡的分支串联创建。
- 不直推 `main`、不使用 `git push --force`；泳道模式下只需**本地提交**（无需 push/PR），评审与合并由总览 AI 执行。
- 一次提交只做任务卡范围内的事；提交信息用英文祈使句 + 任务号，例：`task-001: add sqlite schema and FTS5 writer`。
- **不在共享工作区用 `git add -A`**：它会把别人未提交的改动一起卷进你的提交。只 `git add` 自己的文件。
- 共享文件（`docs/tasks/README.md` / `docs/contracts/PROCESS.md` / 根 `pyproject.toml`）**只做最小改动**，不要顺手重排格式（会让别人的 diff 全部冲突）。
- 不提交运行时产物（数据库、`.zace/`、构建输出）；不提交任何 secret/token。

## 3.1 发布 zace-client（npm）

**只有用户明确指定版本号（如“发布 zace-client 0.0.8”）时才允许发布。**
禁止 Agent 自行 bump 版本号、自行创建 tag，禁止为了验证脚本而触发真实发布（改代码 ≠ 发版）。

用户给出明确版本号后，只跑这一条命令：

```bash
bash scripts/release-client.sh x.y.z
```

脚本只做本地准备（检查 main/干净/tag 不存在 → 改版本号 → 同步平台子包 → 一致性检查
→ commit → push main → push tag）后退出，**不等 CI**。

- 六平台构建与 npm publish **全部由 GitHub Actions 完成**；禁止本地 publish；
- 禁止 `--force`、禁止移动已有 tag、禁止覆盖/回退版本号（失败就升 patch）；
- 禁止读取、打印或提交 `NPM_TOKEN`；
- npm 包名用 `windows-*`，`os` 字段必须是 `win32`；
- 禁止 GitHub Release 二进制 fallback（npm 是唯一二进制分发渠道）。

详细说明见 [`docs/handbook/release/npm.md`](docs/handbook/release/npm.md)。

## 4. 验证纪律

- 任务卡"验收标准"里的命令必须本地跑通，并把关键输出贴进 PR/完成报告；测试失败不得声明完成。
- 新增逻辑必须有单元测试；测试数据用临时目录/内存库，禁止依赖本机绝对路径。
- 全仓库基线检查必须保持绿：`uv run ruff check .`、`uv run python scripts/check_dependency_direction.py`、`uv run pytest`。
  **注意**：根 `pyproject.toml` 设了 `addopts = "-q"`，直接跑 `uv run pytest` **不打印** passed/failed 汇总行；要看数字用 `uv run pytest -o addopts="" -q`。

## 5. 完成后必须回填

1. 任务卡底部"执行记录"：日期 + 分支 + 验收命令与结果 + 与设计的偏差 + 未决问题；
2. `docs/tasks/README.md` 任务板对应行状态改为 `review`；
3. PR 描述按 `docs/plan/orchestration.md` §3 的报告模板写。

## 6. 与设计文档冲突时

- 实现与 Module 设计冲突：**先停下来**在任务卡"执行记录/未决问题"里写清冲突点与建议，交编排者裁决；不得静默偏离设计。
- 代码与设计最终不一致且已上线：由编排者更新设计文档并在 `docs/design/INDEX.md` §4 记录漂移。

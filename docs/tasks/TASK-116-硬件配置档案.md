# TASK-116：硬件配置档案（`configs/profiles/`）

> 状态：review ｜ 阶段：Phase 5+（工程化） ｜ 硬依赖：TASK-115 ✅ ｜ soft 依赖：无
> 建议分支：`feature/task-116_config-profiles0926`
> 交付物所有权：`configs/profiles/**`（新增）、`docs/tasks/README.md`、`docs/handbook/deployment/vps.md`
> （只加指针 + 一个键）、本卡。清单外文件不得改。

## 目标

把**随硬件变化**的调优旋钮（`EMBED_BATCH_SIZE` / `EMBED_CONCURRENCY` / `ZACE_EMBED_WORKERS`，
以及 `EMBED_BATCH_TOKEN_BUDGET` / `EMBED_MAX_INPUT_TOKENS`）从"散落在各机器 `/etc/zace/zace.env`
与文档正文"变成**按机器命名、进 Git 留档的档案**，让换机（或回滚到上一台机器的参数）有据可依。

- 命名约定：**档案名 = 机器标识**；现役 VPS 档案名 = `154.12.34.214`（用户 2026-09-26 指定）；
- 未上机的目标机用规格占位名（`dmit-2c2g-200m`），上机后按 IP 复制新档、占位档留作历史；
- 档案只放**非密钥**取值——密钥仍只留在 `/etc/zace/zace.env`（0600，永不进 Git）。

## 交付物

| 路径 | 内容 |
|---|---|
| `configs/profiles/README.md` | 旋钮表（含义 / 缺省 / 内存与时间效应）、命名约定、用法（source 顺序 / systemd `EnvironmentFile`）、档案清单、内存实测与估算方法 |
| `configs/profiles/154.12.34.214.env` | 现役 VPS 的已实测冻结档（窗口 2000、K=2、MemoryMax=1400M） |
| `configs/profiles/dmit-2c2g-200m.env` | 计划中的 DMIT 2C2G/200Mbps 占位档（含上机后必做的三件事） |
| `docs/handbook/deployment/vps.md` | §3 指向档案目录 + 补上此前缺失的 `ZACE_EMBED_WORKERS` |

## 验收标准（DoD）

- [x] `set -a; source configs/profiles/154.12.34.214.env; set +a` 后 `EmbeddingConfig.from_env()` 与
      `embed_workers()` 读到的就是档案值（实测 `batch=500 concurrency=4 workers=2 window=2000`）；
- [x] 档案里**没有**任何密钥/令牌（人工核对 + 无 `KEY=`/token 形态）；
- [x] README 给出"不要把窗口按消费者切小"的明确告警（TASK-115 的负结果）；
- [x] 手册 §3 有指针，且不再漏 `ZACE_EMBED_WORKERS`。

## 明确不做

- 不在代码里引入 `ZACE_PROFILE` 之类的加载器（部署走 systemd `EnvironmentFile`，本地走 `source`；
  档案是纯数据，不给 core 增加启动期副作用）；
- 不改任何默认值（`ZACE_EMBED_WORKERS` 仍是 2），本卡只做命名与留档；
- 不动冻结契约、不动 `/etc/zace/zace.env`（生产文件由用户按 README 的 systemd 片段接入）。

## 执行记录

### 2026-09-26 ｜ 分支 `feature/task-116_config-profiles0926`

- 核实旋钮来源：`core/zace_core/embedding/factory.py`（`EMBED_*`）、
  `core/zace_core/pipeline/embedding_sink.py`（`ZACE_EMBED_WORKERS`，TASK-115）、
  `core/zace_core/engine.py`（`ZACE_EMBED_CACHE`）、`pipeline/ignore.py`（索引范围阈值）；
  均已是环境变量驱动，本卡只做**命名/留档/单点说明**。
- 现役 VPS 的实测取值来自 `/etc/zace/zace.env`（2026-09-15 冻结批参数）+ TASK-115 的 K 默认值；
  systemd 内存护栏（`MemoryHigh/MemoryMax`）不在环境变量里，按"留档注释"写进档案。
- 已提示的运维动作（用户侧）：把 `ZACE_EMBED_WORKERS=2` 显式写进 `/etc/zace/zace.env`，并按 README
  的片段加第二个 `EnvironmentFile=`（现役机器即使不写也是 2，写出来是为了留档与可回滚）。

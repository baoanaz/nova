/**
 * CF-05 响应类型（手写，只覆盖本卡消费的端点）。
 *
 * 为什么不引 openapi 代码生成：V1 只消费 6 个端点，生成器的依赖与产物维护成本高于收益。
 * 字段名与 `docs/contracts/openapi.yaml` **逐字一致**；改这里等于改前端契约，
 * 必须同时对照 `service/nova_service/packmeta.py` 的 `_META_FIELDS`。
 */

/** CF-05 错误信封（`service/nova_service/errors.py`）。 */
export interface ErrorEnvelope {
  error: { code: string; message: string };
}

/** `GET /healthz`（TASK-034 §D 追加 projects 进度）。 */
export interface Health {
  status: string;
  version: string;
  dataRoot: string;
  localMode: boolean;
  /** `disabled(local)` / `enabled`（TASK-060 引入真实鉴权后为 `required`）。 */
  auth: string;
  core: { importable: boolean; ok?: boolean; modelId?: string; dim?: number; reason?: string };
  projects: HealthProject[];
}

export interface HealthProject {
  projectId: string;
  attachedRoot?: string | null;
  indexProgress?: IndexProgress | null;
}

/** `indexProgress` 六字段（TASK-034；口径见 M2a 手册 §2，`processedFiles` 与 `totalFiles` 不同量纲）。 */
export interface IndexProgress {
  state: "idle" | "running" | "done" | "failed";
  startedAt: number | null;
  finishedAt: number | null;
  processedFiles: number;
  totalFiles: number;
  error: string | null;
}

export interface SyncStatus {
  filesIndexed?: number;
  chunks?: number;
  symbols?: number;
  edges?: number;
  pendingJobs?: number;
  lastIndexedAt?: number | null;
  branch?: string | null;
  commit?: string | null;
  blobs?: { count: number; bytes: number };
}

export interface Project {
  projectId: string;
  displayName?: string;
  createdAt?: number;
  /**
   * 分支（TASK-111）：同一仓库的不同分支是**不同项目**（分支参与 D-29 身份计算）。
   * `null`/缺失表示该项目没有分支信息（无 git、detached 取不到、或旧数据）。
   */
  branch?: string | null;
  attachedRoot?: string | null;
  indexProgress?: IndexProgress | null;
  sync?: SyncStatus;
  /**
   * 索引数据的磁盘占用（TASK-094 §A：`{data_root}/projects/{id}` 递归求和，**不含源码仓库**）。
   * `null`/缺失表示后端未提供（页面显示 `—`，不伪装成 0）。
   */
  diskBytes?: number | null;
}

/** `meta`（TASK-032 冻结字段集；`service/nova_service/packmeta.py`）。 */
export interface PackMeta {
  projectId: string;
  query: string;
  mode: string;
  checkpointId: string | null;
  answerable: boolean;
  confidence: string | null;
  channelsUsed: string[];
  degraded: boolean;
  degradedReason: string | null;
  candidateCount: number;
  freshness: {
    indexedAt: number | null;
    staleFiles: string[];
    indexingFiles: string[];
    rescanError?: string;
  };
  budget: {
    usedTokens: number;
    hardCap: number;
    truncated: boolean;
    omittedCount: number;
  };
  evidenceCount: number;
  docsCount: number;
  flowsCount: number;
  missingEvidence: { code: string; message: string; symbol: string | null }[];
  pack: unknown | null;
}

export interface SearchResponse {
  /** 服务端渲染的 ContextPack Markdown（D-21）；web **不得**重新拼装。 */
  markdown: string;
  meta: PackMeta;
}

export interface EvidenceSummaryItem {
  id: string;
  type: string;
  path: string;
  lines: number[] | null;
  tier: number | null;
  score: number | null;
}

export interface AskResponse {
  status: "answered" | "insufficient_evidence" | "degraded";
  answer: string;
  evidenceSummary: EvidenceSummaryItem[];
  meta: PackMeta;
}

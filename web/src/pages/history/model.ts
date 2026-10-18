/**
 * 历史记录的数据模型：把「索引记录 + 检索审计」归一成同一种行（初始化 / search / ask），
 * 并把后端的阶段耗时整理成用户能读懂的瀑布。纯函数，不碰网络和 React。
 */

import type { EvidenceMetaItem, IndexProgressView, IndexRun, UsageRecord } from "../../api/client";

export type Kind = "init" | "search" | "ask";
export type RowState = "ok" | "insufficient" | "failed" | "degraded" | "running";

export const KIND_LABEL: Record<Kind, string> = {
  init: "仓库初始化",
  search: "search",
  ask: "ask",
};

/** 一次初始化可能分多批上传（客户端按 1MB 分批，每批一个 run）。 */
export type InitBatch = {
  runId: number;
  at: number;
  durationMs: number;
  files: number;
  totalFiles: number;
  chunks: number;
  failed: boolean;
  error: string | null;
};

export type StageRow = { name: string; label: string; wallMs: number; cpuMs: number };

export type StageBreakdown = {
  /** 互不重叠的顶层阶段（按流水线顺序）。 */
  top: StageRow[];
  /** 「解析并写库」内部的细分（嵌套阶段，不与顶层相加）。 */
  writeDetail: StageRow[];
  /** 所有阶段的原始值（含嵌套 / 并行），给想对账的人看。 */
  raw: StageRow[];
  /** 顶层阶段墙钟之和。 */
  topTotalMs: number;
};

export type ActivityRow = {
  kind: Kind;
  key: string;
  at: number;
  project: string;
  /** 列表里的一句话摘要：检索 = 问题原文；初始化 = 文件与 chunks。 */
  summary: string;
  tool: string;
  state: RowState;
  durationMs: number;
  /** 初始化 = chunks，检索 = 用到的 token。 */
  volume: string;
  traceId: string | null;
  input: string;
  output: string;
  metrics: { label: string; value: string }[];
  note?: string;
  /** 仅初始化有。 */
  batches?: InitBatch[];
  stages?: StageBreakdown;
  /** 初始化的耗时是否只有整秒精度（旧记录）。 */
  coarseDuration?: boolean;
};

// ---------------------------------------------------------------- 阶段

/** 顶层阶段：在 ingest 主流程里串行出现，墙钟可相加。 */
const TOP_STAGES: [string, string][] = [
  ["engine_open", "打开项目"],
  ["delete", "清理已删除文件"],
  ["collect", "收集待索引文件"],
  ["write_batch", "解析并写入索引"],
  ["graph", "建立调用关系图"],
  ["vector_drain", "等待向量写入完成"],
  ["vector_delete", "清理旧向量"],
  ["finalize", "收尾"],
  ["engine_ready", "就绪检查"],
];

/** `write_batch` 内部的嵌套阶段。 */
const WRITE_STAGES: [string, string][] = [
  ["prefetch_take", "取预解析结果"],
  ["prepare", "解析与切块"],
  ["persist", "写入存储"],
  ["submit", "提交向量化"],
  ["rebuild_vectors", "重建向量"],
  ["flush_pending", "刷新向量队列"],
];

export function buildStages(runs: IndexRun[]): StageBreakdown | undefined {
  const sum = new Map<string, { wallMs: number; cpuMs: number }>();
  const order: string[] = [];
  for (const run of runs) {
    for (const stage of run.stages ?? []) {
      const acc = sum.get(stage.name);
      if (acc === undefined) {
        sum.set(stage.name, { wallMs: stage.wallMs, cpuMs: stage.cpuMs });
        order.push(stage.name);
      } else {
        acc.wallMs += stage.wallMs;
        acc.cpuMs += stage.cpuMs;
      }
    }
  }
  if (order.length === 0) return undefined;

  const pick = (table: [string, string][]): StageRow[] =>
    table.flatMap(([name, label]) => {
      const value = sum.get(name);
      return value === undefined ? [] : [{ name, label, ...value }];
    });
  const top = pick(TOP_STAGES);
  const labelOf = new Map([...TOP_STAGES, ...WRITE_STAGES]);
  return {
    top,
    writeDetail: pick(WRITE_STAGES),
    raw: order.map((name) => ({ name, label: labelOf.get(name) ?? name, ...sum.get(name)! })),
    topTotalMs: top.reduce((total, row) => total + row.wallMs, 0),
  };
}

// ---------------------------------------------------------------- 行

type Namer = (projectId: string) => string;

export function buildRows(
  runs: { projectId: string; run: IndexRun }[],
  records: UsageRecord[],
  since: number,
  nameOf: Namer,
  active: { projectId: string; progress: IndexProgressView | null }[] = [],
  now: number = Math.floor(Date.now() / 1000),
): ActivityRow[] {
  return [
    ...buildRunningRows(active, now, nameOf),
    ...buildInitRows(runs, since, nameOf),
    ...buildQueryRows(records, since, nameOf),
  ].sort((left, right) => right.at - left.at);
}

/**
 * 正在进行的初始化。``index_runs`` 只在索引结束后才写入，进行中的那次只能从项目的
 * ``indexProgress``（state=running）看到；不补这一行，用户在冷启动的一两分钟里会以为什么都没发生。
 */
function buildRunningRows(
  active: { projectId: string; progress: IndexProgressView | null }[],
  now: number,
  nameOf: Namer,
): ActivityRow[] {
  return active.flatMap(({ projectId, progress }) => {
    if (progress === null || progress.state !== "running" || progress.startedAt === null) return [];
    const files = progress.totalFiles
      ? `${progress.processedFiles}/${progress.totalFiles} 文件`
      : "收集文件中";
    return [
      {
        kind: "init" as const,
        key: `running-${projectId}-${progress.startedAt}`,
        at: progress.startedAt,
        project: nameOf(projectId),
        summary: `正在初始化 · ${files}`,
        tool: "—",
        state: "running" as const,
        durationMs: Math.max(0, now - progress.startedAt) * 1000,
        volume: "—",
        traceId: null,
        input: "",
        output: "",
        metrics: [{ label: "进度", value: files }],
        note: "索引进行中，完成后这里会换成完整的初始化记录（分阶段耗时等）。",
        coarseDuration: true,
      },
    ];
  });
}

/** 同一 callId 的多批上传折叠成一次初始化；没有 callId 的各自独立，不假装属于同一次。 */
function buildInitRows(
  runs: { projectId: string; run: IndexRun }[],
  since: number,
  nameOf: Namer,
): ActivityRow[] {
  const groups = new Map<string, { projectId: string; runs: IndexRun[] }>();
  for (const { projectId, run } of runs) {
    if (run.finishedAt < since) continue;
    const key = run.callId ?? `standalone-${projectId}-${run.runId}`;
    const group = groups.get(key);
    if (group === undefined) groups.set(key, { projectId, runs: [run] });
    else group.runs.push(run);
  }

  return [...groups.values()].map(({ projectId, runs: group }) => {
    const sorted = [...group].sort((a, b) => a.finishedAt - b.finishedAt || a.runId - b.runId);
    const first = sorted[0]!;
    const last = sorted[sorted.length - 1]!;
    const runMs = (run: IndexRun) => run.elapsedMs ?? run.durationMs;
    const batches: InitBatch[] = sorted.map((run) => ({
      runId: run.runId,
      at: run.finishedAt,
      durationMs: runMs(run),
      files: run.filesProcessed,
      totalFiles: run.filesTotal,
      chunks: run.chunks,
      failed: run.state !== "done",
      error: run.error,
    }));
    const files = batches.reduce((total, item) => total + item.files, 0);
    const totalFiles = batches.reduce((total, item) => total + item.totalFiles, 0);
    const errors = sorted.reduce((total, run) => total + run.errors, 0);
    const failed = batches.some((item) => item.failed);
    const errorText = sorted.map((run) => run.error).filter(Boolean).join("；");
    const name = nameOf(projectId);
    const callId = first.callId ?? null;

    return {
      kind: "init" as const,
      key: `init-${projectId}-${first.runId}`,
      at: last.finishedAt,
      project: name,
      summary: `${files} / ${totalFiles} 个文件 · ${last.chunks} chunks`,
      tool: sorted.length > 1 ? `仓库初始化（${sorted.length} 批）` : "仓库初始化",
      state: failed ? ("failed" as const) : ("ok" as const),
      durationMs: batches.reduce((total, item) => total + item.durationMs, 0),
      volume: `${last.chunks} chunks`,
      traceId: null,
      input: [`项目：${name}`, `projectId：${projectId}`, `上传批次：${sorted.length} 批`].join("\n"),
      output: [
        `解析文件：${files} / ${totalFiles}`,
        `chunks：${last.chunks}`,
        `解析问题：${errors > 0 ? `${errors} 个` : "无"}`,
      ].join("\n"),
      metrics: [
        { label: "解析文件", value: `${files} / ${totalFiles}` },
        { label: "chunks", value: String(last.chunks) },
        ...(callId ? [{ label: "callId", value: callId }] : []),
      ],
      ...(errorText ? { note: errorText } : {}),
      batches,
      ...(buildStages(sorted) ? { stages: buildStages(sorted)! } : {}),
      coarseDuration: sorted.some((run) => run.elapsedMs == null),
    };
  });
}

function buildQueryRows(records: UsageRecord[], since: number, nameOf: Namer): ActivityRow[] {
  return records
    .filter((record) => record.createdAt >= since)
    .map((record) => {
      const isAsk = Boolean(record.answerText || record.answerStatus);
      return {
        kind: isAsk ? ("ask" as const) : ("search" as const),
        key: `query-${record.queryId}`,
        at: record.createdAt,
        project: nameOf(record.projectId),
        summary: record.query,
        tool: isAsk ? "ask_project" : "search_context",
        state: queryState(record),
        durationMs: record.latencyMs,
        volume: `${record.usedTokens} token`,
        traceId: record.requestId,
        input: record.query,
        output: record.answerText
          ? `${record.answerText}\n\n── 证据 ──\n${evidenceLines(record)}`
          : evidenceLines(record),
        metrics: [
          ...(record.confidence ? [{ label: "信心", value: record.confidence }] : []),
          { label: "Token", value: String(record.usedTokens) },
          { label: "证据（代码/文档/测试）", value: evidenceBreakdown(record) },
          ...(record.answerStatus ? [{ label: "答案状态", value: record.answerStatus }] : []),
        ],
        ...noteFor(record),
      };
    });
}

function noteFor(record: UsageRecord): { note?: string } {
  if (record.answerText) return {};
  if (record.answerStatus === "insufficient_evidence") {
    return { note: "证据不足：未调用 LLM，没有答案正文。" };
  }
  if (record.answerStatus === "degraded") {
    return { note: "LLM 调用失败（超时 / 不可达 / 返回格式不对），没有答案正文。" };
  }
  return {};
}

function queryState(record: UsageRecord): RowState {
  if (record.answerable === null) return record.degraded ? "degraded" : "insufficient";
  return record.answerable ? "ok" : "insufficient";
}

// ---------------------------------------------------------------- 汇总

export type KindSummary = { count: number; failed: number; avgMs: number | null; maxMs: number | null };

export function summarize(rows: ActivityRow[], kind: Kind): KindSummary {
  // 进行中的那次耗时还在涨，不计入统计。
  const list = rows.filter((row) => row.kind === kind && row.state !== "running");
  if (list.length === 0) return { count: 0, failed: 0, avgMs: null, maxMs: null };
  const total = list.reduce((sum, row) => sum + row.durationMs, 0);
  return {
    count: list.length,
    failed: list.filter((row) => row.state === "failed" || row.state === "degraded").length,
    avgMs: Math.round(total / list.length),
    maxMs: Math.max(...list.map((row) => row.durationMs)),
  };
}

// ---------------------------------------------------------------- 证据文本

/** 证据数量按「代码/文档/测试」拆分；旧记录没有 group，只给总数，不编造拆分。 */
function evidenceBreakdown(record: UsageRecord): string {
  const items = record.evidence ?? [];
  if (items.length === 0) return "0/0/0";
  if (!items.some((item) => item.group)) return `${record.evidenceCount}/${record.docsCount}/—`;
  const docs = items.filter((item) => item.group === "Docs").length;
  const tests = items.filter((item) => item.group === "Tests").length;
  return `${items.length - docs - tests}/${docs}/${tests}`;
}

/** 证据清单：与 Agent 实际收到的 Markdown 同构（不含源码正文）。 */
function evidenceLines(record: UsageRecord): string {
  const items = record.evidence ?? [];
  if (items.length === 0) return "（无证据）";
  const groups: [string, string][] = [
    ["Core", "#### Core"],
    ["Related", "#### Related"],
    ["Tests", "#### Tests"],
  ];
  const out: string[] = ["## Relevant Context"];
  const codeItems = items.filter((item) => item.group !== "Docs");
  const docItems = items.filter((item) => item.group === "Docs");
  if (codeItems.length > 0) {
    out.push("### Code");
    for (const [group, heading] of groups) {
      const bucket = codeItems.filter((item) => (item.group ?? "Related") === group);
      if (bucket.length === 0) continue;
      out.push(heading, ...bucket.map(evidenceEntry));
    }
  }
  if (docItems.length > 0) out.push("### Docs", ...docItems.map(evidenceEntry));
  return out.join("\n");
}

function evidenceEntry(item: EvidenceMetaItem): string {
  const id = item.id ?? "?";
  const span =
    Array.isArray(item.lines) && item.lines.length >= 2 ? `:${item.lines[0]}-${item.lines[1]}` : "";
  const header = item.symbol
    ? `[${id}] ${item.symbol} — ${item.path ?? ""}${span}`
    : `[${id}] ${item.path ?? ""}${span}`;
  return `${header}${item.reason ? `\n     reason: ${item.reason}` : ""}`;
}

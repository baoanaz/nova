import { describe, expect, it } from "vitest";

import { buildRows, summarize } from "./model";

describe("buildRows：进行中的初始化", () => {
  const progress = {
    state: "running",
    startedAt: 1000,
    finishedAt: null,
    processedFiles: 120,
    totalFiles: 3000,
    error: null,
  };

  it("indexProgress=running 时补一行“进行中”，耗时随当前时间增长", () => {
    const rows = buildRows([], [], 0, (id) => id, [{ projectId: "p1", progress }], 1030);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: "init", state: "running", durationMs: 30_000 });
    expect(rows[0]!.summary).toContain("120/3000");
    // 进行中的不计入统计。
    expect(summarize(rows, "init").count).toBe(0);
  });

  it("已完成或空闲的项目不补行", () => {
    const done = { ...progress, state: "done" };
    expect(buildRows([], [], 0, (id) => id, [{ projectId: "p1", progress: done }], 1030)).toEqual([]);
    expect(buildRows([], [], 0, (id) => id, [{ projectId: "p1", progress: null }], 1030)).toEqual([]);
  });
});

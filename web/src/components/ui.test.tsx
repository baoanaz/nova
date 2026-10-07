/**
 * 通用展示组件（TASK-083）：`EmptyState` 与 `ErrorBlock`。
 *
 * 守两件事（本卡的核心价值）：
 * 1. `EmptyState` 的 title / hint / action 三个分支都能渲染——**hint 是"怎样才会有数据"**
 *    的载体，缺了就退化成无信息量的"暂无数据"；
 * 2. `ErrorBlock` 对任何抛出物都给得出**可读原因**，永远不显示 `[object Object]`
 *    ——错误可读是"不把后端故障伪装成空态"的最后一道防线。
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { EmptyState, ErrorBlock, Switch } from "./ui";

describe("EmptyState", () => {
  it("只给 title 时渲染标题，不渲染 hint / action", () => {
    render(<EmptyState title="还没有数据" />);

    expect(screen.getByText("还没有数据")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("渲染 hint（说明怎样才会有数据）", () => {
    render(
      <EmptyState
        title="还没有索引记录"
        hint="接入 Agent 后让它同步一次就会产生记录。"
      />,
    );

    expect(screen.getByText("还没有索引记录")).toBeInTheDocument();
    expect(screen.getByText("接入 Agent 后让它同步一次就会产生记录。")).toBeInTheDocument();
  });

  it("渲染 action（ReactNode，如去接入指南的链接）", () => {
    render(
      <EmptyState
        title="还没有项目"
        hint="接入 Agent 并同步一个仓库后项目会出现。"
        action={<a href="/connect">去接入指南</a>}
      />,
    );

    const link = screen.getByRole("link", { name: "去接入指南" });
    expect(link).toHaveAttribute("href", "/connect");
  });
});

describe("ErrorBlock", () => {
  it("显示 ApiError 的 message 与 code，并给出可操作指引", () => {
    render(
      <ErrorBlock
        error={new ApiError("index_failed", "上次索引失败", 500)}
      />,
    );

    expect(screen.getByText("上次索引失败")).toBeInTheDocument();
    expect(screen.getByText("index_failed")).toBeInTheDocument();
    // 指引由 code 推出，页面不各写文案。
    expect(screen.getByText(/上次索引失败：请到项目页查看/)).toBeInTheDocument();
  });

  it("服务不可达时提示「先启动服务」而不是空态", () => {
    render(
      <ErrorBlock
        error={new ApiError("network_error", "连不上 nova-service", 0)}
      />,
    );

    expect(screen.getByText("连不上 nova-service")).toBeInTheDocument();
    // message 与 hint 相同时不重复渲染两遍。
    expect(screen.getAllByText(/先启动服务/)).toHaveLength(1);
  });

  it("非 Error 的抛出物也不会渲染成 [object Object]", () => {
    const { container } = render(<ErrorBlock error={{ detail: "boom" }} />);

    expect(container.textContent).toContain('{"detail":"boom"}');
    expect(container.textContent).not.toContain("[object Object]");
  });

  it("完全没有可读信息的抛出物给兜底文案，而不是 [object Object]", () => {
    const { container } = render(<ErrorBlock error={null} />);

    expect(container.textContent).toContain("请求失败（错误对象没有可读的 message）");
    expect(container.textContent).not.toContain("[object Object]");
  });
});

describe("Switch（TASK-099 前端收尾：滑动开关）", () => {
  it("是 role=switch 的可访问控件，aria-checked 反映状态", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [on, setOn] = React.useState(false);
      return <Switch checked={on} onChange={setOn} label="自动刷新" testId="sw" />;
    }
    render(<Harness />);

    const sw = screen.getByRole("switch", { name: "自动刷新" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await user.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "true");
  });

  it("disabled 时不可切换（刷新中不给重复触发）", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="自动刷新" disabled />);

    const sw = screen.getByRole("switch", { name: "自动刷新" });
    expect(sw).toBeDisabled();
    await user.click(sw);
    expect(onChange).not.toHaveBeenCalled();
  });
});

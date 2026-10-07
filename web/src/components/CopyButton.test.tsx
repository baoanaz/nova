import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { CopyButton } from "./ui";

const originalExecCommand = Object.getOwnPropertyDescriptor(document, "execCommand");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  if (originalExecCommand) Object.defineProperty(document, "execCommand", originalExecCommand);
  else delete (document as Partial<Document>).execCommand;
});

function legacyClipboard(success: boolean) {
  const copy = vi.fn(() => {
    expect(document.activeElement).toBeInstanceOf(HTMLTextAreaElement);
    expect((document.activeElement as HTMLTextAreaElement).value).toBe("示例提示词");
    return success;
  });
  Object.defineProperty(document, "execCommand", { configurable: true, value: copy });
  return copy;
}

describe("HTTP 下的复制按钮", () => {
  it("Clipboard API 不可用时复制正确文本，随后清理临时输入框并恢复焦点", async () => {
    vi.stubGlobal("navigator", { clipboard: undefined });
    const copy = legacyClipboard(true);
    render(<CopyButton text="示例提示词" label="复制提示词" />);
    const button = screen.getByRole("button", { name: "复制提示词" });
    button.focus();
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
    expect(copy).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("Clipboard API 拒绝时仍尝试兼容复制", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    const copy = legacyClipboard(true);
    render(<CopyButton text="示例提示词" />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
    expect(copy).toHaveBeenCalledOnce();
  });

  it("复制失败时明确提示手动复制，不显示成功", async () => {
    vi.stubGlobal("navigator", { clipboard: undefined });
    legacyClipboard(false);
    render(<CopyButton text="示例提示词" />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(await screen.findByRole("button", { name: "请手动复制" })).toBeInTheDocument();
    expect(screen.queryByText("已复制")).not.toBeInTheDocument();
  });
});

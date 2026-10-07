import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConnectPage } from "./ConnectPage";
import { AGENT_TARGETS, BASE_URL_PLACEHOLDER, TOKEN_PLACEHOLDER } from "../app/connect-info";

// 默认地址与编辑后的地址必须真正进入可复制片段，而不只是输入框显示正确。
describe("接入指南服务地址", () => {
  it("预填当前站点地址，编辑后同步到三种 Agent，清空后仍保留地址与 Key 占位符", () => {
    const { container } = render(<ConnectPage />);
    const address = screen.getByRole("textbox", { name: /服务地址/ });
    expect(address).toHaveValue(window.location.origin);
    const snippet = () => container.querySelectorAll("pre")[1]!.textContent;
    expect(snippet()).toContain(window.location.origin);

    fireEvent.change(address, { target: { value: "https://context.example.com" } });
    for (const target of AGENT_TARGETS) {
      fireEvent.click(screen.getByRole("button", { name: target.label }));
      expect(snippet()).toContain("https://context.example.com");
      expect(snippet()).toContain(TOKEN_PLACEHOLDER);
    }
    fireEvent.change(address, { target: { value: "" } });
    expect(snippet()).toContain(BASE_URL_PLACEHOLDER);
  });
});

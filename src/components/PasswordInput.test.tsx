import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PasswordInput } from "@/components/ui";

function Harness(props: Partial<Parameters<typeof PasswordInput>[0]>) {
  const [value, setValue] = useState("");
  return <PasswordInput value={value} onChange={setValue} aria-label="Password" {...props} />;
}

describe("PasswordInput", () => {
  it("is masked by default", () => {
    render(<Harness />);
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("toggles between password and text when the eye button is clicked", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("Password");
    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(input).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(input).toHaveAttribute("type", "password");
  });

  it("is keyboard accessible: reachable by Tab and toggled with Enter and Space", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("Password");
    await user.click(input);
    await user.tab();
    const toggle = screen.getByRole("button", { name: "Show password" });
    expect(toggle).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(input).toHaveAttribute("type", "text");
    await user.keyboard(" ");
    expect(input).toHaveAttribute("type", "password");
  });

  it("exposes state via aria-label and aria-pressed", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const toggle = screen.getByRole("button", { name: "Show password" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
  });

  it("does not alter the typed value or submit the form when toggling", async () => {
    const user = userEvent.setup();
    let submitted = false;
    render(
      <form onSubmit={(e) => { e.preventDefault(); submitted = true; }}>
        <Harness />
      </form>,
    );
    const input = screen.getByLabelText("Password");
    await user.type(input, "S3cret!pw");
    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(input).toHaveValue("S3cret!pw");
    expect(submitted).toBe(false);
  });
});

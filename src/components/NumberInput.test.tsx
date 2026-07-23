import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { NumberInput } from "@/components/ui";

function Harness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return <NumberInput value={value} onChange={setValue} aria-label="Amount" />;
}

describe("NumberInput (non-negative)", () => {
  it("renders with min=0 so the spinner cannot decrement below zero", () => {
    render(<Harness />);
    expect(screen.getByLabelText("Amount")).toHaveAttribute("min", "0");
  });

  it("blocks typing a minus sign", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("Amount");
    await user.type(input, "-5");
    expect(input).toHaveValue(5);
  });

  it("clamps pasted negative values to zero-or-above", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("Amount");
    await user.click(input);
    await user.paste("-7");
    expect(input).toHaveValue(7);
  });

  it("clamps a below-minimum value on blur", async () => {
    const user = userEvent.setup();
    render(<Harness initial="-4" />);
    const input = screen.getByLabelText("Amount");
    await user.click(input);
    await user.tab();
    expect(input).toHaveValue(0);
  });

  it("accepts zero and positive values unchanged", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("Amount");
    await user.type(input, "0");
    expect(input).toHaveValue(0);
    await user.clear(input);
    await user.type(input, "42");
    expect(input).toHaveValue(42);
  });
});

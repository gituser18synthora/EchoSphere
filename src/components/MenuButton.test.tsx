import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MenuButton } from "@/components/ui";

const rect = (values: Partial<DOMRect>): DOMRect => ({
  x: 0, y: 0, top: 0, right: 0, bottom: 0, left: 0,
  width: 0, height: 0, toJSON: () => ({}), ...values,
});

describe("MenuButton", () => {
  afterEach(() => vi.restoreAllMocks());

  it("portals its menu outside overflow-clipping containers", async () => {
    const archive = vi.fn();
    const user = userEvent.setup();
    render(
      <div data-testid="clip-container" style={{ overflow: "hidden", height: 30 }}>
        <MenuButton actions={[{ label: "Archive", onClick: archive }]} />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "More actions" }));
    const menu = screen.getByRole("menu");
    expect(menu.parentElement).toBe(document.body);

    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    expect(archive).toHaveBeenCalledOnce();
  });

  it("opens upward when there is not enough room below the trigger", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("menu")
        ? rect({ width: 180, height: 150 })
        : rect({});
    });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1200 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });

    const user = userEvent.setup();
    render(<MenuButton actions={[{ label: "Archive", onClick: vi.fn() }]} />);
    const button = screen.getByRole("button", { name: "More actions" });
    vi.spyOn(button, "getBoundingClientRect").mockReturnValue(rect({
      top: 700, bottom: 732, left: 960, right: 1000, width: 40, height: 32,
    }));

    await user.click(button);
    await waitFor(() => expect(screen.getByRole("menu")).toHaveStyle({
      top: "546px",
      left: "820px",
    }));
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { AppProvider, useApp } from "./AppContext";
import { logout } from "@/services/api";
vi.mock("@/services/api", () => ({ logout: vi.fn() }));
function Control() {
  const { user, signOut, toasts } = useApp();
  return <><p>{user?.name ?? "Signed out"}</p><button onClick={() => { void signOut().catch(() => undefined); }}>Sign out</button>{toasts.map((t) => <p role="alert" key={t.id}>{t.message}</p>)}</>;
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("echosphere.session", JSON.stringify({ name: "Member", role: "tenant_user", email: "member@example.com" }));
  localStorage.setItem("echosphere.token", "current-token");
});
it("revokes the server session before removing the local account", async () => {
  let resolve!: (value: { signedOut: boolean }) => void;
  vi.mocked(logout).mockReturnValue(new Promise((r) => { resolve = r; }));
  const user = userEvent.setup(); render(<AppProvider><Control /></AppProvider>);
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  expect(logout).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem("echosphere.token")).toBe("current-token");
  resolve({ signedOut: true });
  await screen.findByText("Signed out");
  expect(localStorage.getItem("echosphere.token")).toBeNull();
});
it("shows the server error and retains the session when revocation fails", async () => {
  vi.mocked(logout).mockRejectedValue(new Error("Server unreachable"));
  const user = userEvent.setup(); render(<AppProvider><Control /></AppProvider>);
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not sign out on the server"));
  expect(localStorage.getItem("echosphere.token")).toBe("current-token");
});

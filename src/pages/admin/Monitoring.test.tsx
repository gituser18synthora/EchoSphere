import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Monitoring from "@/pages/admin/Monitoring";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  getPlatformHealth: vi.fn(),
  listAlerts: vi.fn(),
  simulateAction: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const getPlatformHealth = vi.mocked(api.getPlatformHealth);
const listAlerts = vi.mocked(api.listAlerts);

/** Exactly what the live probe endpoint returns for a half-up platform. */
const METRICS = [
  { name: "Platform API", status: "good", value: "Up", target: "127.0.0.1:9001",
    spark: [], group: "platform", detail: "Serving this request on /api/health" },
  { name: "Voice Worker", status: "good", value: "62 ms", target: "127.0.0.1:9002",
    spark: [], group: "ai", detail: "http://127.0.0.1:9002/health → 200" },
  { name: "MCP Server", status: "good", value: "52 ms", target: "127.0.0.1:9003",
    spark: [], group: "platform", detail: "http://127.0.0.1:9003/health → 200" },
  { name: "FreeSWITCH ESL", status: "critical", value: "Unreachable", target: "127.0.0.1:9004",
    spark: [], group: "telephony", detail: "127.0.0.1:9004 — ConnectionRefusedError" },
  { name: "Telephony gateway", status: "critical", value: "Unreachable", target: "127.0.0.1:9011",
    spark: [], group: "telephony", detail: "http://127.0.0.1:9011/health — ConnectError" },
] as unknown as Awaited<ReturnType<typeof api.getPlatformHealth>>;

const renderPage = async () => {
  getPlatformHealth.mockResolvedValue(METRICS);
  listAlerts.mockResolvedValue([]);
  render(<Monitoring />);
  await waitFor(() => expect(screen.getByText("Platform API")).toBeInTheDocument());
};

describe("Monitoring — Platform Health", () => {
  it("groups services by the tab id the backend sends, not by name", async () => {
    await renderPage();
    // Platform tab: only the services that declared group "platform".
    expect(screen.getByText("MCP Server")).toBeInTheDocument();
    expect(screen.queryByText("Voice Worker")).not.toBeInTheDocument();
    expect(screen.queryByText("FreeSWITCH ESL")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Telephony Health/ }));
    await waitFor(() => expect(screen.getByText("FreeSWITCH ESL")).toBeInTheDocument());
    expect(screen.getByText("Telephony gateway")).toBeInTheDocument();
    expect(screen.queryByText("MCP Server")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /AI Health/ }));
    await waitFor(() => expect(screen.getByText("Voice Worker")).toBeInTheDocument());
  });

  it("shows reachable services as healthy and dead ones as critical", async () => {
    await renderPage();
    const api9001 = screen.getByText("Platform API").closest(".card") as HTMLElement;
    expect(within(api9001).getByText("Healthy")).toBeInTheDocument();
    expect(within(api9001).getByText("Up")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /Telephony Health/ }));
    await waitFor(() => expect(screen.getByText("FreeSWITCH ESL")).toBeInTheDocument());
    const esl = screen.getByText("FreeSWITCH ESL").closest(".card") as HTMLElement;
    expect(within(esl).getByText("Critical")).toBeInTheDocument();
    expect(within(esl).getByText("Unreachable")).toBeInTheDocument();
  });

  it("shows the probed host:port so a wrong port is visible", async () => {
    await renderPage();
    expect(screen.getByText("127.0.0.1:9001")).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1:9003")).toBeInTheDocument();
  });

  it("reports why a probe failed", async () => {
    await renderPage();
    await userEvent.click(screen.getByRole("tab", { name: /Telephony Health/ }));
    await waitFor(() => expect(screen.getByText("FreeSWITCH ESL")).toBeInTheDocument());
    expect(screen.getByText(/ConnectionRefusedError/)).toBeInTheDocument();
  });

  it("renders no sparkline when the probe carries no history", async () => {
    getPlatformHealth.mockResolvedValue(METRICS);
    listAlerts.mockResolvedValue([]);
    const { container } = render(<Monitoring />);
    await waitFor(() => expect(screen.getByText("Platform API")).toBeInTheDocument());
    // Empty spark arrays must not draw a flat/NaN line.
    expect(container.querySelector(".sparkline")).toBeNull();
  });
});

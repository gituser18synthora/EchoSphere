import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Monitoring from "@/pages/admin/Monitoring";
import * as api from "@/services/api";
import type { PlatformAlert } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getPlatformHealth: vi.fn(),
  listAlertsPage: vi.fn(),
  updateAlert: vi.fn(),
  getAlert: vi.fn(),
}));
const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast, user: { id: "root", role: "super_admin" } }),
}));

const getPlatformHealth = vi.mocked(api.getPlatformHealth);
const listAlertsPage = vi.mocked(api.listAlertsPage);
const updateAlert = vi.mocked(api.updateAlert);
const alert = { id: "alert-a", severity: "critical", title: "Gateway unavailable", source: "Gateway", time: "2026-11-13T10:00:00Z", status: "open", scope: "telephony" } as const;
const page = (items: PlatformAlert[] = [], total = items.length) => ({ items, meta: { page: 1, pageSize: 25, total, totalPages: Math.max(1, Math.ceil(total / 25)), counts: { open: total, acknowledged: 0, resolved: 0, total } } });
beforeEach(() => {
  vi.resetAllMocks();
  getPlatformHealth.mockResolvedValue(METRICS);
  listAlertsPage.mockResolvedValue(page());
});
afterEach(() => { vi.useRealTimers(); });

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
  listAlertsPage.mockResolvedValue(page());
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
    listAlertsPage.mockResolvedValue(page());
    const { container } = render(<Monitoring />);
    await waitFor(() => expect(screen.getByText("Platform API")).toBeInTheDocument());
    // Empty spark arrays must not draw a flat/NaN line.
    expect(container.querySelector(".sparkline")).toBeNull();
  });
});


describe("Monitoring operations", () => {
  it("shows alert failures with retry instead of a healthy empty state", async () => {
    listAlertsPage.mockRejectedValue(new Error("Alerts service unavailable"));
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Alerts service unavailable");
    expect(screen.queryByText("No matching alerts")).not.toBeInTheDocument();
    expect(screen.queryByText("All systems nominal.")).not.toBeInTheDocument();
    listAlertsPage.mockResolvedValue(page([alert]));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(alert.title)).toBeInTheDocument();
  });

  it("shows health failures instead of claiming nothing is registered", async () => {
    getPlatformHealth.mockRejectedValue(new Error("Probe request timed out"));
    render(<Monitoring />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Probe request timed out");
    expect(screen.queryByText("No services in this group")).not.toBeInTheDocument();
    getPlatformHealth.mockResolvedValue(METRICS);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Platform API")).toBeInTheDocument();
  });

  it("persists acknowledgement through the API and reloads the saved status", async () => {
    const acknowledged = { ...alert, status: "acknowledged" };
    listAlertsPage.mockResolvedValueOnce(page([alert])).mockResolvedValue(page([acknowledged] as never));
    updateAlert.mockResolvedValue(acknowledged as never);
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(updateAlert).toHaveBeenCalledWith(alert.id, "acknowledged", { expectedStatus: "open" }));
    expect(await screen.findByText("acknowledged")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith("Alert acknowledged.");
    expect(toast).not.toHaveBeenCalledWith(expect.stringMatching(/on.call/i));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("acknowledged")).toBeInTheDocument();
  });

  it("retains an open alert and displays the API error when acknowledgement fails", async () => {
    listAlertsPage.mockResolvedValue(page([alert]));
    updateAlert.mockRejectedValue(new Error("Permission denied"));
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled();
    expect(toast).not.toHaveBeenCalledWith("Alert acknowledged.");
  });

  it("supports resolve and reopen with the current expected status", async () => {
    listAlertsPage.mockResolvedValue(page([alert]));
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await screen.findByText(alert.title);
    listAlertsPage.mockResolvedValue(page([{ ...alert, status: "resolved" }] as never));
    updateAlert.mockResolvedValue({ ...alert, status: "resolved" } as never);
    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(updateAlert).toHaveBeenCalledWith(alert.id, "resolved", { expectedStatus: "open" });
    await userEvent.click(await screen.findByRole("button", { name: "Reopen" }));
    expect(updateAlert).toHaveBeenCalledWith(alert.id, "open", { expectedStatus: "resolved" });
  });

  it("waits for both refresh requests before announcing success", async () => {
    await renderPage();
    let finishHealth!: (value: typeof METRICS) => void;
    getPlatformHealth.mockImplementationOnce(() => new Promise((resolve) => { finishHealth = resolve; }));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    expect(toast).not.toHaveBeenCalledWith("Monitoring refreshed.", "good");
    await act(async () => finishHealth(METRICS));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Monitoring refreshed.", "good"));
  });

  it("reports a partial refresh failure and identifies the saved health snapshot", async () => {
    await renderPage();
    getPlatformHealth.mockRejectedValueOnce(new Error("Health endpoint disconnected"));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Health endpoint disconnected");
    expect(screen.getByText("Platform API")).toBeInTheDocument();
    expect(screen.getByText(/Current service health is unconfirmed/)).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith("Monitoring refresh failed. Check the errors below.", "error");
    expect(toast).not.toHaveBeenCalledWith("Monitoring refreshed.", "good");
  });

  it("sends filters and pagination to the server, resetting the page on filter changes", async () => {
    const user = userEvent.setup();
    listAlertsPage.mockResolvedValue(page([alert], 60));
    render(<Monitoring />);
    await user.click(screen.getByRole("tab", { name: /Alerts/ }));
    await screen.findByText(alert.title);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(listAlertsPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "acknowledged");
    await waitFor(() => expect(listAlertsPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, status: "acknowledged" })));
    await user.selectOptions(screen.getByRole("combobox", { name: "Severity" }), "critical");
    await user.type(screen.getByRole("textbox", { name: "Search alerts" }), "Gateway");
    await waitFor(() => expect(listAlertsPage).toHaveBeenLastCalledWith(expect.objectContaining({ search: "Gateway", severity: "critical", status: "acknowledged", page: 1 })));
    await user.selectOptions(screen.getByRole("combobox", { name: "Rows per page" }), "50");
    await waitFor(() => expect(listAlertsPage).toHaveBeenLastCalledWith(expect.objectContaining({ pageSize: 50, page: 1 })));
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(listAlertsPage).toHaveBeenLastCalledWith({ page: 1, pageSize: 25 }));
  });

  it("shows real recorded history and labels availability as sampled checks", async () => {
    getPlatformHealth.mockResolvedValue([{ ...METRICS[0], uptimePercent: 50, knownSampleCount: 2, unknownSampleCount: 0, checkedAt: "2026-11-13T10:00:30Z", spark: [12, 30], history: [
      { time: "2026-11-13T10:00:00Z", status: "critical" }, { time: "2026-11-13T10:00:30Z", status: "good" },
    ] }] as never);
    render(<Monitoring />);
    expect(await screen.findByText(/2 samples · 50.0% of 2 rated checks healthy; 0 unrated/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Platform API recorded health history" })).toBeInTheDocument();
    expect(screen.getByLabelText("Recorded response time trend")).toBeInTheDocument();
  });

  it("shows the total observation count and a stale collector even after a fresh live probe", async () => {
    getPlatformHealth.mockResolvedValue([{ ...METRICS[0], checkedAt: new Date().toISOString(), sampleCount: 721, knownSampleCount: 700, unknownSampleCount: 21, uptimePercent: 99.2,
      collectedAt: new Date(Date.now() - 300_000).toISOString(), history: [{ time: new Date(Date.now() - 300_000).toISOString(), status: "good" }] }] as never);
    render(<Monitoring />);
    expect(await screen.findByText(/721 samples · 99.2% of 700 rated checks healthy; 21 unrated/)).toBeInTheDocument();
    expect(screen.getByText(/History collection is stale/)).toBeInTheDocument();
  });

  it("keeps unrated observations out of the healthy sample denominator", async () => {
    getPlatformHealth.mockResolvedValue([{ ...METRICS[0], sampleCount: 10, knownSampleCount: 1, unknownSampleCount: 9, uptimePercent: 100,
      history: [{ time: new Date().toISOString(), status: "good" }] }] as never);
    render(<Monitoring />);
    expect(await screen.findByText(/10 samples · 100.0% of 1 rated check healthy; 9 unrated/)).toBeInTheDocument();
  });

  it("does not let an older alert request replace the latest filtered page", async () => {
    let finishOld!: (value: ReturnType<typeof page>) => void;
    listAlertsPage.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; })).mockResolvedValue(page([{ ...alert, title: "Filtered incident" }]));
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Status" }), "resolved");
    expect(await screen.findByText("Filtered incident")).toBeInTheDocument();
    await act(async () => finishOld(page([alert])));
    expect(screen.queryByText(alert.title)).not.toBeInTheDocument();
    expect(screen.getByText("Filtered incident")).toBeInTheDocument();
  });

  it("reloads the server status after a concurrent alert change", async () => {
    listAlertsPage.mockResolvedValueOnce(page([alert])).mockResolvedValue(page([{ ...alert, status: "resolved" }] as never));
    updateAlert.mockRejectedValue(Object.assign(new Error("Alert changed. Reload before saving."), { status: 409 }));
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith("Alert changed. Reload before saving.", "error");
  });

  it("opens persisted alert history and saves an optional action note", async () => {
    listAlertsPage.mockResolvedValue(page([alert]));
    vi.mocked(api.getAlert).mockResolvedValue({ ...alert, history: [{ id: "audit-1", actor: "System monitor", action: "Alert opened", time: alert.time, previousValue: null, newValue: { status: "open" }, note: null }], historyCount: 1, historyTruncated: false } as never);
    updateAlert.mockResolvedValue({ ...alert, status: "acknowledged" } as never);
    render(<Monitoring />);
    await userEvent.click(screen.getByRole("tab", { name: /Alerts/ }));
    await userEvent.click(await screen.findByRole("button", { name: "View details" }));
    const dialog = await screen.findByRole("dialog", { name: "Alert details" });
    expect(await within(dialog).findByText("System monitor")).toBeInTheDocument();
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Action note" }), "Investigating gateway connectivity");
    await userEvent.click(within(dialog).getByRole("button", { name: "Acknowledge alert" }));
    await waitFor(() => expect(updateAlert).toHaveBeenCalledWith(alert.id, "acknowledged", { expectedStatus: "open", note: "Investigating gateway connectivity" }));
  });

  it("polls only while the browser page is visible and stops after unmount", async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const view = render(<Monitoring />);
    await act(async () => {});
    expect(getPlatformHealth).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(getPlatformHealth).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue("hidden");
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(getPlatformHealth).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(getPlatformHealth).toHaveBeenCalledTimes(3);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(getPlatformHealth).toHaveBeenCalledTimes(3);
    visibility.mockRestore();
  });
});

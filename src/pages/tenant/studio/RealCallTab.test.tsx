/* Real call view: number → confirm → Start Call → status → Hang up. The
   backend fills the dialer parameters and the bot's customer test data; the
   view sends only the number (and an optional ring timeout). */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RealCallTab, { normalizePhoneInput, phoneInputError } from "@/pages/tenant/studio/RealCallTab";
import * as api from "@/services/api";
import type { RealCallDefaults, RealCallJob, VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getRealCallDefaults: vi.fn(),
  startRealCall: vi.fn(),
  getRealCallStatus: vi.fn(),
  hangupRealCall: vi.fn(),
}));

const BOT = { id: "bot_450a4f7b4832", tenantId: "tn_fb39b67f5361", name: "Customer Support" } as VoiceBot;

const DEFAULTS: RealCallDefaults = {
  botId: BOT.id, botName: BOT.name, tenantId: BOT.tenantId, botStatus: "published",
  eslConfigured: true, eslHost: "127.0.0.1", callerIdNumber: "+918047133633",
  voiceChannel: { enabled: true, phoneNumber: "+918047133633", telephonyProvider: "freeswitch" },
  dialTemplate: "sofia/external/{number}@{domain_name}", application: "&lua(voicebot.lua)",
  systemParams: [], params: [], customerData: [],
  runtimeContext: { configured: true, sourceMode: "dialer", fields: [] },
  warnings: [], blockers: [],
};

const JOB: RealCallJob = {
  jobId: "11111111-2222-3333-4444-555555555555", botId: BOT.id, tenantId: BOT.tenantId,
  phoneMasked: "XXXXXXXX3210", originationUuid: "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b", leadId: "test-1",
  status: "dialing", detail: "Originate sent to FreeSWITCH; waiting for the phone to answer.",
  cause: null, channelUuid: null, variableNames: [], customerVariables: { vb_customer_name: "Rohan" },
  dialString: "sofia/external/+919876543210@dialer.test", waitSeconds: 90, startedAt: "2026-11-05T10:00:00Z",
};

const mocked = vi.mocked(api);

beforeEach(() => {
  mocked.getRealCallDefaults.mockReset();
  mocked.startRealCall.mockReset();
  mocked.getRealCallStatus.mockReset();
  mocked.hangupRealCall.mockReset();
  mocked.getRealCallDefaults.mockResolvedValue(DEFAULTS);
  mocked.startRealCall.mockResolvedValue(JOB);
  mocked.getRealCallStatus.mockResolvedValue(JOB);
});

describe("helpers", () => {
  it("normalizes and validates the number like the backend", () => {
    expect(normalizePhoneInput("+91 (98765) 43-210")).toBe("+919876543210");
    expect(phoneInputError("")).toMatch(/Enter/);
    expect(phoneInputError("12345")).toMatch(/8 to 15/);
    expect(phoneInputError("+91 98765 43210")).toBeUndefined();
  });
});

describe("RealCallTab", () => {
  it("fetches readiness only once shown and never on mount while hidden", async () => {
    const { rerender } = render(<RealCallTab bot={BOT} active={false} />);
    expect(mocked.getRealCallDefaults).not.toHaveBeenCalled();
    rerender(<RealCallTab bot={BOT} active />);
    await waitFor(() => expect(mocked.getRealCallDefaults).toHaveBeenCalledWith(BOT.id));
    expect(await screen.findByText(/call from \+918047133633/)).toBeInTheDocument();
    expect(mocked.startRealCall).not.toHaveBeenCalled();
  });

  it("hides every infrastructure detail: no ESL parameters, no customer JSON, no identifiers", async () => {
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText(/call from/);
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("sip_route_uri");
    expect(text).not.toContain("vb_");
    expect(text).not.toContain(BOT.id);
    expect(text).not.toContain(BOT.tenantId);
    expect(text).not.toContain("127.0.0.1");
  });

  it("starts only with a valid number and the confirmation, sending just the number", async () => {
    const user = userEvent.setup();
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText(/call from/);
    const start = screen.getByTestId("real-call-start");
    expect(start).toBeDisabled();

    await user.type(screen.getByTestId("real-call-phone"), "12345");
    await user.click(screen.getByTestId("real-call-confirm"));
    expect(start).toBeDisabled();
    await user.tab();
    expect(screen.getByText(/8 to 15 digits/)).toBeInTheDocument();

    await user.clear(screen.getByTestId("real-call-phone"));
    await user.type(screen.getByTestId("real-call-phone"), "+91 98765 43210");
    expect(start).toBeEnabled();
    await user.click(start);
    await waitFor(() => expect(mocked.startRealCall).toHaveBeenCalledTimes(1));
    expect(mocked.startRealCall).toHaveBeenCalledWith(BOT.id, { phoneNumber: "+919876543210" });

    const status = await screen.findByTestId("real-call-status");
    expect(status).toHaveTextContent("Dialing");
    expect(status).toHaveTextContent("XXXXXXXX3210");
    expect(screen.getByTestId("real-call-hangup")).toBeInTheDocument();
    expect(screen.queryByTestId("real-call-start")).toBeNull();
  });

  it("passes only the ring timeout from Advanced settings, validated", async () => {
    const user = userEvent.setup();
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText(/call from/);
    await user.click(screen.getByTestId("real-call-advanced"));
    await user.type(screen.getByTestId("real-call-ring"), "5");
    await user.type(screen.getByTestId("real-call-phone"), "9876543210");
    await user.click(screen.getByTestId("real-call-confirm"));
    expect(screen.getByText(/between 10 and 120/)).toBeInTheDocument();
    expect(screen.getByTestId("real-call-start")).toBeDisabled();

    await user.clear(screen.getByTestId("real-call-ring"));
    await user.type(screen.getByTestId("real-call-ring"), "30");
    await user.click(screen.getByTestId("real-call-start"));
    await waitFor(() => expect(mocked.startRealCall).toHaveBeenCalledWith(BOT.id, {
      phoneNumber: "9876543210", params: { dialTimeout: "30" },
    }));
  });

  it("is blocked when the backend reports blockers", async () => {
    const user = userEvent.setup();
    mocked.getRealCallDefaults.mockResolvedValue({
      ...DEFAULTS, eslConfigured: false,
      blockers: ["FreeSWITCH event socket is not configured on this server."],
    });
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText("Calls cannot be placed right now");
    await user.type(screen.getByTestId("real-call-phone"), "9876543210");
    await user.click(screen.getByTestId("real-call-confirm"));
    expect(screen.getByTestId("real-call-start")).toBeDisabled();
    expect(mocked.startRealCall).not.toHaveBeenCalled();
  });

  it("surfaces a refused start without losing the form", async () => {
    const user = userEvent.setup();
    mocked.startRealCall.mockRejectedValue(new Error("The bot is not published."));
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText(/call from/);
    await user.type(screen.getByTestId("real-call-phone"), "9876543210");
    await user.click(screen.getByTestId("real-call-confirm"));
    await user.click(screen.getByTestId("real-call-start"));
    await screen.findByText("The bot is not published.");
    expect(screen.getByTestId("real-call-phone")).toHaveValue("9876543210");
    expect(screen.queryByTestId("real-call-status")).toBeNull();
  });

  it("tracks the call to connected and hangs it up from the view", async () => {
    const user = userEvent.setup();
    mocked.getRealCallStatus.mockResolvedValue({
      ...JOB, status: "answered", channelUuid: JOB.originationUuid,
      detail: "Call answered; the dialplan is handing it to the bot.",
      conversation: { id: "cv_1", sessionId: "vs_1", botId: BOT.id, status: "active", startedAt: null, durationSec: 12, disposition: null, language: "hi-IN" },
    });
    mocked.hangupRealCall.mockResolvedValue({ ...JOB, status: "hungup", detail: "Ended from the Studio." });
    render(<RealCallTab bot={BOT} active />);
    await screen.findByText(/call from/);
    await user.type(screen.getByTestId("real-call-phone"), "9876543210");
    await user.click(screen.getByTestId("real-call-confirm"));
    await user.click(screen.getByTestId("real-call-start"));
    await screen.findByTestId("real-call-status");
    await waitFor(() => expect(screen.getByTestId("real-call-status")).toHaveTextContent("Connected"), { timeout: 6000 });
    expect(screen.getByTestId("real-call-status")).toHaveTextContent("Conversation active · 12s");
    expect(mocked.getRealCallStatus).toHaveBeenCalledWith(BOT.id, JOB.jobId);

    await user.click(screen.getByTestId("real-call-hangup"));
    await waitFor(() => expect(mocked.hangupRealCall).toHaveBeenCalledWith(BOT.id, JOB.jobId));
    await waitFor(() => expect(screen.getByTestId("real-call-status")).toHaveTextContent("Ended"));
    expect(screen.getByTestId("real-call-start")).toBeInTheDocument();
  }, 10000);
});

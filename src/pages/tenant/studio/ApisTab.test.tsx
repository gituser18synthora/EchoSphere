import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ApisTab from "@/pages/tenant/studio/ApisTab";
import * as api from "@/services/api";
import type { RuntimeContextConfig, VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  createApi: vi.fn(),
  createContextRecord: vi.fn(),
  deleteApi: vi.fn(),
  deleteContextRecord: vi.fn(),
  duplicateApi: vi.fn(),
  getRuntimeContext: vi.fn(),
  listApis: vi.fn(),
  listContextRecords: vi.fn(),
  listIntents: vi.fn(),
  listWorkflows: vi.fn(),
  saveRuntimeContext: vi.fn(),
  testApiConnection: vi.fn(),
  updateApi: vi.fn(),
  updateContextRecord: vi.fn(),
  validateRuntimeContext: vi.fn(),
}));

vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const BOT = { id: "bot_80487d7ce2e9", name: "Bot Dev" } as VoiceBot;

const CONFIG: RuntimeContextConfig = {
  id: null,
  botId: BOT.id,
  name: "User details",
  sourceMode: "manual",
  apiConnectionId: null,
  responsePath: null,
  fields: [],
  allowAdditional: true,
  testPayload: null,
  missingValuePolicy: null,
  domainPolicy: "generic",
  status: "active",
  configured: false,
};

describe("ApisTab runtime-context test user form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listApis).mockResolvedValue([]);
    vi.mocked(api.listIntents).mockResolvedValue([]);
    vi.mocked(api.listWorkflows).mockResolvedValue([]);
    vi.mocked(api.getRuntimeContext).mockResolvedValue(CONFIG);
    vi.mocked(api.saveRuntimeContext).mockResolvedValue({ ...CONFIG, configured: true });
  });

  it("saves friendly test-user fields as the runtime test payload", async () => {
    const user = userEvent.setup();
    render(<ApisTab bot={BOT} />);

    await user.click(await screen.findByText("Runtime context / User details"));
    await user.type(screen.getByPlaceholderText("Rahul Sharma"), "Amit Kumar");
    await user.type(screen.getByPlaceholderText("+91 98765 43210"), "+91 99999 88888");
    await user.type(screen.getByPlaceholderText("rahul@example.com"), "amit@example.com");
    await user.type(screen.getByPlaceholderText("4500"), "7250");
    await user.click(screen.getByRole("button", { name: "Save test user details" }));

    await waitFor(() => expect(api.saveRuntimeContext).toHaveBeenCalledTimes(1));
    expect(api.saveRuntimeContext).toHaveBeenCalledWith(BOT.id, expect.objectContaining({
      sourceMode: "manual",
      apiConnectionId: null,
      testPayload: {
        customer_name: "Amit Kumar",
        mobile: "+91 99999 88888",
        email: "amit@example.com",
        overdue_amount: 7250,
      },
      fields: expect.arrayContaining([
        expect.objectContaining({ key: "customer_name", label: "Name", type: "string" }),
        expect.objectContaining({ key: "mobile", label: "Mobile", type: "string" }),
        expect.objectContaining({ key: "email", label: "Email", type: "string" }),
        expect.objectContaining({ key: "overdue_amount", label: "Due amount", type: "number" }),
      ]),
    }));
  });

  it("shows configured custom fields without requiring JSON editing", async () => {
    vi.mocked(api.getRuntimeContext).mockResolvedValue({
      ...CONFIG,
      fields: [{ key: "policy_number", label: "Policy number", type: "string" }],
      testPayload: { policy_number: "POL-1042" },
    });

    const user = userEvent.setup();
    render(<ApisTab bot={BOT} />);
    await user.click(await screen.findByText("Runtime context / User details"));

    expect(screen.getByDisplayValue("POL-1042")).toBeInTheDocument();
    expect(screen.getByText("Prompt variable: {policy_number}")).toBeInTheDocument();
  });
});

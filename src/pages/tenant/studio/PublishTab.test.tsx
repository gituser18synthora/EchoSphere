/* Publish tab: a bot with no in-flight release gets a "create release" form
   (previously a dead-end callout), approval sends the note, and a failing
   checklist disables Publish for tenant admins but offers super admins an
   audited override. */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PublishTab from "@/pages/tenant/studio/PublishTab";
import * as api from "@/services/api";
import type { Release, VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listReleases: vi.fn(),
  createRelease: vi.fn(),
  updateReleaseStage: vi.fn(),
}));

const session = { role: "tenant_admin" as string };
const toast = vi.fn();
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast, user: { id: "usr_1", role: session.role, tenantId: "tn_x" }, hasPermission: () => true }),
}));

const BOT = {
  id: "bot_x", name: "Kotak neo", version: "v0.1.0", status: "draft",
  readiness: [
    { id: "r1", label: "Knowledge sources indexed", done: true, studioTab: "knowledge" },
    { id: "r6", label: "Channel connected", done: false, studioTab: "channels" },
    { id: "r7", label: "Regression suite passing", done: false, studioTab: "testing" },
  ],
} as unknown as VoiceBot;

const release = (over: Partial<Release>): Release => ({
  id: "rel_1", botId: "bot_x", version: "v0.1.0", stage: "review", notes: "first cut",
  requestedBy: "Kotak Neo", checklist: [
    { id: "c1", label: "All regression tests passing", ok: false, detail: "No scenarios defined" },
    { id: "c2", label: "Prompts approved", ok: true },
  ], diff: [], ...over,
});

const mount = () => render(<MemoryRouter><PublishTab bot={BOT} /></MemoryRouter>);

describe("PublishTab — release creation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    session.role = "tenant_admin";
  });

  it("offers a create-release form when the bot has no releases", async () => {
    vi.mocked(api.listReleases).mockResolvedValue([]);
    mount();
    expect(await screen.findByText("Create the first release")).toBeInTheDocument();
    expect(screen.getByDisplayValue("v0.1.0")).toBeInTheDocument();
    // open readiness items are listed as deep links, not as a hard block
    expect(screen.getByText("Channel connected")).toBeInTheDocument();
    expect(screen.getByText("Regression suite passing")).toBeInTheDocument();
    expect(screen.queryByText("Release history")).not.toBeInTheDocument();
  });

  it("posts the release and reloads", async () => {
    vi.mocked(api.listReleases).mockResolvedValueOnce([]).mockResolvedValue([release({})]);
    vi.mocked(api.createRelease).mockResolvedValue(release({}));
    mount();
    const user = userEvent.setup();
    await screen.findByText("Create the first release");
    await user.type(screen.getByPlaceholderText(/Initial go-live/), "Go-live pilot");
    await user.click(screen.getByRole("button", { name: "Create release" }));
    await waitFor(() => expect(api.createRelease).toHaveBeenCalledWith("bot_x", { version: "v0.1.0", notes: "Go-live pilot" }));
    expect(await screen.findByText("Release v0.1.0")).toBeInTheDocument();
    expect(screen.queryByText("Create the first release")).not.toBeInTheDocument();
  });

  it("proposes the next patch version once the last release is published", async () => {
    vi.mocked(api.listReleases).mockResolvedValue([release({ stage: "published", publishedAt: "2026-09-03T05:00:00Z", approvedBy: "Admin" })]);
    mount();
    expect(await screen.findByText("Create the next release")).toBeInTheDocument();
    expect(screen.getByDisplayValue("v0.1.1")).toBeInTheDocument();
    expect(screen.getByText("Release history")).toBeInTheDocument();
  });
});

describe("PublishTab — approval and the publish gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    session.role = "tenant_admin";
    vi.mocked(api.updateReleaseStage).mockResolvedValue(release({ stage: "approved" }));
  });

  it("requires a note to approve and sends it with the stage change", async () => {
    vi.mocked(api.listReleases).mockResolvedValue([release({})]);
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Review & approve" }));
    const approve = screen.getByRole("button", { name: "Approve release" });
    expect(approve).toBeDisabled();
    await user.type(screen.getByPlaceholderText(/Verified wait-time/), "Checked in staging");
    await user.click(approve);
    await waitFor(() => expect(api.updateReleaseStage).toHaveBeenCalledWith("rel_1", "approved", { note: "Checked in staging" }));
  });

  it("disables Publish for a tenant admin while checklist items fail", async () => {
    vi.mocked(api.listReleases).mockResolvedValue([release({ stage: "approved", approvedBy: "Kotak Neo" })]);
    mount();
    const publish = await screen.findByRole("button", { name: "Publish now" });
    expect(publish).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Publish with override" })).not.toBeInTheDocument();
  });

  it("publishes directly when the checklist passes", async () => {
    vi.mocked(api.listReleases).mockResolvedValue([release({
      stage: "approved", checklist: [{ id: "c1", label: "All regression tests passing", ok: true }],
    })]);
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Publish now" }));
    await waitFor(() => expect(api.updateReleaseStage).toHaveBeenCalledWith("rel_1", "published", undefined));
  });

  it("lets a super admin publish past a failing checklist with a justification", async () => {
    session.role = "super_admin";
    vi.mocked(api.listReleases).mockResolvedValue([release({ stage: "approved", approvedBy: "Admin" })]);
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Publish with override" }));
    const confirm = screen.getByRole("button", { name: "Publish anyway" });
    expect(confirm).toBeDisabled();
    // the failing checks are repeated inside the override dialog
    expect(screen.getAllByText(/No scenarios defined/).length).toBeGreaterThan(1);
    await user.type(screen.getByPlaceholderText(/Imported tenant/), "Supervised pilot approved by customer");
    await user.click(confirm);
    await waitFor(() => expect(api.updateReleaseStage).toHaveBeenCalledWith(
      "rel_1", "published", { overrideReason: "Supervised pilot approved by customer" },
    ));
  });
});

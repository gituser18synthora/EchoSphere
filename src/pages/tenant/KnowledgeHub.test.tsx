import { MemoryRouter } from "react-router-dom";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSource } from "@/types/domain";
import KnowledgeHub from "@/pages/tenant/KnowledgeHub";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listKnowledge: vi.fn(),
  listBots: vi.fn(),
  listKnowledgeGaps: vi.fn(),
  resyncKnowledge: vi.fn(),
  searchTest: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const listKnowledge = vi.mocked(api.listKnowledge);
const listBots = vi.mocked(api.listBots);
const listKnowledgeGaps = vi.mocked(api.listKnowledgeGaps);

const source = (over: Partial<KnowledgeSource>): KnowledgeSource => ({
  id: "ks_1", botId: "bot-1", scope: "bot", type: "document", name: "Handbook",
  detail: "handbook.pdf", status: "indexed", chunks: 12, sizeKb: 400,
  lastSync: "2026-07-20T10:00:00Z", quality: 90, usage30d: 5,
  createdAt: "2026-07-01T10:00:00Z", updatedAt: "2026-07-20T10:00:00Z",
  ...over,
});

const renderHub = () => render(<MemoryRouter><KnowledgeHub /></MemoryRouter>);

describe("KnowledgeHub — search sources", () => {
  beforeEach(() => {
    vi.mocked(api.listKnowledge).mockReset();
    listBots.mockResolvedValue([]);
    listKnowledgeGaps.mockResolvedValue([]);
  });

  it("shows a loading state while sources load", () => {
    listKnowledge.mockReturnValue(new Promise(() => {}));
    renderHub();
    expect(document.querySelector(".skeleton, .card-skeleton, [class*=skeleton]")).toBeTruthy();
  });

  it("renders source cards with name, file, type, KB id, chunk count, status and dates", async () => {
    listKnowledge.mockResolvedValue([source({})]);
    renderHub();
    const card = (await screen.findByRole("button", { name: "Open Handbook" }));
    const c = within(card);
    expect(c.getByText("Handbook")).toBeInTheDocument();
    expect(c.getByText("handbook.pdf")).toBeInTheDocument();
    expect(c.getByText("document")).toBeInTheDocument();
    expect(c.getByText("ks_1")).toBeInTheDocument();
    expect(c.getByText(/12 chunks/)).toBeInTheDocument();
    expect(c.getByText("indexed")).toBeInTheDocument();
    expect(c.getByText(/Created Jul 1, 2026/)).toBeInTheDocument();
    expect(c.getByText(/synced Jul 20, 2026/)).toBeInTheDocument();
  });

  it("never renders duplicate source entries", async () => {
    listKnowledge.mockResolvedValue([source({}), source({})]);
    renderHub();
    await screen.findByRole("button", { name: "Open Handbook" });
    expect(screen.getAllByRole("button", { name: "Open Handbook" })).toHaveLength(1);
  });

  it("filters sources by the text search", async () => {
    listKnowledge.mockResolvedValue([
      source({}),
      source({ id: "ks_2", name: "Pricing FAQ", detail: "pricing.md", type: "faq" }),
    ]);
    const user = userEvent.setup();
    renderHub();
    await screen.findByRole("button", { name: "Open Handbook" });
    await user.type(screen.getByLabelText("Search knowledge sources"), "pricing");
    expect(screen.queryByRole("button", { name: "Open Handbook" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Pricing FAQ" })).toBeInTheDocument();
  });

  it("shows the empty state when no source matches the filters", async () => {
    listKnowledge.mockResolvedValue([source({})]);
    const user = userEvent.setup();
    renderHub();
    await screen.findByRole("button", { name: "Open Handbook" });
    await user.type(screen.getByLabelText("Search knowledge sources"), "does-not-exist");
    expect(screen.getByText("No sources match")).toBeInTheDocument();
  });

  it("shows an error state with retry when the API fails", async () => {
    listKnowledge.mockRejectedValue(new Error("Network unreachable"));
    renderHub();
    expect(await screen.findByText("Network unreachable")).toBeInTheDocument();
  });

  it("switches to the table view", async () => {
    listKnowledge.mockResolvedValue([source({})]);
    const user = userEvent.setup();
    renderHub();
    await screen.findByRole("button", { name: "Open Handbook" });
    await user.click(screen.getByRole("button", { name: "Table view" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

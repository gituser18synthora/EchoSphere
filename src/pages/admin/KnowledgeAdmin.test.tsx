import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import KnowledgeAdmin from "@/pages/admin/KnowledgeAdmin";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listKnowledgePaged: vi.fn(),
  listTenants: vi.fn(),
  getKnowledgeDetail: vi.fn(),
}));

const listKnowledgePaged = vi.mocked(api.listKnowledgePaged);
const getKnowledgeDetail = vi.mocked(api.getKnowledgeDetail);

const paged = (items: Record<string, unknown>[], total = items.length) => ({
  items, meta: { page: 1, pageSize: 25, total, totalPages: Math.ceil(total / 25) },
});

const KB_A = {
  id: "ks_a", name: "Meridian Handbook", detail: "handbook.pdf", type: "document",
  scope: "tenant", status: "indexed", chunks: 42, sizeKb: 300, lastSync: "—",
  quality: 90, usage30d: 12, tenantId: "tn-001",
};
const KB_B = {
  id: "ks_b", name: "Northwind FAQ", detail: "faq.md", type: "faq",
  scope: "tenant", status: "failed", chunks: 0, sizeKb: 10, lastSync: "—",
  quality: 0, usage30d: 0, tenantId: "tn-002",
};

const DETAIL = {
  id: "ks_a", name: "Meridian Handbook", description: "handbook.pdf", type: "document",
  scope: "tenant", status: "indexed", tenantId: "tn-001", tenantName: "Meridian Health Group",
  botId: null, botName: null, chunks: 42, sizeKb: 300, quality: 90, usage30d: 12,
  lastSync: null, createdAt: "2026-07-01T10:00:00Z", updatedAt: "2026-07-20T10:00:00Z",
  createdBy: "Priya Sharma",
  stats: {
    documentCount: 2, readyDocuments: 1, failedDocuments: 1, activeChunks: 42,
    embeddedChunks: 42, embeddingModels: ["mock-embedding"], lastError: "Parse failed on page 3",
  },
  documents: [{
    documentId: "kdoc_1", kbId: "ks_a", fileName: "handbook.pdf", status: "ready",
    stage: "", progress: 100, attempts: 1, failureReason: null, chunkCount: 42,
    pageCount: 10, queuedAt: null, startedAt: null, finishedAt: null,
  }],
};

describe("KnowledgeAdmin — filters and View", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listTenants).mockResolvedValue([
      { id: "tn-001", name: "Meridian Health Group", domain: "meridian.example" },
      { id: "tn-002", name: "Northwind Insurance", domain: "northwind.example" },
    ] as never);
    listKnowledgePaged.mockResolvedValue(paged([KB_A, KB_B]) as never);
    getKnowledgeDetail.mockResolvedValue(DETAIL as never);
  });

  it("renders the tenant filter and knowledge rows", async () => {
    render(<KnowledgeAdmin />);
    expect(await screen.findByText("Meridian Handbook")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by tenant" })).toBeInTheDocument();
  });

  it("selecting a tenant (via search) requests only that tenant's KBs and resets to page 1", async () => {
    const user = userEvent.setup();
    render(<KnowledgeAdmin />);
    await screen.findByText("Meridian Handbook");
    listKnowledgePaged.mockResolvedValue(paged([KB_A]) as never);

    await user.click(screen.getByRole("combobox", { name: "Filter by tenant" }));
    await user.type(screen.getByPlaceholderText("Search tenants…"), "meri");
    await user.click(await screen.findByRole("option", { name: /Meridian Health Group/ }));

    await waitFor(() => {
      const last = listKnowledgePaged.mock.calls.at(-1)?.[0];
      expect(last).toMatchObject({ tenantId: "tn-001", page: 1 });
    });
    expect(screen.getByText(/1 filter active/)).toBeInTheDocument();
  });

  it("clearing filters restores the unfiltered list", async () => {
    const user = userEvent.setup();
    render(<KnowledgeAdmin />);
    await screen.findByText("Meridian Handbook");
    await user.click(screen.getByRole("combobox", { name: "Filter by tenant" }));
    await user.click(await screen.findByRole("option", { name: /Northwind Insurance/ }));
    await screen.findByText(/1 filter active/);
    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    await waitFor(() => {
      const last = listKnowledgePaged.mock.calls.at(-1)?.[0];
      expect(last?.tenantId).toBeUndefined();
    });
    expect(screen.queryByText(/filter active/)).not.toBeInTheDocument();
  });

  it("status filter combines with search", async () => {
    const user = userEvent.setup();
    render(<KnowledgeAdmin />);
    await screen.findByText("Meridian Handbook");
    await user.selectOptions(screen.getByLabelText("Filter by status"), ["failed"]);
    await user.type(screen.getByLabelText("Search knowledge bases"), "faq");
    await waitFor(() => {
      const last = listKnowledgePaged.mock.calls.at(-1)?.[0];
      expect(last).toMatchObject({ status: "failed", search: "faq" });
    });
  });

  it("View opens the knowledge detail drawer with full information", async () => {
    const user = userEvent.setup();
    render(<KnowledgeAdmin />);
    await screen.findByText("Meridian Handbook");
    await user.click(screen.getAllByRole("button", { name: "View" })[0]);

    await waitFor(() => expect(getKnowledgeDetail).toHaveBeenCalledWith("ks_a"));
    const drawer = await screen.findByRole("dialog");
    const d = within(drawer);
    expect(await d.findByText("ks_a")).toBeInTheDocument();
    expect(d.getByText("Meridian Health Group")).toBeInTheDocument();
    expect(d.getByText("tn-001")).toBeInTheDocument();
    expect(d.getByText("2 documents")).toBeInTheDocument();
    expect(d.getByText("42 active chunks")).toBeInTheDocument();
    expect(d.getByText("Parse failed on page 3")).toBeInTheDocument();
    expect(d.getAllByText("handbook.pdf").length).toBeGreaterThan(0);
    expect(d.getAllByText("mock-embedding").length).toBeGreaterThan(0);
  });

  it("shows the filtered empty state when nothing matches", async () => {
    const user = userEvent.setup();
    render(<KnowledgeAdmin />);
    await screen.findByText("Meridian Handbook");
    listKnowledgePaged.mockResolvedValue(paged([]) as never);
    await user.type(screen.getByLabelText("Search knowledge bases"), "nothing-matches");
    expect(await screen.findByText(/no knowledge bases match the current filters/i)).toBeInTheDocument();
  });
});

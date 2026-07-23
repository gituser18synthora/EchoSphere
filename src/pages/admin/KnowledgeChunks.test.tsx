import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import KnowledgeChunks from "@/pages/admin/KnowledgeChunks";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  reviewFacets: vi.fn(),
  reviewDocuments: vi.fn(),
  getReviewDocument: vi.fn(),
  reviewChunks: vi.fn(),
  getReviewChunk: vi.fn(),
  setChunkStatus: vi.fn(),
  flagChunk: vi.fn(),
  retryReviewDocument: vi.fn(),
  reindexReviewDocument: vi.fn(),
  archiveReviewDocument: vi.fn(),
  downloadReviewDocument: vi.fn(),
  reviewRetrievalTest: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const paged = (items: unknown[], total = items.length) => ({
  items, meta: { page: 1, pageSize: 50, total, totalPages: 1 },
});

const WARNINGS = {
  flaggedForReview: false, promptInjection: false, emptyChunk: false, shortChunk: false,
  missingPage: false, missingSection: false, ocr: false, table: false, fromImage: false,
};

const DOC = {
  documentId: "kdoc_1", tenantId: "tn-001", tenantName: "Meridian Health Group",
  tenantCode: "meridian", kbId: "ks_a", kbName: "Handbook KB", fileName: "handbook.pdf",
  fileExt: "pdf", mimeType: "application/pdf", sizeBytes: 4096, status: "ready",
  uploadStatus: "ready", ingestionStatus: "completed", ingestionStage: null,
  ingestionProgress: 100, chunkCount: 2, pageCount: 3, embeddingModel: "mock-embedding",
  embeddingDimension: 1536, language: "en", failureReason: null, uploadedBy: "u1",
  uploadedByName: "Priya", uploadedAt: "2026-07-01T10:00:00Z",
  processingCompletedAt: "2026-07-01T10:05:00Z", isDeleted: false, hasOriginalFile: true,
};

const LONG_TEXT = "This is the complete chunk text. ".repeat(40).trim();

const CHUNK = {
  chunkId: "chk_1", documentId: "kdoc_1", kbId: "ks_a", kbName: "Handbook KB",
  tenantId: "tn-001", chunkIndex: 0, pageNumber: 1, section: "Intro", topic: null,
  chunkType: "text", language: "en", keywords: ["policy"], tokenCount: 250,
  charCount: LONG_TEXT.length, status: "active", contentPreview: LONG_TEXT.slice(0, 240),
  content: LONG_TEXT, hasMetadata: true, embeddingModel: "mock-embedding",
  embeddingDimension: 1536, embeddingGenerated: true,
  createdAt: "2026-07-01T10:03:00Z", updatedAt: "2026-07-01T10:03:00Z", warnings: WARNINGS,
};

const CHUNK_DETAIL = {
  ...CHUNK,
  metadata: { file_name: "handbook.pdf", chunk_strategy: { name: "layout", size: 2048 } },
  contentHash: "abc123",
  tenantName: "Meridian Health Group",
  fileName: "handbook.pdf",
  quality: {
    ...WARNINGS, tokenCount: 250, charCount: LONG_TEXT.length, overlapWithPrevChars: 0,
    duplicate: false, duplicateCount: 1, piiKinds: [], pii: false,
    promptInjectionPatterns: [], reviewFlag: null,
  },
  prev: null,
  current: { chunkId: "chk_1", chunkIndex: 0, pageNumber: 1, content: LONG_TEXT },
  next: { chunkId: "chk_2", chunkIndex: 1, pageNumber: 1, content: "Next chunk text" },
};

function installMocks() {
  vi.mocked(api.reviewFacets).mockResolvedValue({
    tenants: [{ id: "tn-001", name: "Meridian Health Group", code: "meridian" }],
    fileTypes: ["pdf"], uploadStatuses: ["ready", "failed"],
    ingestionStatuses: ["completed"], languages: ["en"],
  } as never);
  vi.mocked(api.reviewDocuments).mockResolvedValue(paged([DOC]) as never);
  vi.mocked(api.getReviewDocument).mockResolvedValue({
    ...DOC,
    quality: {
      activeChunks: 2, archivedChunks: 0, minTokens: 100, maxTokens: 300, avgTokens: 200,
      shortChunks: 0, chunksMissingPage: 0, chunksMissingSection: 0, ocrChunks: 0,
      tableChunks: 0, promptInjectionChunks: 0, flaggedChunks: 0,
    },
  } as never);
  vi.mocked(api.reviewChunks).mockResolvedValue(paged([CHUNK]) as never);
  vi.mocked(api.getReviewChunk).mockResolvedValue(CHUNK_DETAIL as never);
}

describe("KnowledgeChunks — layout, hidden Uploaded filter, chunk view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installMocks();
  });

  it("renders the aligned filter toolbar without the Uploaded date filter", async () => {
    render(<KnowledgeChunks />);
    await screen.findByText("handbook.pdf");
    // Labeled filter cells exist in the aligned grid…
    const grid = document.querySelector(".filter-grid");
    expect(grid).not.toBeNull();
    for (const label of ["Search", "Tenant", "File type", "Status", "Ingestion"]) {
      expect(within(grid as HTMLElement).getByText(label)).toBeInTheDocument();
    }
    // …but the Uploaded date-range filter is temporarily hidden.
    expect(screen.queryByText("Uploaded from")).not.toBeInTheDocument();
    expect(screen.queryByText("Uploaded to")).not.toBeInTheDocument();
    expect(document.querySelector('input[type="date"]')).toBeNull();
  });

  it("never sends the uploaded date-range filter to the API", async () => {
    render(<KnowledgeChunks />);
    await screen.findByText("handbook.pdf");
    const call = vi.mocked(api.reviewDocuments).mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(call.uploadedFrom).toBeUndefined();
    expect(call.uploadedTo).toBeUndefined();
  });

  it("remaining filters still work together", async () => {
    const user = userEvent.setup();
    render(<KnowledgeChunks />);
    await screen.findByText("handbook.pdf");
    await user.selectOptions(screen.getByLabelText("Filter by tenant"), ["tn-001"]);
    await user.selectOptions(screen.getByLabelText("Filter by status"), ["failed"]);
    await waitFor(() => {
      const call = vi.mocked(api.reviewDocuments).mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(call).toMatchObject({ tenantId: "tn-001", status: "failed", page: 1 });
    });
  });

  it("document detail drawer hides the Uploaded at date but keeps uploader", async () => {
    const user = userEvent.setup();
    render(<KnowledgeChunks />);
    await user.click(await screen.findByText("handbook.pdf"));
    const drawer = await screen.findByRole("dialog");
    const d = within(drawer);
    expect(await d.findByText("Uploaded by")).toBeInTheDocument();
    expect(d.queryByText("Uploaded at")).not.toBeInTheDocument();
  });

  it("chunk View shows the complete text, ownership, embedding info and metadata — without an uploaded date", async () => {
    const user = userEvent.setup();
    render(<KnowledgeChunks />);
    // documents → detail drawer → Review chunks → chunk row → chunk drawer
    await user.click(await screen.findByText("handbook.pdf"));
    await user.click(await screen.findByRole("button", { name: "Review chunks" }));
    await screen.findByText("Content preview");
    await user.click((await screen.findAllByText(/This is the complete chunk text/))[0]);

    await waitFor(() => expect(api.getReviewChunk).toHaveBeenCalledWith("chk_1"));
    const drawer = await screen.findByRole("dialog");
    const d = within(drawer);
    // Complete chunk text (current block shows the full content).
    expect(await d.findByText(LONG_TEXT)).toBeInTheDocument();
    // Ownership + embedding facts.
    expect(d.getByText("Meridian Health Group")).toBeInTheDocument();
    expect(d.getAllByText("Handbook KB").length).toBeGreaterThan(0);
    expect(d.getByText("kdoc_1")).toBeInTheDocument();
    expect(d.getByText("1536d")).toBeInTheDocument();
    expect(d.getByText(`${LONG_TEXT.length} chars`)).toBeInTheDocument();
    // Metadata rendered as a readable JSON tree.
    expect(d.getByText(/"handbook.pdf"/)).toBeInTheDocument();
    expect(d.getByText("chunk_strategy:")).toBeInTheDocument();
    // No uploaded date anywhere in the chunk view.
    expect(d.queryByText(/uploaded/i)).not.toBeInTheDocument();
  });

  it("shows empty state when no documents match", async () => {
    vi.mocked(api.reviewDocuments).mockResolvedValue(paged([]) as never);
    render(<KnowledgeChunks />);
    expect(await screen.findByText("No documents match")).toBeInTheDocument();
  });

  it("shows the error state when loading fails", async () => {
    vi.mocked(api.reviewDocuments).mockRejectedValue(new Error("Backend unavailable"));
    render(<KnowledgeChunks />);
    expect(await screen.findByText(/backend unavailable/i)).toBeInTheDocument();
  });
});

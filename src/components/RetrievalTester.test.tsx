import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchTestResult } from "@/types/domain";
import { RetrievalTester } from "@/components/RetrievalTester";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({ searchTest: vi.fn() }));
const searchTest = vi.mocked(api.searchTest);

const answerableResult: SearchTestResult = {
  usedKnowledgeBase: true,
  answerable: true,
  confidence: 0.78,
  query: "renewal grace period",
  kbIds: ["ks_1"],
  durationMs: 42.5,
  skippedReason: null,
  diagnostics: {
    kbCount: 1, queryLength: 20, embedder: "mock-embedding",
    fusionMethod: "weighted", semanticWeight: 0.65, bm25Weight: 0.35,
    minScore: 0.35, minKeywordRank: 0.02,
    denseCandidates: 3, keywordCandidates: 2, mergedCandidates: 4,
    afterGate: 1, reranked: 0, returned: 1,
    timingsMs: { embed: 0.4, dense: 11.2, keyword: 8.3, fuse: 0.1 },
    zeroResultReason: null,
  },
  sources: [{
    kbId: "ks_1", documentId: "kdoc_1", chunkId: "chk_1", chunkIndex: 0,
    pageNumber: 2, section: "Renewals", rank: 1, score: 0.778,
    vectorScore: 0.8357, keywordScore: 0.6258, rerankScore: null, passedGate: true,
    text: "The policy grace period for renewal is exactly 30 days.",
    documentName: "policy.pdf", meta: { language: "en" },
  }],
};

const emptyResult: SearchTestResult = {
  ...answerableResult,
  answerable: false,
  confidence: 0,
  skippedReason: "no_matching_chunks",
  diagnostics: {
    ...answerableResult.diagnostics!,
    denseCandidates: 0, keywordCandidates: 0, mergedCandidates: 0,
    afterGate: 0, returned: 0, zeroResultReason: "no_matching_chunks",
  },
  sources: [],
};

async function runQuery(user: ReturnType<typeof userEvent.setup>, text = "renewal grace period") {
  await user.type(screen.getByLabelText("Retrieval test query"), text);
  await user.click(screen.getByRole("button", { name: /run retrieval test/i }));
}

describe("RetrievalTester", () => {
  // Braces matter: returning the mock from beforeEach would register it as a
  // teardown hook, and vitest would then *call* searchTest() after each test.
  beforeEach(() => { searchTest.mockReset(); });

  it("renders chunk text, document, KB, rank and every pipeline score", async () => {
    searchTest.mockResolvedValue(answerableResult);
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_1"]} />);
    await runQuery(user);

    expect(await screen.findByText(/The policy grace period for renewal/)).toBeInTheDocument();
    expect(screen.getByText("policy.pdf")).toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("KB: ks_1")).toBeInTheDocument();
    expect(screen.getByText("final 0.778")).toBeInTheDocument();
    expect(screen.getByText("semantic 0.836")).toBeInTheDocument();
    expect(screen.getByText("BM25 0.626")).toBeInTheDocument();
    expect(screen.getByText("Answerable")).toBeInTheDocument();
    expect(searchTest).toHaveBeenCalledWith({ query: "renewal grace period", kbIds: ["ks_1"] });
  });

  it("shows chunk metadata behind a toggle", async () => {
    searchTest.mockResolvedValue(answerableResult);
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_1"]} />);
    await runQuery(user);
    await user.click(await screen.findByRole("button", { name: /metadata/i }));
    expect(screen.getByText(/language/)).toBeInTheDocument();
  });

  it("explains zero results with the diagnostic reason", async () => {
    searchTest.mockResolvedValue(emptyResult);
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_1"]} />);
    await runQuery(user, "quantum entanglement");
    expect(await screen.findByText("No chunks matched")).toBeInTheDocument();
    expect(screen.getByText(/neither semantic nor keyword search found any candidate chunk/i)).toBeInTheDocument();
  });

  it("shows pipeline diagnostics (candidate counts and weights)", async () => {
    searchTest.mockResolvedValue(answerableResult);
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_1"]} />);
    await runQuery(user);
    await user.click(await screen.findByRole("button", { name: /pipeline diagnostics/i }));
    expect(screen.getByText(/semantic 3 · keyword 2 · merged 4/)).toBeInTheDocument();
    expect(screen.getByText(/weighted \(semantic 0.65 \/ BM25 0.35\)/)).toBeInTheDocument();
  });

  it("surfaces API errors inline with a retry button", async () => {
    searchTest.mockRejectedValue(new Error("Knowledge base not found."));
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_bad"]} />);
    await runQuery(user);
    expect(await screen.findByText("Retrieval request failed")).toBeInTheDocument();
    expect(screen.getByText("Knowledge base not found.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("marks below-threshold near-misses when nothing is answerable", async () => {
    searchTest.mockResolvedValue({
      ...emptyResult,
      skippedReason: "below_confidence_threshold",
      diagnostics: { ...emptyResult.diagnostics!, mergedCandidates: 2, returned: 1, zeroResultReason: "below_confidence_threshold" },
      sources: [{ ...answerableResult.sources[0], passedGate: false, rank: 1, score: 0.11, vectorScore: 0.11, keywordScore: null }],
    });
    const user = userEvent.setup();
    render(<RetrievalTester kbIds={["ks_1"]} />);
    await runQuery(user);
    expect(await screen.findByText(/below the relevance thresholds/i)).toBeInTheDocument();
    expect(screen.getByText("below threshold")).toBeInTheDocument();
  });
});

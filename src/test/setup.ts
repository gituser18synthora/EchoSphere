import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom does not implement scrollIntoView (used by SearchableSelect/MultiSelect).
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
// …nor Element.scrollTo (used by transcript autoscroll in TestingTab).
Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});

// jsdom's Blob implements arrayBuffer() but not stream(); Node's Response
// requires stream() when tests pass a Blob body (download-service tests).
if (typeof Blob.prototype.stream !== "function") {
  Blob.prototype.stream = function stream(this: Blob) {
    return new ReadableStream<Uint8Array<ArrayBuffer>>({
      start: async (controller) => {
        controller.enqueue(new Uint8Array(await this.arrayBuffer()));
        controller.close();
      },
    });
  };
}

// jsdom does not implement ResizeObserver (used by the responsive SVG charts).
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class ResizeObserverMock implements ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

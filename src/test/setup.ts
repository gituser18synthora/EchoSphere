import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom does not implement scrollIntoView (used by SearchableSelect/MultiSelect).
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
// …nor Element.scrollTo (used by transcript autoscroll in TestingTab).
Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});

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

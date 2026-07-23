import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom does not implement scrollIntoView (used by SearchableSelect/MultiSelect).
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

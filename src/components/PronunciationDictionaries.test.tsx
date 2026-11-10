import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DictionaryField } from "@/components/PronunciationDictionaries";
import type { PronunciationDictionary } from "@/types/domain";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  createPronunciationDictionary: vi.fn(),
  deletePronunciationDictionary: vi.fn(),
  getModelLanguages: vi.fn(),
  getPronunciationDictionary: vi.fn(),
  listPronunciationDictionaries: vi.fn(),
  updatePronunciationDictionary: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({ useApp: () => ({ toast: vi.fn() }) }));

const BRAND: PronunciationDictionary = {
  id: "dictionary_brand", dictId: "p_brand", provider: "sarvam", name: "Brand names",
  description: null, languageWordCounts: { "hi-IN": 1 }, createdAt: null, updatedAt: null,
};
const APPLIANCES: PronunciationDictionary = {
  ...BRAND, id: "dictionary_appliances", dictId: "p_appliances", name: "Appliances",
};

function Harness({ initial = null, allowInherit = false, explicitNone = false, inheritedValue, changed = vi.fn() }: {
  initial?: string | null;
  allowInherit?: boolean;
  explicitNone?: boolean;
  inheritedValue?: string | null;
  changed?: (value: string | null) => void;
}) {
  const [value, setValue] = useState<string | null>(initial);
  return (
    <DictionaryField value={value} allowInherit={allowInherit} explicitNone={explicitNone} inheritedValue={inheritedValue}
      onChange={(next) => { setValue(next); changed(next); }} />
  );
}

async function openBrandEditor() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Manage" }));
  const brand = await screen.findByText("Brand names", { selector: "span" });
  await user.click(within(brand.closest("li")!).getByRole("button", { name: "Edit" }));
  await screen.findByDisplayValue("हायर");
  return user;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listPronunciationDictionaries).mockResolvedValue([BRAND, APPLIANCES]);
  vi.mocked(api.getModelLanguages).mockResolvedValue({
    languages: [{ code: "hi-IN", name: "Hindi" }],
  } as Awaited<ReturnType<typeof api.getModelLanguages>>);
  vi.mocked(api.getPronunciationDictionary).mockResolvedValue({
    ...BRAND, pronunciations: { "hi-IN": { Haier: "हायर" } },
  });
  vi.mocked(api.updatePronunciationDictionary).mockResolvedValue(BRAND);
});

describe("pronunciation dictionary selection", () => {
  it("selects an edited dictionary in the invoking draft, without merging another dictionary", async () => {
    const changed = vi.fn();
    render(<Harness changed={changed} />);
    const user = await openBrandEditor();
    await user.click(screen.getByRole("button", { name: "Add pronunciation" }));
    await user.type(screen.getByLabelText("Row 2 word"), "Refrigerator");
    await user.type(screen.getByLabelText("Row 2 spoken as"), "फ्रिज");
    await user.click(screen.getByRole("button", { name: "Save dictionary" }));

    await waitFor(() => expect(changed).toHaveBeenCalledWith("p_brand"));
    expect(screen.getByRole("combobox", { name: "Pronunciation dictionary" })).toHaveValue("p_brand");
    expect(api.updatePronunciationDictionary).toHaveBeenCalledExactlyOnceWith("dictionary_brand", {
      name: "Brand names", pronunciations: { "hi-IN": { Haier: "हायर", Refrigerator: "फ्रिज" } },
    });
    expect(api.getPronunciationDictionary).toHaveBeenCalledExactlyOnceWith("dictionary_brand");
    expect(APPLIANCES.languageWordCounts).toEqual({ "hi-IN": 1 });
    expect(screen.getByText(/Put all intended word rules in one selected dictionary/)).toBeInTheDocument();
    expect(screen.getByText(/save the bot's voice settings to use it in calls/)).toBeInTheDocument();
  });

  it("uses the saved provider id if an update returns a replacement id", async () => {
    vi.mocked(api.updatePronunciationDictionary).mockResolvedValue({ ...BRAND, dictId: "p_replacement" });
    const changed = vi.fn();
    render(<Harness changed={changed} />);
    const user = await openBrandEditor();
    await user.click(screen.getByRole("button", { name: "Save dictionary" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith("p_replacement"));
    expect(changed).not.toHaveBeenCalledWith("dictionary_brand");
  });

  it("falls back to the loaded provider id when older update metadata omits it", async () => {
    vi.mocked(api.getPronunciationDictionary).mockResolvedValue({
      ...BRAND, dictId: "p_loaded_detail", pronunciations: { "hi-IN": { Haier: "हायर" } },
    });
    vi.mocked(api.updatePronunciationDictionary).mockResolvedValue({ ...BRAND, dictId: "" });
    const changed = vi.fn();
    render(<Harness changed={changed} />);
    const user = await openBrandEditor();
    await user.click(screen.getByRole("button", { name: "Save dictionary" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith("p_loaded_detail"));
    expect(changed).not.toHaveBeenCalledWith("dictionary_brand");
  });

  it("selects a newly created dictionary only after a successful create", async () => {
    const created = { ...BRAND, id: "dictionary_new", dictId: "p_new", name: "Haier Hindi" };
    vi.mocked(api.createPronunciationDictionary).mockResolvedValue(created);
    const changed = vi.fn();
    render(<Harness changed={changed} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Manage" }));
    await user.click(screen.getByRole("button", { name: "New dictionary" }));
    await user.type(screen.getByLabelText("Dictionary name"), "Haier Hindi");
    await user.type(screen.getByLabelText("Row 1 word"), "Haier");
    await user.type(screen.getByLabelText("Row 1 spoken as"), "हायर");
    expect(changed).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Create dictionary" }));
    await waitFor(() => expect(changed).toHaveBeenCalledExactlyOnceWith("p_new"));
    expect(api.createPronunciationDictionary).toHaveBeenCalledExactlyOnceWith({
      name: "Haier Hindi", pronunciations: { "hi-IN": { Haier: "हायर" } },
    });
  });

  it("keeps the existing draft selection when an update fails", async () => {
    vi.mocked(api.updatePronunciationDictionary).mockRejectedValue(new Error("Provider rejected the dictionary"));
    const changed = vi.fn();
    render(<Harness initial="p_appliances" changed={changed} />);
    const user = await openBrandEditor();
    await user.click(screen.getByRole("button", { name: "Save dictionary" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Provider rejected the dictionary");
    expect(changed).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "Pronunciation dictionary" })).toHaveValue("p_appliances");
  });

  it("does not select a failed new dictionary", async () => {
    vi.mocked(api.createPronunciationDictionary).mockRejectedValue(new Error("Dictionary quota exceeded"));
    const changed = vi.fn();
    render(<Harness initial="p_appliances" changed={changed} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Manage" }));
    await user.click(screen.getByRole("button", { name: "New dictionary" }));
    await user.type(screen.getByLabelText("Dictionary name"), "New rules");
    await user.type(screen.getByLabelText("Row 1 word"), "Haier");
    await user.type(screen.getByLabelText("Row 1 spoken as"), "हायर");
    await user.click(screen.getByRole("button", { name: "Create dictionary" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Dictionary quota exceeded");
    expect(changed).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "Pronunciation dictionary" })).toHaveValue("p_appliances");
  });

  it("does not offer saving when dictionary contents could not be loaded", async () => {
    vi.mocked(api.getPronunciationDictionary).mockRejectedValue(new Error("Detail unavailable"));
    const changed = vi.fn();
    render(<Harness initial="p_appliances" changed={changed} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Manage" }));
    const brand = await screen.findByText("Brand names", { selector: "span" });
    await user.click(within(brand.closest("li")!).getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Detail unavailable");
    expect(screen.getByRole("button", { name: "Save dictionary" })).toBeDisabled();
    expect(api.updatePronunciationDictionary).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
  });
});

describe("language dictionary inheritance", () => {
  it("distinguishes inherited default, explicit None and a directly selected dictionary", async () => {
    const changed = vi.fn();
    render(<Harness allowInherit inheritedValue="p_brand" changed={changed} />);
    const select = screen.getByRole("combobox", { name: "Pronunciation dictionary" });
    const inherited = await screen.findByRole("option", { name: "Inherit default dictionary (Brand names)" });
    expect(inherited).toHaveProperty("selected", true);
    const user = userEvent.setup();
    await user.selectOptions(select, screen.getByRole("option", { name: "None" }));
    expect(changed).toHaveBeenLastCalledWith("");
    expect(select).toHaveValue("");
    await user.selectOptions(select, inherited);
    expect(changed).toHaveBeenLastCalledWith(null);
    await user.selectOptions(select, "p_appliances");
    expect(changed).toHaveBeenLastCalledWith("p_appliances");
    expect(changed.mock.calls.map(([value]) => value)).toEqual(["", null, "p_appliances"]);
  });

  it("renders explicit disable as None even when the default has a dictionary", async () => {
    render(<Harness initial="" allowInherit inheritedValue="p_brand" />);
    const inherited = await screen.findByRole("option", { name: "Inherit default dictionary (Brand names)" });
    expect(inherited).toHaveProperty("selected", false);
    expect(screen.getByRole("option", { name: "None" })).toHaveProperty("selected", true);
  });

  it("uses inheritance for an unset override without requiring a resolved default", () => {
    render(<DictionaryField value={undefined} onChange={vi.fn()} allowInherit inheritedValue={null} />);
    expect(screen.getByRole("option", { name: "Inherit default dictionary" })).toHaveProperty("selected", true);
  });

  it("preserves default-field None callbacks as null", async () => {
    const changed = vi.fn();
    render(<Harness initial="p_brand" changed={changed} />);
    const select = screen.getByRole("combobox", { name: "Pronunciation dictionary" });
    await screen.findByRole("option", { name: /Brand names/ });
    await userEvent.selectOptions(select, "");
    expect(changed).toHaveBeenCalledExactlyOnceWith(null);
    expect(screen.queryByRole("option", { name: /Inherit default dictionary/ })).not.toBeInTheDocument();
  });

  it("preserves explicit None when a language uses a different engine and cannot inherit", async () => {
    const changed = vi.fn();
    render(<Harness initial="p_brand" explicitNone changed={changed} />);
    const select = screen.getByRole("combobox", { name: "Pronunciation dictionary" });
    await screen.findByRole("option", { name: /Brand names/ });
    expect(screen.queryByRole("option", { name: /Inherit default dictionary/ })).not.toBeInTheDocument();
    await userEvent.selectOptions(select, "");
    expect(changed).toHaveBeenCalledExactlyOnceWith("");
    expect(select).toHaveValue("");
  });

  it("clears a deleted language selection to explicit None instead of inheriting another dictionary", async () => {
    vi.mocked(api.deletePronunciationDictionary).mockResolvedValue({ deleted: true, providerDeleted: true });
    const changed = vi.fn();
    render(<Harness initial="p_brand" allowInherit inheritedValue="p_appliances" changed={changed} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Manage" }));
    await user.click(await screen.findByRole("button", { name: "Delete dictionary Brand names" }));
    const confirmation = screen.getByRole("dialog", { name: "Delete pronunciation dictionary" });
    await user.click(within(confirmation).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(changed).toHaveBeenCalledExactlyOnceWith(""));
    expect(screen.getByRole("option", { name: "None" })).toHaveProperty("selected", true);
    expect(changed).not.toHaveBeenCalledWith("p_appliances");
  });
});

/* Pronunciation dictionary selector + manager (Sarvam bulbul:v3 dict_id).

   Tenants never see or type raw provider ids ("p_5cb7faa6") — they pick a
   named dictionary from their account, or manage word → "speak as" rows in
   the editor. The backend uploads the JSON to the provider and stores the
   returned id; the selected dictionary's dictId is what bot settings persist
   under the schema key (dict_id) and what preview/live TTS send. */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { PronunciationDictionary, PronunciationMap } from "@/types/domain";
import {
  createPronunciationDictionary, deletePronunciationDictionary,
  getModelLanguages, getPronunciationDictionary, listPronunciationDictionaries,
  updatePronunciationDictionary,
} from "@/services/api";
import { Button, ConfirmModal, Field, Modal } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

interface LangOption { code: string; name: string }

function countsSummary(counts: Record<string, number>): string {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const langs = Object.keys(counts).length;
  if (!total) return "empty";
  return `${total} word${total === 1 ? "" : "s"} · ${langs} language${langs === 1 ? "" : "s"}`;
}

/* ---------- selector ---------- */

export function DictionaryField({ value, onChange, disabled, help }: {
  /** Provider dict id currently selected (schema key dict_id). */
  value: string | null | undefined;
  onChange: (dictId: string | null) => void;
  disabled?: boolean;
  help?: string;
}) {
  const [dictionaries, setDictionaries] = useState<PronunciationDictionary[] | null>(null);
  const [manageOpen, setManageOpen] = useState(false);
  const { toast } = useApp();

  const reload = useCallback(async () => {
    try {
      setDictionaries(await listPronunciationDictionaries());
    } catch {
      setDictionaries([]); // selector degrades to None + current value
    }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  const known = (dictionaries ?? []).some((d) => d.dictId === value);
  return (
    <Field label="Pronunciation dictionary" plain hint={help}>
      <div className="row gap-8" style={{ alignItems: "stretch" }}>
        <select
          className="select" style={{ flex: 1, minWidth: 0 }}
          aria-label="Pronunciation dictionary"
          value={value ?? ""} disabled={disabled}
          onChange={(e) => onChange(e.target.value || null)}
        >
          <option value="">None</option>
          {value && !known && <option value={value}>{value} (unavailable)</option>}
          {(dictionaries ?? []).map((d) => (
            <option key={d.id} value={d.dictId}>
              {d.name} — {countsSummary(d.languageWordCounts)}
            </option>
          ))}
        </select>
        <Button
          size="sm" icon="settings" disabled={disabled}
          title="Create or edit pronunciation dictionaries"
          onClick={() => setManageOpen(true)}
        >
          Manage
        </Button>
      </div>
      {manageOpen && (
        <DictionaryManagerModal
          onClose={() => setManageOpen(false)}
          onChanged={(next) => {
            void reload();
            /* Deleting the selected dictionary clears the selection so a
               dangling provider id is never staged. */
            if (value && next.deletedDictId === value) onChange(null);
            if (next.selectDictId) onChange(next.selectDictId);
          }}
          toast={toast}
        />
      )}
    </Field>
  );
}

/* ---------- manager modal ---------- */

interface Row { locale: string; word: string; spokenAs: string }

function toRows(pronunciations: PronunciationMap | undefined): Row[] {
  const rows: Row[] = [];
  for (const [locale, entries] of Object.entries(pronunciations ?? {})) {
    for (const [word, spokenAs] of Object.entries(entries)) rows.push({ locale, word, spokenAs });
  }
  return rows;
}

function toMap(rows: Row[]): PronunciationMap {
  const map: PronunciationMap = {};
  for (const row of rows) {
    if (!row.word.trim() || !row.spokenAs.trim()) continue;
    (map[row.locale] ??= {})[row.word.trim()] = row.spokenAs.trim();
  }
  return map;
}

export function DictionaryManagerModal({ onClose, onChanged, toast }: {
  onClose: () => void;
  onChanged: (next: { deletedDictId?: string; selectDictId?: string }) => void;
  toast: (message: string) => void;
}) {
  const [dictionaries, setDictionaries] = useState<PronunciationDictionary[] | null>(null);
  const [languages, setLanguages] = useState<LangOption[]>([]);
  /* null = list view; "new" = creating; otherwise the id being edited. */
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<PronunciationDictionary | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setDictionaries(await listPronunciationDictionaries());
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not load dictionaries");
        setDictionaries([]);
      }
      try {
        /* Dictionaries are a bulbul:v3 capability — offer exactly the
           languages that model supports on this platform. */
        const info = await getModelLanguages("tts", "sarvam", "bulbul:v3");
        setLanguages(info.languages.map((l) => ({ code: l.code, name: l.name })));
      } catch {
        setLanguages([]);
      }
    })();
  }, []);

  const defaultLocale = languages[0]?.code ?? "hi-IN";

  const openEditor = async (dictionary: PronunciationDictionary | null) => {
    setError(null);
    if (dictionary === null) {
      setEditing("new");
      setName("");
      setRows([{ locale: defaultLocale, word: "", spokenAs: "" }]);
      return;
    }
    setEditing(dictionary.id);
    setName(dictionary.name);
    setRows([]);
    setLoadingDetail(true);
    try {
      const detail = await getPronunciationDictionary(dictionary.id);
      setRows(toRows(detail.pronunciations));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the dictionary contents");
    } finally {
      setLoadingDetail(false);
    }
  };

  const wordCount = useMemo(
    () => rows.filter((r) => r.word.trim() && r.spokenAs.trim()).length, [rows],
  );
  const canSave = Boolean(name.trim()) && wordCount > 0 && wordCount <= 100 && !busy && !loadingDetail;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const pronunciations = toMap(rows);
      if (editing === "new") {
        const created = await createPronunciationDictionary({ name: name.trim(), pronunciations });
        toast(`Dictionary "${created.name}" created`);
        onChanged({ selectDictId: created.dictId });
      } else if (editing) {
        await updatePronunciationDictionary(editing, { name: name.trim(), pronunciations });
        toast(`Dictionary "${name.trim()}" updated`);
        onChanged({});
      }
      setDictionaries(await listPronunciationDictionaries());
      setEditing(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Saving the dictionary failed");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (dictionary: PronunciationDictionary) => {
    setBusy(true);
    setError(null);
    try {
      await deletePronunciationDictionary(dictionary.id);
      toast(`Dictionary "${dictionary.name}" deleted`);
      onChanged({ deletedDictId: dictionary.dictId });
      setDictionaries(await listPronunciationDictionaries());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Deleting the dictionary failed");
    } finally {
      setBusy(false);
      setConfirmDelete(null);
    }
  };

  const setRow = (index: number, patch: Partial<Row>) =>
    setRows((r) => r.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  return (
    <Modal
      open onClose={onClose} wide
      title="Pronunciation dictionaries"
      sub="Fix how brands, acronyms and names are spoken (Sarvam bulbul:v3)"
      footer={
        editing === null ? (
          <>
            <Button variant="ghost" onClick={onClose}>Close</Button>
            <Button
              variant="primary" icon="plus" disabled={busy}
              onClick={() => void openEditor(null)}
            >
              New dictionary
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={() => setEditing(null)} disabled={busy}>Back</Button>
            <Button
              variant="primary" icon="check" busy={busy} disabled={!canSave}
              title={canSave ? undefined : "Name plus 1–100 complete rows required"}
              onClick={() => void save()}
            >
              {editing === "new" ? "Create dictionary" : "Save dictionary"}
            </Button>
          </>
        )
      }
    >
      <div className="col gap-12">
        {error && <p className="field-error" role="alert"><Icon name="alert" size={12} />{error}</p>}

        {editing === null ? (
          dictionaries === null ? (
            <p className="t-sub">Loading…</p>
          ) : dictionaries.length === 0 ? (
            <p className="t-sub">
              No pronunciation dictionaries yet. Create one to control how specific
              words are spoken — for example EMI → “ई एम आई”.
            </p>
          ) : (
            <ul className="dict-list">
              {dictionaries.map((d) => (
                <li key={d.id} className="dict-row">
                  <div className="col gap-2" style={{ minWidth: 0 }}>
                    <span className="t-strong" style={{ fontSize: 13 }}>{d.name}</span>
                    <span className="t-micro">{countsSummary(d.languageWordCounts)}</span>
                  </div>
                  <div className="row gap-6">
                    <Button size="sm" icon="edit" onClick={() => void openEditor(d)} disabled={busy}>Edit</Button>
                    <Button
                      size="sm" variant="ghost" icon="trash" disabled={busy}
                      aria-label={`Delete dictionary ${d.name}`}
                      onClick={() => setConfirmDelete(d)}
                    >
                      Delete
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : (
          <>
            <Field label="Dictionary name" plain>
              <input
                className="input" value={name} maxLength={100}
                aria-label="Dictionary name" placeholder="e.g. Collections Hindi"
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            {loadingDetail ? (
              <p className="t-sub">Loading dictionary contents…</p>
            ) : (
              <div className="col gap-8">
                <div className="dict-grid dict-grid-head" aria-hidden>
                  <span className="t-label">Language</span>
                  <span className="t-label">Word / text</span>
                  <span className="t-label">Speak as</span>
                  <span />
                </div>
                {rows.map((row, index) => (
                  <div className="dict-grid" key={index}>
                    <select
                      className="select" value={row.locale}
                      aria-label={`Row ${index + 1} language`}
                      onChange={(e) => setRow(index, { locale: e.target.value })}
                    >
                      {!languages.some((l) => l.code === row.locale) && (
                        <option value={row.locale}>{row.locale}</option>
                      )}
                      {languages.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
                    </select>
                    <input
                      className="input" value={row.word} maxLength={200}
                      aria-label={`Row ${index + 1} word`} placeholder="CIBIL"
                      onChange={(e) => setRow(index, { word: e.target.value })}
                    />
                    <input
                      className="input" value={row.spokenAs} maxLength={200}
                      aria-label={`Row ${index + 1} spoken as`} placeholder="सिबिल"
                      onChange={(e) => setRow(index, { spokenAs: e.target.value })}
                    />
                    <Button
                      size="sm" variant="ghost" icon="trash"
                      aria-label={`Delete row ${index + 1}`}
                      onClick={() => setRows((r) => r.filter((_, i) => i !== index))}
                    />
                  </div>
                ))}
                <div className="row-between">
                  <Button
                    size="sm" icon="plus"
                    onClick={() => setRows((r) => [...r, { locale: defaultLocale, word: "", spokenAs: "" }])}
                  >
                    Add pronunciation
                  </Button>
                  <span className="t-micro">{wordCount}/100 words</span>
                </div>
              </div>
            )}
          </>
        )}
      </div>
      <ConfirmModal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => confirmDelete && void remove(confirmDelete)}
        title="Delete pronunciation dictionary"
        body={
          <>Deleting <strong>{confirmDelete?.name}</strong> removes it from the provider
          account. Bots still referencing it will synthesize without it.</>
        }
        confirmLabel="Delete" danger busy={busy}
      />
    </Modal>
  );
}

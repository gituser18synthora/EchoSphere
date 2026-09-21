"""Enable English-only MDND extraction, preserving all Hindi configuration.

Run on each environment: env/bin/python zepto/setup/13_mdnd_english_extraction.py
Preview by default; --apply saves through the authoring API with a backup.
Deploy the matching runtime code before applying. No prompt/voice edits.
"""
import argparse
import copy
import json
import runpy
import tempfile
from contextlib import closing
from pathlib import Path

BOT_ID = "bot_59a84478f155"
WORKFLOW_ID = "wf_7e4cf166c7bd"


def patched_nodes(nodes):
    out = copy.deepcopy(nodes)
    start = next(n for n in out if n["id"] == "n_start")
    config = start["config"]
    if config.get("semanticSlots") != "mdnd_v1":
        raise ValueError("Expected existing MDND state guards")
    # Do not narrow an already enabled multilingual extractor silently.
    if config.get("semanticExtraction") == "llm" and config.get("semanticExtractionLanguages") != ["en"]:
        raise ValueError("Existing semantic extraction configuration requires review")
    config.update(semanticExtraction="llm", semanticExtractionLanguages=["en"])
    narrative = next(n for n in out if n["id"] == "n_ask_issue_desc")
    narrative["config"]["acceptUnderstoodNarrative"] = True
    english = runpy.run_path(str(Path(__file__).with_name("06_single_bots.py")))["MDND_ENGLISH_TEXT"]
    for node in out:
        if node["id"] in english:
            node["config"].setdefault("textByLanguage", {})["en"] = english[node["id"]]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    stage = runpy.run_path(str(Path(__file__).with_name("06_single_bots.py")))
    with closing(stage["client"]()) as client:
        wf = stage["check"](client.get(f"/bots/{BOT_ID}/workflow"), "read workflow")
        if wf["id"] != WORKFLOW_ID:
            raise ValueError("Unexpected workflow; refusing to overwrite it")
        nodes = patched_nodes(wf["nodes"])
        changed = [n["id"] for n, old in zip(nodes, wf["nodes"]) if n != old]
        print(json.dumps({"bot": BOT_ID, "workflow": WORKFLOW_ID,
                          "version": wf["version"], "changed_nodes": changed,
                          "extraction_languages": ["en"], "edges_changed": False}))
        if not args.apply or not changed:
            return
        with tempfile.NamedTemporaryFile(mode="w", prefix="mdnd-english-before-",
                                         suffix=".json", delete=False) as backup:
            json.dump(wf, backup, ensure_ascii=False, indent=2)
            print("Workflow backup:", backup.name)
        saved = stage["check"](client.put(f"/bots/{BOT_ID}/workflow", json={
            "name": wf["name"], "nodes": nodes, "edges": wf["edges"], "status": wf["status"],
        }), "save English extraction")
        assert saved["nodes"] == nodes and saved["edges"] == wf["edges"]
        print("Saved workflow version", saved["version"])


if __name__ == "__main__":
    main()

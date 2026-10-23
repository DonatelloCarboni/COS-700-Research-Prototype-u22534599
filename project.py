import getpass
import json
import os

import injector
import preflight
import resolver
import scanner
import writer

STATUSES = {
    "generated": "written by the model; replaced if the figure changes",
    "edited": "written or changed by a person; never replaced",
    "approved": "checked by a person; never replaced",
    "artifact": "decorative: no alt text, left out of tagging",
}

EDITABLE = ("describe", "reuse", "locked", "artifact")


class StoreError(Exception):
    pass


def store_path(document):
    return os.path.splitext(document)[0] + ".alt"


def pdf_path(document):
    stem = os.path.splitext(os.path.basename(document))[0]
    return os.path.join(os.path.dirname(document), injector.BUILD_DIR, stem + ".pdf")


def open_document(path):
    result = {"document": None, "store": None, "created": False, "error": None}
    if not path or os.path.splitext(path)[1].lower() != ".tex":
        result["error"] = "choose the main .tex file of the document"
        return result
    if not os.path.isfile(path):
        result["error"] = f"no such file: {path}"
        return result

    document = os.path.abspath(path)
    name = os.path.basename(document)
    try:
        text = preflight.read(document)
    except (OSError, UnicodeDecodeError) as exc:
        result["error"] = f"cannot read {name} as UTF-8 ({exc})"
        return result

    where = preflight.locate(preflight.blank_comments(text))
    if where["error"]:
        result["error"] = f"{name}: {where['error']}"
        return result

    store = store_path(document)
    result.update(document=document, store=store)
    if not os.path.exists(store):
        writer.save(store, {"schema": writer.SCHEMA}, {}, [], document)
        result["created"] = True
    return result


def overview(document):
    state = {"document": document, "figures": []}
    scanner.scan(state)
    config = state.get("config") or {}
    view = {
        "rows": [],
        "warnings": list(state.get("warnings", [])),
        "error": None,
        "files": state.get("tex_files", []),
        "budget": (config.get("defaults") or {}).get("alt_max_chars", 250),
    }

    store, warnings, fatal = resolver.load_store(store_path(document))
    view["warnings"] += warnings
    if fatal:
        view["error"] = fatal
        return view

    entries = store.get("entries", {})
    for figure in state["figures"]:
        entry = entries.get(figure["key"])
        row = resolver.decide(figure, entry, view["warnings"])
        row["entry"] = entry
        view["rows"].append(row)

    used = {f["key"] for f in state["figures"]}
    for key, entry in entries.items():
        if key not in used:
            view["rows"].append(orphan(key, entry))
    return view


def orphan(key, entry):
    return {
        "key": key, "outcome": "orphan", "alt": entry.get("alt"), "previous": None,
        "reason": "no figure in the document has this key; it was renamed or removed",
        "profile": entry.get("profile"), "where": None, "source": entry.get("source"),
        "kind": None, "caption": None, "hash": None, "file": None, "body": None,
        "entry": entry,
    }


def save_entry(document, key, alt, status, figure=None, who=None):
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; use one of {', '.join(STATUSES)}")
    figure = figure or {}
    outcome = figure.get("outcome")
    if outcome == "inline":
        raise ValueError("this figure has alt= in the .tex source, which takes "
                         "priority over the .alt file. Change the text there instead")
    if outcome == "blocked":
        raise ValueError("its image file is not on disk, so there is no figure to "
                         "save text for yet. Add the file first")
    if (figure.get("profile") == resolver.ARTIFACT and outcome != "orphan"
            and status != resolver.ARTIFACT):
        raise ValueError("the document marks this figure decorative (with a %!alt "
                         "artifact line or a decorative filename), and the .alt file "
                         "cannot override that. Give it a %!alt <profile> line in "
                         "the source instead")

    path = store_path(document)
    store = read_store(path)
    entries = store["entries"]
    existing = entries.get(key)
    if existing is not None and not isinstance(existing, dict):
        raise StoreError(f"the entry for {key!r} in {os.path.basename(path)} is not "
                         f"an object. Fix it by hand")
    old = existing or {}

    text = " ".join((alt or "").split())
    if status == resolver.ARTIFACT:
        text = None
    elif not text:
        raise ValueError("there is no alt text to save. Write some, or mark the "
                         "figure decorative")

    live = figure.get("hash") or old.get("hash")
    words_changed = text != old.get("alt")
    note = f"saved as {status}"
    if status == "generated" and (existing is None or words_changed
                                  or live != old.get("hash")):
        status = "edited"
        note = "saved as edited, since the text was written or checked by a person"

    entry = dict(old)
    entry["alt"] = text
    entry["profile"] = (resolver.ARTIFACT if status == resolver.ARTIFACT
                        else figure.get("profile") or old.get("profile"))
    entry["source"] = figure.get("source") or old.get("source")
    if live is not None:
        entry["hash"] = live
    entry["status"] = status
    if existing is None or words_changed:
        entry["by"] = who or person()

    if entry == old:
        return {"entry": old, "wrote": False,
                "note": "nothing changed, so nothing was written"}
    entry["at"] = writer.now()

    order = list(entries)
    entries[key] = entry
    writer.save(path, store, entries, order, document)
    return {"entry": entry, "wrote": True, "note": note}


def remove_entry(document, key):
    path = store_path(document)
    store = read_store(path)
    if key not in store["entries"]:
        return False
    entries = {k: v for k, v in store["entries"].items() if k != key}
    writer.save(path, store, entries, list(entries), document)
    return True


def read_store(path):
    if not os.path.isfile(path):
        return {"schema": writer.SCHEMA, "entries": {}}
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as fh:
            store = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise StoreError(f"{name} is not readable JSON ({exc}). Fix it by hand "
                         f"before saving")
    if not isinstance(store, dict) or not isinstance(store.get("entries"), dict):
        raise StoreError(f"{name} has no 'entries' object, so it is not an .alt file")
    return store


def person():
    try:
        return getpass.getuser()
    except Exception:
        return "author"

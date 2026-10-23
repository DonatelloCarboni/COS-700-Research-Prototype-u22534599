import json
import os

LOCKED = ("edited", "approved")

STUB = "stub"

ARTIFACT = "artifact"

QUEUED = "describe"


def resolve(state):
    path = state["document"]
    figures = state["figures"]
    store_path = os.path.splitext(path)[0] + ".alt"

    store, warnings, fatal = load_store(store_path)
    if fatal:
        state["stop"] = True
        print(f"  {fatal}")
        return "cannot read the .alt store, so nothing can be resolved"

    verbose = state.get("verbose")
    entries = store.get("entries", {})
    if store.get("_absent") and verbose:
        print(f"  no {os.path.basename(store_path)} yet, so every figure is new")

    resolved = []
    for figure in figures:
        resolved.append(decide(figure, entries.get(figure["key"]), warnings))

    for key in entries:
        if not any(f["key"] == key for f in figures):
            warnings.append(
                f"{os.path.basename(store_path)} has an entry for {key!r} but no such "
                f"figure is in the document; it was renamed or removed"
            )

    requests = [r for r in resolved if r["outcome"] == QUEUED]
    blocked = [r for r in resolved if r["outcome"] == "blocked"]

    state["resolved"] = resolved
    state["requests"] = requests
    state["warnings"] = state.get("warnings", []) + warnings

    if verbose:
        for w in warnings:
            print(f"  warning: {w}")
        for r in blocked:
            print(f"  blocked: {r['key']}: {r['reason']}")

    write_requests(path, requests)
    return summarise(resolved, len(warnings) if not verbose else 0)


def decide(figure, entry, warnings):
    key, where = figure["key"], figure["where"]

    if figure["alt"]:
        if entry and entry.get("alt") and entry["alt"] != figure["alt"]:
            warnings.append(
                f"{where}: {key!r} has inline alt= and a different .alt entry; the "
                f"inline text is used and the entry is left as it is"
            )
        return outcome(figure, "inline", figure["alt"],
                       "alt= in the source takes priority over the .alt file")

    if figure["profile"] == ARTIFACT or (entry and entry.get("status") == ARTIFACT):
        if figure["caption"]:
            warnings.append(
                f"{where}: {key!r} is decorative but has a caption; a captioned "
                f"figure usually needs alt text"
            )
        return outcome(figure, ARTIFACT, None, "decorative: no alt text needed")

    if figure["hash"] is None:
        return outcome(figure, "blocked", entry.get("alt") if entry else None,
                       "its file is not on disk, so it cannot be hashed or rendered")

    if entry is None:
        return outcome(figure, QUEUED, None, "no entry in the .alt file")

    status = entry.get("status")
    stored = entry.get("hash")
    text = entry.get("alt")

    if status in LOCKED:
        if stored and stored != figure["hash"]:
            warnings.append(
                f"{where}: {key!r} is {status} but the figure has changed since the "
                f"text was written; the text is kept, so check it still fits"
            )
            return outcome(figure, "locked", text,
                           f"{status}: kept although the figure changed")
        return outcome(figure, "locked", text, f"{status}: owned by a person")

    if not text:
        return outcome(figure, QUEUED, None,
                       f"entry has status {status!r} but no alt text")
    if entry.get("by") == STUB:
        warnings.append(
            f"{where}: {key!r} has placeholder text from an --offline run, so it "
            f"is queued to be described again"
        )
        return outcome(figure, QUEUED, None,
                       "written by the --offline stub, so it is not a description",
                       previous=text)
    if stored == figure["hash"]:
        return outcome(figure, "reuse", text, "hash matches: reused, no model call")

    return outcome(figure, QUEUED, None,
                   f"figure changed since it was described "
                   f"(stored {shorten(stored)}, live {shorten(figure['hash'])})",
                   previous=text)


def outcome(figure, name, alt, reason, previous=None):
    return {
        "key": figure["key"],
        "outcome": name,
        "alt": alt,
        "previous": previous,
        "reason": reason,
        "profile": figure["profile"],
        "where": figure["where"],
        "source": figure["source"],
        "kind": figure["kind"],
        "caption": figure["caption"],
        "hash": figure["hash"],
        "file": figure.get("file"),
        "body": figure.get("body"),
    }


def load_store(store_path):
    if not os.path.isfile(store_path):
        return {"entries": {}, "_absent": True}, [], None

    try:
        with open(store_path, encoding="utf-8") as fh:
            store = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return {}, [], (f"{store_path} is not readable JSON ({exc}). Fix or remove it "
                        f"so no hand-written alt text is overwritten.")

    if not isinstance(store.get("entries"), dict):
        return {}, [], (f"{store_path} has no 'entries' object, so it is not an .alt "
                        f"store. Fix or remove it.")

    warnings = []
    schema = store.get("schema")
    if schema != "alt/1":
        warnings.append(f"{os.path.basename(store_path)} says schema {schema!r}, "
                        f"expected 'alt/1'; reading it anyway")
    warnings += check_entries(store, os.path.basename(store_path))
    return store, warnings, None


def check_entries(store, name):
    entries, warnings, kept = store["entries"], [], {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            warnings.append(f"{name}: the entry for {key!r} is "
                            f"{type(entry).__name__}, not an object; ignoring it. "
                            f"The figure will be described again")
            continue
        alt = entry.get("alt")
        if alt is not None and not isinstance(alt, str):
            warnings.append(f"{name}: {key!r} has a non-text 'alt' "
                            f"({type(alt).__name__}); ignoring it. The figure will "
                            f"be described again")
            continue
        kept[key] = entry
    store["entries"] = kept
    return warnings


def write_requests(document, requests):
    folder = os.path.join(os.path.dirname(document) or ".", "_alt")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(document))[0]
    out = os.path.join(folder, stem + ".altreq")

    lines = [str(len(requests))]
    width = max([len(r["key"]) for r in requests], default=0)
    for r in requests:
        lines.append(f"{r['key']:<{width}}  profile={r['profile']:<9} "
                     f"live={r['hash']}  {r['reason']}")

    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return out


def summarise(resolved, hidden_warnings=0):
    order = ["describe", "blocked", "stale", "reuse", "locked", "inline", ARTIFACT]
    counts = {}
    for r in resolved:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    parts = [f"{counts[name]} {name}" for name in order if name in counts]
    for name in sorted(counts):
        if name not in order:
            parts.append(f"{counts[name]} {name}")
    total = len(resolved)
    line = f"{total} figure(s): " + (", ".join(parts) if parts else "nothing to do")
    if hidden_warnings:
        line += f", {hidden_warnings} warning(s)"
    return line


def shorten(digest):
    if not digest:
        return "none"
    body = digest.split(":", 1)[-1]
    return body[:4] + ".."

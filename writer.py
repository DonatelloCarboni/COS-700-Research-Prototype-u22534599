import json
import os
import time

import resolver

LOCKED = ("edited", "approved", "artifact")

STUB = "stub"

SCHEMA = "alt/1"


def write(state):
    proposals = [d for d in state.get("described", []) if d.get("ok")]
    document = state["document"]
    store_path = os.path.splitext(document)[0] + ".alt"

    if not proposals:
        state["written"] = {"added": [], "refreshed": [], "refused": [],
                            "rejected": [], "stubbed": []}
        return "nothing to write"

    store, load_warnings, fatal = resolver.load_store(store_path)
    if fatal:
        state["stop"] = True
        print(f"  {fatal}")
        return "cannot read the .alt store, so nothing was written"

    entries = dict(store.get("entries", {}))
    original_order = list(store.get("entries", {}))
    sources = {f["key"]: f.get("source") for f in state.get("figures", [])}

    added, refreshed, refused, rejected, stubbed = [], [], [], [], []
    warnings = []

    for proposal in proposals:
        key = proposal.get("key")
        problem = validate(proposal)
        if problem:
            rejected.append(key)
            warnings.append(f"{key}: not written, {problem}")
            continue

        existing = entries.get(key)
        if existing and existing.get("status") in LOCKED:
            refused.append(key)
            warnings.append(
                f"{key}: left alone, it is {existing['status']} "
                f"(stage 2 should not have queued it)"
            )
            continue

        entries[key] = merge(existing, proposal, sources.get(key))
        (refreshed if existing else added).append(key)
        if proposal.get("by") == STUB:
            stubbed.append(key)

    if not (added or refreshed):
        state["written"] = {"added": [], "refreshed": [], "refused": refused,
                            "rejected": rejected, "stubbed": []}
        state["warnings"] = state.get("warnings", []) + load_warnings + warnings
        if state.get("verbose"):
            for w in load_warnings + warnings:
                print(f"  warning: {w}")
        return summarise(added, refreshed, refused, rejected,
                         len(load_warnings + warnings), state.get("verbose"))

    if stubbed:
        warnings.append(
            f"{len(stubbed)} entry(ies) have placeholder text from --offline: "
            f"{', '.join(stubbed[:5])}"
            f"{' ...' if len(stubbed) > 5 else ''}. Run again without --offline to "
            f"replace them"
        )

    save(store_path, store, entries, original_order, document)

    state["written"] = {"added": added, "refreshed": refreshed,
                        "refused": refused, "rejected": rejected,
                        "stubbed": stubbed}
    state["warnings"] = state.get("warnings", []) + load_warnings + warnings
    if state.get("verbose"):
        for w in load_warnings + warnings:
            print(f"  warning: {w}")
    return summarise(added, refreshed, refused, rejected,
                     len(load_warnings + warnings), state.get("verbose"))


def validate(proposal):
    if not proposal.get("key"):
        return "it has no key"
    alt = proposal.get("alt")
    if not isinstance(alt, str) or not alt.strip():
        return "it has no alt text"
    if proposal.get("status") != "generated":
        return (f"its status is {proposal.get('status')!r}; this stage only "
                f"writes 'generated'")
    if not proposal.get("by"):
        return "it does not say what produced it"
    if not proposal.get("hash"):
        return "it has no hash"
    return None


def merge(existing, proposal, source):
    entry = dict(existing) if existing else {}
    entry.update({
        "alt": proposal["alt"].strip(),
        "profile": proposal.get("profile") or entry.get("profile"),
        "source": source or entry.get("source"),
        "hash": proposal["hash"],
        "status": "generated",
        "by": proposal["by"],
        "at": proposal.get("at") or now(),
    })
    return entry


def save(store_path, store, entries, original_order, document):
    ordered = {}
    for key in original_order:
        if key in entries:
            ordered[key] = entries[key]
    for key in entries:
        if key not in ordered:
            ordered[key] = entries[key]

    out = {k: v for k, v in store.items() if k not in ("entries", "_absent")}
    out.setdefault("schema", SCHEMA)
    out.setdefault("document", os.path.basename(document))
    out["updated"] = now()
    out["entries"] = ordered

    temporary = store_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, store_path)


def now():
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())


def summarise(added, refreshed, refused, rejected, warnings, verbose):
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if refreshed:
        parts.append(f"{len(refreshed)} refreshed")
    if refused:
        parts.append(f"{len(refused)} refused (human-owned)")
    if rejected:
        parts.append(f"{len(rejected)} rejected (malformed)")
    line = ", ".join(parts) if parts else "nothing to write"
    if warnings and not verbose:
        line += f", {warnings} warning(s)"
    return line

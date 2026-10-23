import contextlib
import math
import os
import queue
import subprocess
import sys
import textwrap
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pipeline
import project
import renderer
import resolver

SCALE = 1.0
PAD = 8

FONT = ("Segoe UI", 10)
MONO = ("Consolas", 9)
MUTED = "#6b7280"
AMBER = "#b45309"
RED = "#b91c1c"
GREEN = "#15803d"
BLUE = "#1d4ed8"

PREVIEW = (170, 110)

STAGE_INFO = {
    "preflight": (
        "can this document be tagged?",
        "Compiles the document, then checks that it turns tagging on, loads "
        "graphicx and tikz, and reads the file stage 6 writes. Anything missing "
        "is shown before it is added, and the original is kept as a .bak copy."),
    "scan": (
        "find the figures",
        "Reads the main file and every file it \\input-s, and lists each figure "
        "with its key, image file, caption and profile."),
    "resolve": (
        "what still needs describing?",
        "Checks every figure against {stem}.alt. Text a person wrote or approved "
        "is kept, and model text is reused while the figure is unchanged. "
        "Writes _alt/{stem}.altreq."),
    "render": (
        "figure -> image",
        "Turns each figure that still needs text into a PNG for the model. "
        "Writes _alt/renders/."),
    "describe": (
        "image -> alt text",
        "Sends each image to the model for a description. With Offline ticked it "
        "writes placeholder text instead. Writes _alt/responses/."),
    "write": (
        "alt text -> .alt file",
        "Adds the new descriptions to {stem}.alt as 'generated'. Edited, approved "
        "and decorative entries are not overwritten."),
    "inject": (
        ".alt -> tagged PDF",
        "Writes _alt/{stem}.alt.tex and compiles build/{stem}.pdf with tagging "
        "on."),
    "validate": (
        "check the PDF",
        "Checks every figure in the PDF has real alt text and not just its "
        "filename. Also runs veraPDF if it is installed."),
}

OUTCOMES = {
    "reuse": ("model text",
              "Written by the model for this version of the figure. Reused until "
              "the figure changes."),
    "artifact": ("decorative",
                 "Has no alt text and is left out of tagging."),
    "inline": ("alt= in source",
               "The text is set with alt= in the .tex file, which takes priority "
               "over the .alt file. Change it there."),
    "blocked": ("file missing",
                "The image file is not on disk, so it cannot be described or "
                "tagged."),
    "orphan": ("not in document",
               "The .alt file has this entry but no figure uses its key. Remove "
               "it, or restore the figure's \\label."),
}

TAGS = {"describe": "attention", "blocked": "fault", "orphan": "quiet",
        "inline": "quiet", "artifact": "quiet"}

MARKS = {"waiting": "[ ]", "running": "[>]", "done": "[x]", "stopped": "[!]"}


def px(n):
    return int(round(n * SCALE))


def enable(widget, on):
    widget.state(["!disabled"] if on else ["disabled"])


def one_line(text, limit=90):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "..."


def status_of(row):
    outcome = row["outcome"]
    entry = row.get("entry") or {}
    if outcome == "describe":
        if entry.get("by") == resolver.STUB:
            return "placeholder", ("It has placeholder text from an offline run. "
                                   "The next real run replaces it.")
        if row.get("previous"):
            return "figure changed", ("The figure changed after its text was written, "
                                      "so the next run describes it again. The old "
                                      "text is shown below.")
        return "needs alt text", ("Nothing is stored for it yet. The next run asks the "
                                  "model, or you can write the text and save it.")
    if outcome == "locked":
        return entry.get("status", "locked"), ("A person wrote or approved this "
                                               "text, so no run will replace it.")
    return OUTCOMES.get(outcome, (outcome, ""))


def preview_candidates(document, row):
    found = []
    if row.get("file") and os.path.splitext(row["file"])[1].lower() in (".png", ".gif"):
        found.append(row["file"])
    if row.get("hash"):
        render = os.path.join(os.path.dirname(document), "_alt", "renders",
                              renderer.render_name(row))
        if os.path.isfile(render):
            found.append(render)
    return found


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class QueueWriter:
    def __init__(self, events):
        self.events = events

    def write(self, text):
        if text:
            self.events.put(("log", text))
        return len(text)

    def flush(self):
        pass


class App:
    def __init__(self, root, document=None):
        self.root = root
        self.document = None
        self.rows = {}
        self.warnings = []
        self.budget = 250
        self.current = None
        self.current_key = None
        self.original = ("", "")
        self.dirty = False
        self.loading = False
        self.preview_image = None

        self.run_state = None
        self.next_stage = 0
        self.busy = False
        self.results = {}
        self.events = queue.Queue()

        root.title("Alt text")
        width = min(px(1240), root.winfo_screenwidth() - px(40))
        height = min(px(880), root.winfo_screenheight() - px(80))
        root.geometry(f"{width}x{height}")
        root.minsize(min(px(900), width), min(px(600), height))
        if height < px(880) and sys.platform == "win32":
            root.state("zoomed")

        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-o>", lambda _e: self.choose_document())
        root.bind("<Control-s>", lambda _e: self.save())
        root.after(80, self.pump)
        root.after(200, self.place_sashes)
        self.update_controls()
        if document:
            root.after(50, lambda: self.open_document(document))


    def build(self):
        style = ttk.Style(self.root)
        style.configure(".", font=FONT)
        style.configure("Head.TLabel", font=(FONT[0], 11, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Treeview", rowheight=px(23))
        style.configure("Treeview.Heading", font=(FONT[0], 9, "bold"))

        top = ttk.Frame(self.root, padding=(PAD * 2, PAD * 2, PAD * 2, PAD))
        top.pack(fill="x")
        self.open_button = ttk.Button(top, text="Open main.tex...",
                                      command=self.choose_document)
        self.open_button.pack(side="left")
        self.pdf_button = ttk.Button(top, text="Open PDF", command=self.open_pdf)
        self.pdf_button.pack(side="right")
        self.store_button = ttk.Button(top, text="Open .alt file", command=self.open_store)
        self.store_button.pack(side="right", padx=PAD)
        self.reload_button = ttk.Button(top, text="Reload", command=self.reload)
        self.reload_button.pack(side="right")
        self.doc_label = ttk.Label(top, text="No document open", style="Muted.TLabel")
        self.doc_label.pack(side="left", padx=PAD * 2)

        self.banner = ttk.Label(self.root, text="", foreground=AMBER,
                                padding=(PAD * 2, 0, PAD * 2, PAD // 2))
        self.banner.pack(fill="x")
        self.banner.bind("<Button-1>", lambda _e: self.log_warnings())

        self.split = ttk.Panedwindow(self.root, orient="vertical")
        self.split.pack(fill="both", expand=True, padx=PAD * 2, pady=(0, PAD * 2))
        self.upper = ttk.Panedwindow(self.split, orient="horizontal")
        self.split.add(self.upper, weight=3)
        self.upper.add(self.build_figures(self.upper), weight=2)
        self.upper.add(self.build_editor(self.upper), weight=3)
        self.split.add(self.build_pipeline(self.split), weight=1)

    def place_sashes(self):
        self.root.update_idletasks()
        total = self.split.winfo_height()
        lower = self.pipeline_frame.winfo_reqheight()
        self.split.sashpos(0, max(total // 2, total - lower - px(4)))
        self.upper.sashpos(0, int(self.upper.winfo_width() * 0.42))

    def build_figures(self, parent):
        frame = ttk.Frame(parent, padding=(0, 0, PAD, PAD))
        ttk.Label(frame, text="Figures", style="Head.TLabel").pack(anchor="w")
        self.figures_note = ttk.Label(
            frame, text="Open the main .tex file of a document to begin.",
            style="Muted.TLabel")
        self.figures_note.pack(anchor="w", pady=(0, PAD // 2))

        box = ttk.Frame(frame)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("key", "status", "alt"),
                                 show="headings", selectmode="browse")
        for column, title, width, stretch in (("key", "Figure", 170, False),
                                              ("status", "Status", 110, False),
                                              ("alt", "Alt text", 260, True)):
            self.tree.heading(column, text=title, anchor="w")
            self.tree.column(column, width=px(width), stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("attention", foreground=AMBER)
        self.tree.tag_configure("fault", foreground=RED)
        self.tree.tag_configure("quiet", foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        return frame

    def build_editor(self, parent):
        frame = ttk.Frame(parent, padding=(PAD * 2, 0, 0, PAD))

        actions = ttk.Frame(frame)
        self.save_button = ttk.Button(actions, text="Save to .alt file", command=self.save)
        self.save_button.pack(side="left")
        self.remove_button = ttk.Button(actions, text="Remove entry", command=self.remove)
        self.remove_button.pack(side="left", padx=PAD)
        self.figure_button = ttk.Button(actions, text="Open figure", command=self.open_figure)
        self.figure_button.pack(side="right")
        self.note = ttk.Label(frame, text="", style="Muted.TLabel", justify="left",
                              wraplength=px(520))

        self.heading = ttk.Label(frame, text="No figure selected", style="Head.TLabel")

        facts = ttk.Frame(frame)
        facts.columnconfigure(1, weight=1)
        self.facts = {}
        for row, name in enumerate(("Written in", "File", "Profile", "Caption")):
            ttk.Label(facts, text=name, style="Muted.TLabel").grid(
                row=row, column=0, sticky="nw", padx=(0, PAD), pady=1)
            value = ttk.Label(facts, text="", justify="left", wraplength=px(320))
            value.grid(row=row, column=1, sticky="nw", pady=1)
            self.facts[name] = value
        self.preview = ttk.Label(facts, text="", style="Muted.TLabel", anchor="center",
                                 justify="center")
        self.preview.grid(row=0, column=2, rowspan=4, sticky="ne", padx=(PAD, 0))

        self.explain = ttk.Label(frame, text="", justify="left", wraplength=px(520))

        head = ttk.Frame(frame)
        ttk.Label(head, text="Alt text", style="Head.TLabel").pack(side="left")
        self.counter = ttk.Label(head, text="", style="Muted.TLabel")
        self.counter.pack(side="right")
        self.text = tk.Text(frame, height=3, wrap="word", font=FONT, relief="solid",
                            borderwidth=1, padx=6, pady=6, undo=True,
                            highlightthickness=0)
        self.text.bind("<<Modified>>", self.on_text_changed)

        choice = ttk.Frame(frame)
        ttk.Label(choice, text="Status", style="Head.TLabel").pack(side="left",
                                                                    padx=(0, PAD))
        self.status = tk.StringVar()
        self.status.trace_add("write", lambda *_: self.on_status_changed())
        self.status_buttons = []
        for value in project.STATUSES:
            button = ttk.Radiobutton(choice, text=value, value=value, variable=self.status)
            button.pack(side="left", padx=(0, PAD))
            self.status_buttons.append(button)
        self.status_help = ttk.Label(frame, text="", style="Muted.TLabel", justify="left",
                                     wraplength=px(520))

        actions.pack(side="bottom", fill="x", pady=(PAD // 2, 0))
        self.note.pack(side="bottom", fill="x", pady=(PAD // 2, 0))
        self.status_help.pack(side="bottom", fill="x")
        choice.pack(side="bottom", fill="x", pady=(PAD, 0))
        self.text.pack(side="bottom", fill="both", expand=True)
        head.pack(side="bottom", fill="x", pady=(PAD + PAD // 2, 2))
        self.heading.pack(side="top", anchor="w")
        facts.pack(side="top", fill="x", pady=(PAD // 2, 0))
        self.explain.pack(side="top", fill="x", pady=(PAD, 0))

        frame.bind("<Configure>", self.on_editor_resize)
        return frame

    def build_pipeline(self, parent):
        frame = ttk.Frame(parent, padding=(0, PAD, 0, 0))
        self.pipeline_frame = frame
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="Pipeline", style="Head.TLabel").pack(side="left")
        self.step_button = ttk.Button(bar, text="Run next stage",
                                      command=lambda: self.start(one=True))
        self.step_button.pack(side="left", padx=(PAD * 2, 0))
        self.all_button = ttk.Button(bar, text="Run all stages",
                                     command=lambda: self.start(one=False))
        self.all_button.pack(side="left", padx=PAD)
        self.reset_button = ttk.Button(bar, text="Start over", command=self.reset_run)
        self.reset_button.pack(side="left")
        self.offline = tk.BooleanVar(value=False)
        self.offline_box = ttk.Checkbutton(bar, text="Offline (no model call)",
                                           variable=self.offline)
        self.offline_box.pack(side="left", padx=PAD * 2)
        self.run_note = ttk.Label(bar, text="", style="Muted.TLabel")
        self.run_note.pack(side="right")

        body = ttk.Panedwindow(frame, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(PAD // 2, 0))

        left = ttk.Frame(body, padding=(0, 0, PAD, 0))
        self.stages = ttk.Treeview(left, columns=("stage", "what", "result"),
                                   show="headings", selectmode="browse",
                                   height=len(pipeline.STAGES))
        for column, title, width, stretch in (("stage", "Stage", 110, False),
                                              ("what", "What it does", 220, False),
                                              ("result", "Result", 300, True)):
            self.stages.heading(column, text=title, anchor="w")
            self.stages.column(column, width=px(width), stretch=stretch, anchor="w")
        self.stages.tag_configure("running", foreground=BLUE)
        self.stages.tag_configure("done", foreground=GREEN)
        self.stages.tag_configure("stopped", foreground=RED)
        self.stages.pack(fill="x")
        self.stages.bind("<<TreeviewSelect>>", self.on_stage_select)
        self.stage_detail = ttk.Label(
            left, text="Select a stage to see what it does.",
            style="Muted.TLabel", justify="left", anchor="nw", wraplength=px(600))
        self.stage_detail.pack(fill="both", expand=True, pady=(PAD // 2, 0))
        left.bind("<Configure>", lambda e: self.stage_detail.configure(
            wraplength=max(px(200), e.width - PAD)))
        body.add(left, weight=3)

        right = ttk.Frame(body, padding=(PAD, 0, 0, 0))
        ttk.Label(right, text="Log", style="Muted.TLabel").pack(anchor="w")
        box = ttk.Frame(right)
        box.pack(fill="both", expand=True)
        self.log_text = tk.Text(box, height=6, wrap="word", font=MONO, relief="solid",
                                borderwidth=1, padx=6, pady=4, state="disabled",
                                highlightthickness=0)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        body.add(right, weight=2)

        self.reset_stages()
        return frame

    def on_editor_resize(self, event):
        wide = max(px(200), event.width - PAD * 3)
        for label in (self.explain, self.note, self.status_help):
            label.configure(wraplength=wide)
        for label in self.facts.values():
            label.configure(wraplength=max(px(160), wide - px(PREVIEW[0] + 90)))


    def choose_document(self):
        if self.busy or not self.settle_edit():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the main .tex file",
            filetypes=[("LaTeX document", "*.tex"), ("All files", "*.*")])
        if path:
            self.open_document(path)

    def open_document(self, path):
        opened = project.open_document(path)
        if opened["error"]:
            messagebox.showerror("Cannot use this file", opened["error"],
                                 parent=self.root)
            return
        self.document = opened["document"]
        self.current = self.current_key = None
        self.dirty = False
        self.run_state = None
        self.next_stage = 0
        self.reset_stages()
        self.clear_log()

        name = os.path.basename(self.document)
        store = os.path.basename(opened["store"])
        folder = os.path.dirname(self.document)
        if len(folder) > 60:
            folder = "..." + folder[-59:]
        self.root.title(f"Alt text - {name}")
        self.doc_label.configure(text=f"{name}   |   {store}   |   {folder}")
        self.save_button.configure(text=f"Save to {store}")
        if opened["created"]:
            self.log(f"created an empty {store} beside {name}\n")
        else:
            self.log(f"read {store}\n")
        self.reload()
        self.update_controls()

    def reload(self):
        if not self.document or self.busy:
            return
        kept_key = self.current_key
        typed = (self.text.get("1.0", "end-1c"), self.status.get()) if self.dirty else None

        view = project.overview(self.document)
        self.warnings = view["warnings"]
        self.budget = view["budget"]
        self.rows = {}
        self.tree.delete(*self.tree.get_children())
        waiting = 0
        for index, row in enumerate(view["rows"]):
            iid = f"r{index}"
            label, _ = status_of(row)
            shown = row["alt"] if row["alt"] is not None else row.get("previous")
            self.tree.insert("", "end", iid=iid,
                             values=(row["key"], label, one_line(shown)),
                             tags=(TAGS.get(row["outcome"], ""),))
            self.rows[iid] = row
            waiting += row["outcome"] == "describe"

        figures = sum(1 for r in view["rows"] if r["outcome"] != "orphan")
        files = len(view["files"])
        parts = [f"{figures} figure(s)" + (f" in {files} files" if files > 1 else "")]
        if waiting:
            parts.append(f"{waiting} to be described on the next run")
        self.figures_note.configure(text=", ".join(parts))

        if view["error"]:
            self.banner.configure(text=view["error"], foreground=RED, cursor="")
        elif self.warnings:
            self.banner.configure(
                text=f"{len(self.warnings)} warning(s) about this document. "
                     f"Click to show them in the log.", foreground=AMBER, cursor="hand2")
        else:
            self.banner.configure(text="", cursor="")

        target = self.iid_for(kept_key) or next(iter(self.rows), None)
        if target is None:
            self.clear_editor()
        else:
            self.show(target)
            self.tree.selection_set(target)
            self.tree.see(target)
            if typed is not None and self.rows[target]["key"] == kept_key:
                self.set_text(typed[0])
                self.loading = True
                self.status.set(typed[1])
                self.loading = False
                self.update_status_help()
                self.update_counter()
                self.refresh_dirty()
        self.update_controls()

    def iid_for(self, key):
        for iid, row in self.rows.items():
            if row["key"] == key:
                return iid
        return None


    def on_select(self, _event=None):
        chosen = self.tree.selection()
        if not chosen or chosen[0] == self.current or chosen[0] not in self.rows:
            return
        key = self.rows[chosen[0]]["key"]
        if not self.settle_edit():
            if self.current in self.rows:
                self.tree.selection_set(self.current)
            return
        iid = self.iid_for(key)
        if iid is not None:
            self.show(iid)
            self.tree.selection_set(iid)

    def show(self, iid):
        row = self.rows[iid]
        entry = row.get("entry") or {}
        self.current, self.current_key = iid, row["key"]

        self.heading.configure(text=row["key"])
        self.facts["Written in"].configure(text=row.get("where") or "-")
        self.facts["File"].configure(text=row.get("source") or "-")
        self.facts["Profile"].configure(text=row.get("profile") or "-")
        self.facts["Caption"].configure(text=row.get("caption") or "(no caption)")
        label, sentence = status_of(row)
        self.explain.configure(text=f"{label[:1].upper()}{label[1:]}. {sentence}\n"
                                    f"Why: {row['reason']}.")

        if row["outcome"] == "orphan":
            text = entry.get("alt") or ""
        else:
            text = row["alt"] if row["alt"] is not None else (row.get("previous") or "")
        status = entry.get("status")
        if status not in project.STATUSES:
            status = resolver.ARTIFACT if row["outcome"] == resolver.ARTIFACT else "edited"

        self.set_text(text)
        self.loading = True
        self.status.set(status)
        self.loading = False
        self.original = (" ".join(text.split()), status)
        self.dirty = False
        self.note.configure(text="")
        self.update_status_help()
        self.show_preview(row)
        self.update_counter()
        self.update_editor_controls()

    def clear_editor(self):
        self.current = self.current_key = None
        self.heading.configure(text="No figure selected")
        for label in self.facts.values():
            label.configure(text="")
        self.explain.configure(text="")
        self.set_text("")
        self.loading = True
        self.status.set("")
        self.loading = False
        self.original = ("", "")
        self.dirty = False
        self.preview_image = None
        self.preview.configure(image="", text="")
        self.counter.configure(text="")
        self.update_status_help()
        self.update_editor_controls()

    def set_text(self, text):
        self.loading = True
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.loading = False
        self.update_editor_controls()

    def show_preview(self, row):
        self.preview_image = None
        for path in preview_candidates(self.document, row):
            try:
                image = tk.PhotoImage(file=path)
            except tk.TclError:
                continue
            factor = max(1, math.ceil(max(image.width() / px(PREVIEW[0]),
                                          image.height() / px(PREVIEW[1]))))
            if factor > 1:
                image = image.subsample(factor)
            self.preview_image = image
            self.preview.configure(image=image, text="")
            return
        self.preview.configure(image="", text="no preview yet\n(stage 3 renders one)")

    def typed(self):
        return " ".join(self.text.get("1.0", "end-1c").split())

    def on_text_changed(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self.loading:
            return
        self.update_counter()
        if self.typed() != self.original[0] and self.status.get() == "generated":
            self.status.set("edited")
            self.note.configure(text="Status changed to edited, so no run will "
                                     "replace this text.")
        self.refresh_dirty()

    def on_status_changed(self):
        if self.loading:
            return
        self.update_status_help()
        if self.status.get() == resolver.ARTIFACT:
            self.note.configure(text="Decorative figures have no alt text, so the "
                                     "text above is not saved.")
        else:
            self.note.configure(text="")
        self.refresh_dirty()

    def update_status_help(self):
        words = project.STATUSES.get(self.status.get(), "")
        self.status_help.configure(text=f"{words[:1].upper()}{words[1:]}." if words else "")

    def refresh_dirty(self):
        self.dirty = (self.typed(), self.status.get()) != self.original
        key = self.current_key or ""
        self.heading.configure(text=f"{key}   (unsaved)" if self.dirty else key)

    def update_counter(self):
        if self.current is None:
            return
        count = len(self.typed())
        over = count > self.budget
        self.counter.configure(
            text=f"{count} / {self.budget} characters"
                 + ("  (over budget)" if over else ""),
            foreground=RED if over else MUTED)

    def update_editor_controls(self):
        row = self.rows.get(self.current)
        editable = (row is not None and row["outcome"] in project.EDITABLE
                    and not self.busy)
        self.text.configure(state="normal" if editable else "disabled",
                            background="white" if editable else "#f3f4f6")
        for button in self.status_buttons:
            enable(button, editable)
        enable(self.save_button, editable)
        enable(self.remove_button, row is not None and bool(row.get("entry"))
               and not self.busy)
        enable(self.figure_button, row is not None and bool(row.get("file"))
               and os.path.isfile(row["file"]))

    def settle_edit(self):
        if not self.dirty:
            return True
        row = self.rows.get(self.current)
        answer = messagebox.askyesnocancel(
            "Unsaved alt text",
            f"Save your changes to {row['key'] if row else 'this figure'} first?",
            parent=self.root)
        if answer is None:
            return False
        if answer:
            return self.save()
        self.dirty = False
        return True

    def save(self):
        row = self.rows.get(self.current)
        if row is None or self.busy or row["outcome"] not in project.EDITABLE:
            return False
        try:
            result = project.save_entry(self.document, row["key"],
                                        self.text.get("1.0", "end-1c"),
                                        self.status.get(), figure=row)
        except (project.StoreError, ValueError) as exc:
            messagebox.showerror("Not saved", str(exc), parent=self.root)
            return False
        self.dirty = False
        if result["wrote"]:
            store = os.path.basename(project.store_path(self.document))
            self.log(f"[you] {row['key']}: saved as {result['entry']['status']} "
                     f"in {store}\n")
        self.reload()
        self.note.configure(text=result["note"][:1].upper() + result["note"][1:] + ".")
        return True

    def remove(self):
        row = self.rows.get(self.current)
        if row is None or self.busy or not row.get("entry"):
            return
        store = os.path.basename(project.store_path(self.document))
        if not messagebox.askyesno(
                "Remove entry",
                f"Remove the entry for {row['key']} from {store}?\n\nIf the "
                f"figure is still in the document, the next run will describe "
                f"it again.", parent=self.root):
            return
        try:
            project.remove_entry(self.document, row["key"])
        except project.StoreError as exc:
            messagebox.showerror("Not removed", str(exc), parent=self.root)
            return
        self.dirty = False
        self.log(f"[you] {row['key']}: removed from {store}\n")
        self.reload()


    def reset_stages(self):
        self.stages.delete(*self.stages.get_children())
        self.results = {}
        for name, _ in pipeline.STAGES:
            self.stages.insert("", "end", iid=name, values=(
                f"{MARKS['waiting']}  {name}", STAGE_INFO.get(name, ("", ""))[0], ""))

    def mark_stage(self, name, mark, result=""):
        self.stages.item(name, tags=(mark,), values=(
            f"{MARKS[mark]}  {name}", STAGE_INFO.get(name, ("", ""))[0], result))
        self.results[name] = result

    def on_stage_select(self, _event=None):
        chosen = self.stages.selection()
        if not chosen:
            return
        name = chosen[0]
        stem = (os.path.splitext(os.path.basename(self.document))[0]
                if self.document else "main")
        detail = STAGE_INFO.get(name, ("", ""))[1].format(stem=stem)
        result = self.results.get(name)
        self.stage_detail.configure(
            text=f"{name}: {detail}" + (f"\nLast result: {result}" if result else ""))

    def run_over(self):
        return bool(self.run_state.get("stop")) or self.next_stage >= len(pipeline.STAGES)

    def start(self, one):
        if self.busy or not self.document or not self.settle_edit():
            return
        if self.run_state is None or self.run_over():
            self.new_run()
        self.busy = True
        self.update_controls()
        threading.Thread(target=self.work, args=(self.run_state, one),
                         daemon=True).start()

    def new_run(self):
        self.run_state = {"document": self.document, "assume_yes": False,
                          "verbose": True, "figures": [], "ask": self.ask_from_worker}
        self.next_stage = 0
        self.reset_stages()
        self.clear_log()
        self.log(f"new run on {os.path.basename(self.document)}\n")
        if self.offline.get():
            self.run_state["provider"] = "stub"
            self.log("[note] offline: stage 4 writes placeholder text instead of "
                     "calling the model.\n")

    def reset_run(self):
        if self.busy:
            return
        self.run_state = None
        self.next_stage = 0
        self.reset_stages()
        self.clear_log()
        self.update_controls()

    def work(self, state, one):
        out = QueueWriter(self.events)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                while self.next_stage < len(pipeline.STAGES):
                    name, stage = pipeline.STAGES[self.next_stage]
                    self.events.put(("started", name))
                    summary = pipeline.run_stage(state, name, stage)
                    self.next_stage += 1
                    self.events.put(("finished", name, summary,
                                     bool(state.get("stop")), state.get("crashed")))
                    if state.get("stop") or one:
                        break
        finally:
            self.events.put(("idle",))

    def pump(self):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self.pump)

    def handle(self, event):
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "started":
            self.mark_stage(event[1], "running", "running...")
            self.run_note.configure(text=f"running {event[1]}...")
        elif kind == "finished":
            _, name, summary, stopped, crashed = event
            if summary is None:
                self.mark_stage(name, "stopped",
                                f"crashed: {crashed['error']}" if crashed else "crashed")
            else:
                self.mark_stage(name, "stopped" if stopped else "done", summary)
            self.stages.selection_set(name)
        elif kind == "ask":
            self.ask_consent(*event[1:])
        elif kind == "idle":
            self.busy = False
            self.reload()
            self.update_controls()

    def ask_from_worker(self, question, details):
        reply = {"yes": False}
        answered = threading.Event()
        self.events.put(("ask", details, reply, answered))
        answered.wait()
        return reply["yes"]

    def ask_consent(self, details, reply, answered):
        name = os.path.basename(self.document)
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Change {name}?")
        dialog.transient(self.root)
        body = ttk.Frame(dialog, padding=PAD * 2)
        body.pack(fill="both", expand=True)
        ttk.Label(body, justify="left", wraplength=px(640),
                  text=f"{name} is missing lines the later stages need. Nothing has "
                       f"been changed yet. These lines would be added:"
                  ).pack(anchor="w")
        preview = textwrap.dedent(details).strip("\n")
        box = tk.Text(body, height=min(22, preview.count("\n") + 2), width=96,
                      font=MONO, wrap="none", relief="solid", borderwidth=1,
                      padx=6, pady=4, highlightthickness=0)
        box.insert("1.0", preview)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, pady=PAD)
        ttk.Label(body, style="Muted.TLabel",
                  text=f"A backup is saved as a .bak file beside {name}. Without "
                       f"these changes the run stops here.").pack(anchor="w")

        def finish(yes):
            reply["yes"] = yes
            dialog.grab_release()
            dialog.destroy()
            answered.set()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(PAD * 2, 0))
        ttk.Button(buttons, text=f"Add them to {name}",
                   command=lambda: finish(True)).pack(side="right")
        ttk.Button(buttons, text="Cancel",
                   command=lambda: finish(False)).pack(side="right", padx=PAD)
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()


    def update_controls(self):
        has_doc = self.document is not None
        idle = not self.busy
        mid_run = self.run_state is not None and not self.run_over()
        enable(self.open_button, idle)
        enable(self.reload_button, has_doc and idle)
        enable(self.store_button, has_doc)
        enable(self.pdf_button, has_doc and os.path.isfile(project.pdf_path(self.document)))
        enable(self.step_button, has_doc and idle)
        enable(self.all_button, has_doc and idle)
        enable(self.reset_button, idle and self.run_state is not None)
        enable(self.offline_box, idle and not mid_run)
        self.update_editor_controls()

        if self.busy:
            return
        total = len(pipeline.STAGES)
        if not has_doc:
            note = ""
        elif self.run_state is None:
            note = f"not run yet ({total} stages)"
        elif self.run_state.get("crashed"):
            note = (f"crashed in {self.run_state['crashed']['stage']}; "
                    f"running again starts over")
        elif self.run_state.get("stop"):
            note = (f"stopped after {pipeline.STAGES[self.next_stage - 1][0]}; "
                    f"running again starts over")
        elif self.next_stage >= total:
            note = "all stages finished; running again starts over"
        else:
            note = (f"next: {pipeline.STAGES[self.next_stage][0]} "
                    f"({self.next_stage + 1} of {total})")
        self.run_note.configure(text=note)

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def log_warnings(self):
        if not self.warnings:
            return
        self.log(f"\n{len(self.warnings)} warning(s) about "
                 f"{os.path.basename(self.document)}:\n"
                 + "".join(f"  - {w}\n" for w in self.warnings))

    def open_with_system(self, path):
        try:
            open_path(path)
        except OSError as exc:
            messagebox.showerror("Cannot open", f"{path}\n\n{exc}", parent=self.root)

    def open_figure(self):
        row = self.rows.get(self.current)
        if row and row.get("file"):
            self.open_with_system(row["file"])

    def open_store(self):
        if self.document:
            self.open_with_system(project.store_path(self.document))

    def open_pdf(self):
        if self.document:
            self.open_with_system(project.pdf_path(self.document))

    def close(self):
        if self.busy:
            if not messagebox.askokcancel(
                    "A stage is running",
                    "A stage is still running. Close the window anyway?",
                    parent=self.root):
                return
        elif not self.settle_edit():
            return
        self.root.destroy()


def main(argv=None):
    global SCALE, PAD
    argv = sys.argv[1:] if argv is None else argv
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    PAD = px(8)
    App(root, argv[0] if argv else None)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

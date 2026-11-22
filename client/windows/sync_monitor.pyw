"""Setuora Sync Monitor - desktop window showing Lite <-> Master <-> Tally sync.

Read-only. It reads the status file Master writes every few seconds and checks
that Master and the Tally gateway answer. It never writes to Master or Tally.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from tkinter import ttk
from urllib.error import URLError
from urllib.request import urlopen

INSTALL = Path(os.environ.get("SETUORA_MASTER_DIR", r"C:\ProgramData\Setuora\Setuora-Master"))
STATUS_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else INSTALL / "status" / "sync-status.json"
MASTER_URL = os.environ.get("SETUORA_MASTER_URL", "http://127.0.0.1:8000")
TALLY_URL = os.environ.get("SETUORA_TALLY_URL", "http://127.0.0.1:9000")
REFRESH_MS = 15_000
PROBE_EVERY = 4  # check Master and Tally every 4th refresh (about a minute)

COMMAND_NAMES = {
    "PRODUCT_UPSERT": "Products",
    "QR_ALLOCATED": "QR codes",
    "TALLY_RECEIPTS": "Tally receipts",
}
STOCK_NAMES = {
    "AWAITING_STOCK_IN": "Issued, not yet booked in on Lite",
    "IN_STOCK": "In stock (booked in on Lite)",
    "SOLD": "Sold",
    "IN_TRANSIT": "In transit",
    "DAMAGED": "Damaged",
    "MISSING": "Missing",
    "PURCHASE_RETURN": "Returned to supplier",
    "GENERATED": "QR made, not issued",
}
GOOD, WAIT, BAD, MUTED = "#1f7a3d", "#a86400", "#b3261e", "#6b6b6b"


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 90:
        return f"{max(seconds, 0)} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return then.astimezone().strftime("%d %b %Y %H:%M")


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        value = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%d-%m-%Y %H:%M")


def _probe(url: str) -> bool:
    try:
        with urlopen(url, timeout=4) as response:  # noqa: S310 - fixed local URLs
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


class Monitor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Setuora Sync Monitor")
        self.geometry("1060x700")
        self.minsize(820, 520)
        self.configure(bg="#faf8f4")
        self.ticks = 0
        self.master_ok: bool | None = None
        self.tally_ok: bool | None = None
        self._style()
        self._build()
        self.refresh()

    # ---------- layout ----------
    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#faf8f4")
        style.configure("Head.TLabel", font=("Segoe UI Semibold", 16))
        style.configure("Card.TFrame", background="#f1ebe1", relief="flat")
        style.configure("CardTitle.TLabel", background="#f1ebe1", font=("Segoe UI", 9), foreground=MUTED)
        style.configure("CardValue.TLabel", background="#f1ebe1", font=("Segoe UI Semibold", 15))
        style.configure("CardNote.TLabel", background="#f1ebe1", font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("TNotebook.Tab", padding=(14, 6))

    def _build(self) -> None:
        top = ttk.Frame(self, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Setuora Sync Monitor", style="Head.TLabel").pack(side="left")
        ttk.Button(top, text="Open Master", command=lambda: webbrowser.open(MASTER_URL)).pack(side="right")
        ttk.Button(
            top, text="Tally queue", command=lambda: webbrowser.open(f"{MASTER_URL}/network/tally")
        ).pack(side="right", padx=6)
        ttk.Button(top, text="Refresh now", command=self.refresh).pack(side="right")
        self.on_top = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="Keep on top", variable=self.on_top,
            command=lambda: self.attributes("-topmost", self.on_top.get()),
        ).pack(side="right", padx=10)

        self.status_line = ttk.Label(self, padding=(16, 0, 16, 6), foreground=MUTED)
        self.status_line.pack(fill="x")

        self.cards = ttk.Frame(self, padding=(16, 4))
        self.cards.pack(fill="x")
        self.card_widgets: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        for column, (key, title) in enumerate(
            [
                ("tally_in", "Tally \u2192 Master"),
                ("lite_out", "Master \u2192 Lite"),
                ("lite_in", "Lite \u2192 Master"),
                ("tally_out", "Master \u2192 Tally"),
            ]
        ):
            frame = ttk.Frame(self.cards, style="Card.TFrame", padding=12)
            frame.grid(row=0, column=column, sticky="nsew", padx=4)
            self.cards.columnconfigure(column, weight=1)
            ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor="w")
            value = ttk.Label(frame, text="-", style="CardValue.TLabel")
            value.pack(anchor="w")
            note = ttk.Label(frame, text="", style="CardNote.TLabel", wraplength=220, justify="left")
            note.pack(anchor="w")
            self.card_widgets[key] = (value, note)

        self.tabs = ttk.Notebook(self, padding=(16, 8, 16, 12))
        self.tabs.pack(fill="both", expand=True)
        self.franchise_tree = self._tree(
            "Franchises",
            ("item", "value", "state"),
            ("What", "Count / time", "State"),
            (330, 330, 220),
        )
        self.waiting_tree = self._tree(
            "Waiting for Tally",
            ("franchise", "batch", "type", "party", "status", "retries", "created", "error"),
            ("Franchise", "Batch", "Type", "Party", "Status", "Tries", "Created", "Reason"),
            (80, 130, 100, 150, 120, 50, 120, 300),
        )
        self.recent_tree = self._tree(
            "Posted to Tally",
            ("franchise", "batch", "type", "party", "voucher", "synced"),
            ("Franchise", "Batch", "Type", "Party", "Tally voucher", "Synced"),
            (80, 150, 110, 220, 200, 140),
        )
        for tree in (self.franchise_tree, self.waiting_tree, self.recent_tree):
            tree.tag_configure("good", foreground=GOOD)
            tree.tag_configure("wait", foreground=WAIT)
            tree.tag_configure("bad", foreground=BAD)
            tree.tag_configure("head", font=("Segoe UI Semibold", 10), background="#f1ebe1")

    def _tree(self, title, columns, headings, widths) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, heading, width in zip(columns, headings, widths, strict=True):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w", stretch=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return tree

    # ---------- data ----------
    def refresh(self) -> None:
        if self.ticks % PROBE_EVERY == 0:
            threading.Thread(target=self._probe_services, daemon=True).start()
        self.ticks += 1
        self._render(self._load())
        self.after(REFRESH_MS, self.refresh)

    def _probe_services(self) -> None:
        master = _probe(f"{MASTER_URL}/health")
        tally = _probe(TALLY_URL)
        self.after(0, self._set_probes, master, tally)

    def _set_probes(self, master: bool, tally: bool) -> None:
        self.master_ok, self.tally_ok = master, tally
        self._render(self._load())

    def _load(self) -> dict | None:
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _render(self, data: dict | None) -> None:
        def flag(ok: bool | None, name: str) -> str:
            return f"{name}: checking" if ok is None else f"{name}: {'answering' if ok else 'NOT answering'}"

        parts = [flag(self.master_ok, "Master"), flag(self.tally_ok, "Tally gateway")]
        if data is None:
            parts.append(f"No status file yet at {STATUS_FILE} (Master writes it every 15 s once updated).")
            self.status_line.configure(text="   \u00b7   ".join(parts), foreground=BAD)
            return
        parts.append(f"Status updated {_ago(data.get('generated_at'))}")
        parts.append("Tally posting ON" if data.get("tally_enabled") else "Tally posting OFF")
        stale = self.master_ok is False or self.tally_ok is False
        self.status_line.configure(text="   \u00b7   ".join(parts), foreground=BAD if stale else MUTED)

        franchises = data.get("franchises", [])
        receipts = sum(f["from_tally"]["receipts"] for f in franchises)
        waiting_lite = sum(c["waiting"] for f in franchises for c in f["to_lite"].values())
        delivered = sum(c["delivered"] for f in franchises for c in f["to_lite"].values())
        events = sum(e["count"] for f in franchises for e in f["from_lite"].values())
        last_event = max((e["last"] or "" for f in franchises for e in f["from_lite"].values()), default="")
        tally_status: dict[str, int] = {}
        for f in franchises:
            for status, count in f["to_tally"]["by_status"].items():
                tally_status[status] = tally_status.get(status, 0) + count
        tally_wait = sum(tally_status.get(s, 0) for s in ("SUBMITTED", "PENDING_SYNC", "SYNCING"))
        tally_bad = sum(tally_status.get(s, 0) for s in ("FAILED", "REVIEW_REQUIRED"))

        self._card("tally_in", f"{receipts} receipts",
                   f"Last receipt sync {_ago(data['receipt_sync'].get('last_at'))}", GOOD)
        self._card("lite_out", f"{waiting_lite} waiting" if waiting_lite else "All delivered",
                   f"{delivered} batches delivered to Lite", WAIT if waiting_lite else GOOD)
        self._card("lite_in", f"{events} events",
                   f"Last from Lite {_ago(last_event or None)}", GOOD if events else MUTED)
        self._card(
            "tally_out",
            f"{tally_bad} need attention" if tally_bad else (f"{tally_wait} waiting" if tally_wait else "Up to date"),
            f"{tally_status.get('SYNCED', 0)} posted \u00b7 {tally_wait} waiting \u00b7 {tally_bad} failed/review",
            BAD if tally_bad else (WAIT if tally_wait else GOOD),
        )

        self._fill_franchises(franchises)
        self._fill(self.waiting_tree, [
            ((b["franchise"] or "-", b["batch"], b["type"], b["party"] or "", b["status"], b["retries"],
              _local(b["created_at"]), b["error"] or ""),
             "bad" if b["status"] in ("FAILED", "REVIEW_REQUIRED") else "wait")
            for b in data.get("tally_waiting", [])
        ], empty="Nothing is waiting for Tally.")
        self._fill(self.recent_tree, [
            ((b["franchise"] or "-", b["batch"], b["type"], b["party"] or "",
              f"{b['voucher_type'] or ''} {b['tally_voucher'] or ''}".strip(), _local(b["synced_at"])), "good")
            for b in data.get("tally_recent", [])
        ], empty="Nothing has been posted to Tally yet.")
        waiting_count = len(data.get("tally_waiting", []))
        self.tabs.tab(1, text=f"Waiting for Tally ({waiting_count})")

    def _card(self, key: str, value: str, note: str, colour: str) -> None:
        value_label, note_label = self.card_widgets[key]
        value_label.configure(text=value, foreground=colour)
        note_label.configure(text=note)

    def _fill(self, tree: ttk.Treeview, rows, empty: str) -> None:
        tree.delete(*tree.get_children())
        if not rows:
            tree.insert("", "end", values=(empty,), tags=("good",))
        for values, tag in rows:
            tree.insert("", "end", values=values, tags=(tag,))

    def _fill_franchises(self, franchises: list[dict]) -> None:
        tree = self.franchise_tree
        tree.delete(*tree.get_children())
        for f in franchises:
            online = "Online" if f["online"] else "Offline"
            tree.insert("", "end", values=(f"{f['code']}  {f['name']}", f"Godown: {f['godown'] or 'not set'}",
                                            f"{online}, seen {_ago(f['last_seen_at'])}"),
                        tags=("head",))
            vt = f["voucher_types"]
            tree.insert("", "end", values=("   Tally voucher types",
                                            f"Sale: {vt['sale'] or '-'} | Return: {vt['sales_return'] or '-'}",
                                            f"Receipt: {vt['receipt'] or '-'}"),
                        tags=("good" if vt["sale"] and vt["receipt"] else "bad",))
            tree.insert("", "end", values=("   MASTER \u2192 LITE", "", ""), tags=("head",))
            for command_type, c in sorted(f["to_lite"].items()):
                state = f"{c['waiting']} waiting since {_ago(c['oldest_waiting'])}" if c["waiting"] else "all delivered"
                tree.insert("", "end", values=(f"      {COMMAND_NAMES.get(command_type, command_type)}",
                                                f"{c['delivered']} delivered", state),
                            tags=("wait" if c["waiting"] else "good",))
            tree.insert("", "end", values=("   STOCK AT FRANCHISE", f"{sum(f['stock'].values())} QR codes", ""),
                        tags=("head",))
            for status, count in sorted(f["stock"].items(), key=lambda kv: -kv[1]):
                tree.insert("", "end", values=(f"      {STOCK_NAMES.get(status, status)}", count, ""),
                            tags=("wait" if status == "AWAITING_STOCK_IN" else "good",))
            tree.insert("", "end", values=("   LITE \u2192 MASTER", f"last sequence {f['last_sequence']}", ""),
                        tags=("head",))
            if not f["from_lite"]:
                tree.insert("", "end", values=("      No sales or stock events yet", "", ""), tags=("wait",))
            for event_type, e in sorted(f["from_lite"].items()):
                tree.insert("", "end", values=(f"      {event_type.replace('_', ' ').title()}", e["count"],
                                                f"last {_ago(e['last'])}"), tags=("good",))
            tally = f["to_tally"]["by_status"]
            tree.insert("", "end", values=("   MASTER \u2192 TALLY", f"last posted {_ago(f['to_tally']['last_synced_at'])}",
                                            ""), tags=("head",))
            if not tally:
                tree.insert("", "end", values=("      No vouchers yet", "", ""), tags=("good",))
            for status, count in sorted(tally.items()):
                tag = "bad" if status in ("FAILED", "REVIEW_REQUIRED") else ("good" if status in ("SYNCED", "CLOSED") else "wait")
                tree.insert("", "end", values=(f"      {status.replace('_', ' ').title()}", count, ""), tags=(tag,))
            r = f["from_tally"]
            tree.insert("", "end", values=("   TALLY \u2192 MASTER", "", ""), tags=("head",))
            tree.insert("", "end", values=("      Receipts", f"{r['receipts']} (Rs {r['receipts_total']:,.2f})",
                                            f"latest dated {r['last_receipt_date'] or '-'}"), tags=("good",))


if __name__ == "__main__":
    Monitor().mainloop()

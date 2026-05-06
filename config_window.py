"""
Configuration window for Claude Usage Widget.
Two tabs: Accounts (add/edit/remove) and Notifications (thresholds + burn rate).
On save, writes config.json and calls back so the app reloads live.
"""

import gi
gi.require_version('Gtk', '3.0')

import json
import os
from pathlib import Path

from gi.repository import Gtk, Gdk

CONFIG_DIR = Path.home() / ".config" / "claude-usage-widget"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_THRESHOLDS = {"warn": 60, "critical": 85}
DEFAULT_BURN_RATE = {"enabled": False, "multiplier": 1.5}


class ConfigWindow(Gtk.Window):
    """Dialog for editing accounts and notification settings."""

    def __init__(self, current_accounts: list[dict], thresholds: dict,
                 burn_rate_cfg: dict, poll_interval_secs: int, on_save):
        super().__init__(title="Configure")
        self.set_default_size(540, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self._on_save_cb = on_save
        self._rows: list[tuple[Gtk.Entry, Gtk.Entry, Gtk.CheckButton, Gtk.CheckButton]] = []

        self._apply_css()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        notebook = Gtk.Notebook()
        notebook.set_margin_top(8)
        notebook.set_margin_start(4)
        notebook.set_margin_end(4)
        notebook.append_page(
            self._build_accounts_tab(current_accounts),
            Gtk.Label(label="Accounts"),
        )
        notebook.append_page(
            self._build_notifications_tab(thresholds, burn_rate_cfg, poll_interval_secs),
            Gtk.Label(label="Notifications"),
        )
        outer.pack_start(notebook, True, True, 0)

        # Footer (shared across tabs)
        sep = Gtk.Separator()
        sep.get_style_context().add_class("cfg-sep")
        outer.pack_start(sep, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(10)
        footer.set_margin_bottom(12)
        footer.set_margin_start(20)
        footer.set_margin_end(20)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.destroy())
        save_btn = Gtk.Button(label="Save & Refresh")
        save_btn.connect("clicked", self._on_save)
        footer.pack_end(save_btn, False, False, 0)
        footer.pack_end(cancel_btn, False, False, 0)
        outer.pack_start(footer, False, False, 0)

        self.add(outer)
        self.show_all()

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_accounts_tab(self, current_accounts: list[dict]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(8)
        box.set_margin_start(20)
        box.set_margin_end(20)

        # Column headers
        hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for text, expand in [("Label", False), ("Credentials Directory", True)]:
            h = Gtk.Label(label=text)
            h.get_style_context().add_class("cfg-col-header")
            h.set_halign(Gtk.Align.START)
            hdr_row.pack_start(h, expand, expand, 0)
        box.pack_start(hdr_row, False, False, 0)

        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(self._rows_box, False, False, 0)

        for acct in current_accounts:
            self._add_row(acct.get("label", ""), acct.get("credentials_dir", ""),
                          acct.get("hide_from_tray", False), acct.get("disable_polling", False))

        add_btn = Gtk.Button(label="+ Add Account")
        add_btn.get_style_context().add_class("cfg-add-btn")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", lambda _: self._add_row("", "~/.claude", False, False))
        box.pack_start(add_btn, False, False, 4)

        return box

    def _build_notifications_tab(self, thresholds: dict,
                                  burn_rate_cfg: dict,
                                  poll_interval_secs: int) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(16)
        box.set_margin_bottom(8)
        box.set_margin_start(20)
        box.set_margin_end(20)

        # ── Polling interval ─────────────────────────────────────────────────
        poll_lbl = Gtk.Label(label="POLLING")
        poll_lbl.get_style_context().add_class("cfg-section-header")
        poll_lbl.set_halign(Gtk.Align.START)
        box.pack_start(poll_lbl, False, False, 0)

        poll_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        poll_field_lbl = Gtk.Label(label="Check every")
        poll_field_lbl.get_style_context().add_class("cfg-field-label")
        self._poll_spin = Gtk.SpinButton.new_with_range(1, 60, 1)
        self._poll_spin.set_value(max(1, poll_interval_secs // 60))
        poll_suffix = Gtk.Label(label="minutes")
        poll_suffix.get_style_context().add_class("cfg-field-label")
        poll_row.pack_start(poll_field_lbl, False, False, 0)
        poll_row.pack_start(self._poll_spin, False, False, 0)
        poll_row.pack_start(poll_suffix, False, False, 0)
        box.pack_start(poll_row, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)

        # ── Usage thresholds ──────────────────────────────────────────────────
        thresh_lbl = Gtk.Label(label="USAGE THRESHOLDS")
        thresh_lbl.get_style_context().add_class("cfg-section-header")
        thresh_lbl.set_halign(Gtk.Align.START)
        box.pack_start(thresh_lbl, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(8)

        warn_label = Gtk.Label(label="Warn at")
        warn_label.get_style_context().add_class("cfg-field-label")
        warn_label.set_halign(Gtk.Align.START)
        self._warn_spin = Gtk.SpinButton.new_with_range(0, 100, 1)
        self._warn_spin.set_value(thresholds.get("warn", DEFAULT_THRESHOLDS["warn"]))
        warn_suffix = Gtk.Label(label="%")
        warn_suffix.get_style_context().add_class("cfg-field-label")

        crit_label = Gtk.Label(label="Critical at")
        crit_label.get_style_context().add_class("cfg-field-label")
        crit_label.set_halign(Gtk.Align.START)
        self._crit_spin = Gtk.SpinButton.new_with_range(0, 100, 1)
        self._crit_spin.set_value(thresholds.get("critical", DEFAULT_THRESHOLDS["critical"]))
        crit_suffix = Gtk.Label(label="%")
        crit_suffix.get_style_context().add_class("cfg-field-label")

        grid.attach(warn_label,       0, 0, 1, 1)
        grid.attach(self._warn_spin,  1, 0, 1, 1)
        grid.attach(warn_suffix,      2, 0, 1, 1)
        grid.attach(crit_label,       0, 1, 1, 1)
        grid.attach(self._crit_spin,  1, 1, 1, 1)
        grid.attach(crit_suffix,      2, 1, 1, 1)
        box.pack_start(grid, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)

        # ── Burn rate alert ───────────────────────────────────────────────────
        burn_lbl = Gtk.Label(label="BURN RATE ALERT  (7-day window)")
        burn_lbl.get_style_context().add_class("cfg-section-header")
        burn_lbl.set_halign(Gtk.Align.START)
        box.pack_start(burn_lbl, False, False, 0)

        desc = Gtk.Label(
            label="Warns when your usage rate suggests you'll exceed\n"
                  "your weekly limit — e.g. 50% used with only 25% of\n"
                  "the week elapsed."
        )
        desc.get_style_context().add_class("cfg-desc")
        desc.set_halign(Gtk.Align.START)
        box.pack_start(desc, False, False, 0)

        self._burn_check = Gtk.CheckButton(label="Enable burn rate warnings")
        self._burn_check.set_active(burn_rate_cfg.get("enabled", False))
        box.pack_start(self._burn_check, False, False, 0)

        mult_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mult_lbl = Gtk.Label(label="Alert when on pace for")
        mult_lbl.get_style_context().add_class("cfg-field-label")
        self._mult_spin = Gtk.SpinButton.new_with_range(1.0, 5.0, 0.1)
        self._mult_spin.set_digits(1)
        self._mult_spin.set_value(burn_rate_cfg.get("multiplier", DEFAULT_BURN_RATE["multiplier"]))
        mult_suffix = Gtk.Label(label="× your weekly allocation")
        mult_suffix.get_style_context().add_class("cfg-field-label")
        mult_row.pack_start(mult_lbl, False, False, 0)
        mult_row.pack_start(self._mult_spin, False, False, 0)
        mult_row.pack_start(mult_suffix, False, False, 0)

        # Dim the multiplier row when burn rate is disabled
        def on_burn_toggle(btn):
            mult_row.set_sensitive(btn.get_active())

        self._burn_check.connect("toggled", on_burn_toggle)
        mult_row.set_sensitive(self._burn_check.get_active())
        box.pack_start(mult_row, False, False, 0)

        return box

    # ── Account row helpers ───────────────────────────────────────────────────

    def _add_row(self, label: str, cred_dir: str,
                hide_from_tray: bool = False, disable_polling: bool = False):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        label_entry = Gtk.Entry()
        label_entry.set_text(label)
        label_entry.set_width_chars(10)
        label_entry.set_max_width_chars(12)
        label_entry.set_placeholder_text("Name")

        dir_entry = Gtk.Entry()
        dir_entry.set_text(cred_dir)
        dir_entry.set_placeholder_text("~/.claude")

        hide_check = Gtk.CheckButton(label="Hide tray")
        hide_check.set_active(hide_from_tray)
        hide_check.set_tooltip_text("Hide this account from the tray label")

        disable_check = Gtk.CheckButton(label="No poll")
        disable_check.set_active(disable_polling)
        disable_check.set_tooltip_text("Disable background auto-refresh for this account")

        remove_btn = Gtk.Button(label="✕")
        remove_btn.get_style_context().add_class("cfg-remove-btn")
        remove_btn.set_relief(Gtk.ReliefStyle.NONE)

        row.pack_start(label_entry, False, False, 0)
        row.pack_start(dir_entry, True, True, 0)
        row.pack_start(hide_check, False, False, 0)
        row.pack_start(disable_check, False, False, 0)
        row.pack_start(remove_btn, False, False, 0)

        entry_quad = (label_entry, dir_entry, hide_check, disable_check)
        self._rows.append(entry_quad)
        self._rows_box.pack_start(row, False, False, 0)
        row.show_all()

        def on_remove(_btn, r=row, eq=entry_quad):
            self._rows_box.remove(r)
            self._rows.remove(eq)

        remove_btn.connect("clicked", on_remove)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _on_save(self, _btn):
        accounts = [
            {
                "label": le.get_text().strip(),
                "credentials_dir": de.get_text().strip(),
                "hide_from_tray": hc.get_active(),
                "disable_polling": dc.get_active(),
            }
            for le, de, hc, dc in self._rows
            if le.get_text().strip() and de.get_text().strip()
        ]

        if not accounts:
            dlg = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="At least one account is required.",
            )
            dlg.run()
            dlg.destroy()
            return

        labels = [a["label"] for a in accounts]
        if len(labels) != len(set(labels)):
            dlg = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Account labels must be unique.",
            )
            dlg.run()
            dlg.destroy()
            return

        warn = int(self._warn_spin.get_value())
        crit = int(self._crit_spin.get_value())
        if warn >= crit:
            dlg = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Warn threshold must be lower than Critical threshold.",
            )
            dlg.run()
            dlg.destroy()
            return

        new_config = {
            "accounts": accounts,
            "poll_interval_seconds": int(self._poll_spin.get_value()) * 60,
            "thresholds": {"warn": warn, "critical": crit},
            "burn_rate": {
                "enabled": self._burn_check.get_active(),
                "multiplier": round(self._mult_spin.get_value(), 1),
            },
        }

        try:
            existing = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
        except (json.JSONDecodeError, OSError):
            existing = {}

        existing.update(new_config)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(existing, indent=2))
        os.chmod(CONFIG_FILE, 0o600)

        self.destroy()
        self._on_save_cb(new_config)

    # ── CSS ───────────────────────────────────────────────────────────────────

    def _apply_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            /* Base */
            window, box, grid { background-color: #1a1a2e; }

            /* Notebook tabs */
            notebook > header {
                background-color: #12122a;
                border-bottom: 1px solid #2a2a4a;
                padding: 0;
            }
            notebook > header > tabs > tab {
                background-color: #12122a;
                color: #6b7280;
                padding: 6px 18px;
                border: none;
                box-shadow: none;
                outline: none;
            }
            notebook > header > tabs > tab:hover {
                background-color: #1e1e38;
                color: #c0c0ff;
            }
            notebook > header > tabs > tab:checked {
                background-color: #1a1a2e;
                color: #e0e0ff;
                font-weight: bold;
                border-bottom: 2px solid #7070cc;
            }
            /* Page content area */
            notebook > stack {
                background-color: #1a1a2e;
                border: none;
            }

            /* Fields */
            .cfg-section-header { color: #7070aa; font-size: 10px; font-weight: bold; letter-spacing: 1px; }
            .cfg-col-header { color: #6b7280; font-size: 11px; font-weight: bold; }
            .cfg-field-label { color: #a0a0c0; font-size: 12px; }
            .cfg-desc { color: #555577; font-size: 11px; }

            /* Inputs */
            entry {
                background-color: #20203a;
                color: #e0e0ff;
                border: 1px solid #3a3a5a;
                border-radius: 4px;
                padding: 4px 8px;
                caret-color: #e0e0ff;
            }
            entry:focus { border-color: #7070cc; }

            spinbutton entry { border: none; padding: 4px 6px; }
            spinbutton {
                background-color: #20203a;
                color: #e0e0ff;
                border: 1px solid #3a3a5a;
                border-radius: 4px;
            }
            spinbutton button {
                background-color: #2a2a4a;
                color: #a0a0c0;
                border: none;
                border-left: 1px solid #3a3a5a;
                border-radius: 0;
                padding: 2px 6px;
                min-width: 0;
            }
            spinbutton button:hover { background-color: #3a3a6a; color: #e0e0ff; }

            checkbutton { color: #c0c0ff; background-color: transparent; }
            checkbutton check {
                background-color: #20203a;
                border: 1px solid #3a3a5a;
                border-radius: 3px;
            }
            checkbutton check:checked { background-color: #5050aa; border-color: #7070cc; }

            /* Buttons */
            button {
                color: #8888aa;
                background-color: transparent;
                border: 1px solid #3a3a5a;
                border-radius: 4px;
                padding: 4px 12px;
            }
            button:hover { color: #e0e0ff; border-color: #6060aa; }

            .cfg-remove-btn {
                color: #ef4444; background: transparent;
                border: none; padding: 2px 6px;
            }
            .cfg-remove-btn:hover { color: #ff7070; }

            .cfg-add-btn {
                color: #7070aa; background: transparent;
                border: 1px solid #3a3a5a; border-radius: 4px; padding: 4px 12px;
            }
            .cfg-add-btn:hover { color: #c0c0ff; border-color: #6060aa; }

            .cfg-save-btn {
                background-color: #4545b0;
                color: #ffffff;
                border: 1px solid #7575cc;
                border-radius: 4px;
                padding: 4px 16px;
                font-weight: bold;
            }
            .cfg-save-btn:hover { background-color: #5a5acc; color: #ffffff; border-color: #9090dd; }

            /* Misc */
            separator, .cfg-sep { background-color: #2a2a4a; min-height: 1px; }
            label { background-color: transparent; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

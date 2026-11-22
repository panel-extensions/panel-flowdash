"""The Report view's Export button and dialog: pick a format, then the browser saves the file."""

from __future__ import annotations

import io
import logging
import typing as t

import panel as pn
import panel_material_ui as pmui
import param

from panel_flowdash.ui.launcher import build_downloader
from panel_flowdash.util import escape_closes

if t.TYPE_CHECKING:
    from panel_flowdash.export import ExportFile

logger = logging.getLogger(__name__)

EXPORT_NOTE = (
    "The file holds the report's values as they are now. Anyone you send it to can read "
    "them, and it cannot be taken back."
)
PENDING_LABEL = "Include questions not answered yet"


class ReportExport(param.Parameterized):
    """The Export button of the Report view, its dialog and the download it starts."""

    editor = param.Parameter(doc="The FlowDash editor whose report is exported.")

    exporting = param.Boolean(default=False, doc="Whether an export is being built.")

    def __init__(self, **params):
        from panel_flowdash.export import FORMATS

        super().__init__(**params)
        self.button = pmui.Button(
            label="Export",
            icon="download",
            variant="text",
            size="small",
            margin=0,
            align="center",
            css_classes=["fd-report-export"],
        )
        self.button.on_click(lambda _event: self.open())
        self._format = pmui.RadioBoxGroup(
            label="Format",
            options={f.label: f.name for f in FORMATS.values()},
            value="html",
            inline=False,
            margin=(0, 0, 8, 0),
            css_classes=["fd-report-export-format"],
        )
        self._pending = pmui.Switch(
            label=PENDING_LABEL,
            value=False,
            margin=(0, 0, 8, 0),
            css_classes=["fd-report-export-pending"],
        )
        self._note = pmui.Typography(
            EXPORT_NOTE, variant="body2", margin=0, sx={"color": "text.secondary"}
        )
        self._cancel = pmui.Button(label="Cancel", variant="text")
        self._cancel.on_click(lambda _event: self.close())
        self._confirm = pmui.Button(
            label="Export", variant="contained", css_classes=["fd-report-export-confirm"]
        )
        self._confirm.on_click(self._on_export)
        self.dialog = pmui.Dialog(
            objects=[
                self._format,
                self._pending,
                self._note,
                pn.Row(
                    pn.layout.HSpacer(),
                    self._cancel,
                    self._confirm,
                    margin=(12, 0, 0, 0),
                    sizing_mode="stretch_width",
                ),
            ],
            title="Export report",
            open=False,
            min_width=380,
            css_classes=["fd-report-export-dialog"],
            **escape_closes(),
        )
        # The editor mounts the dialog and the download beside its views.
        self.downloader = build_downloader()

    def _has_pending(self) -> bool:
        from panel_flowdash.report import report_blocks

        turns = {turn.id: turn for turn in self.editor.turns}
        return any(
            turns.get(block.get("turn")) is not None and turns[block["turn"]].status == "pending"
            for block in report_blocks(self.editor).blocks
        )

    def open(self):
        """Open the dialog, offering pending questions only when the report has some."""
        self._pending.visible = self._has_pending()
        self.dialog.open = True

    def close(self):
        """Close the dialog."""
        self.dialog.open = False

    def send(self, file: ExportFile):
        """Make the browser save *file*."""
        self.downloader.data = None
        self.downloader.param.update(
            callback=lambda: io.BytesIO(file.data), filename=file.filename
        )
        self.downloader.transfer(download=True)

    async def _on_export(self, _event=None):
        if self.exporting:
            return
        from panel_flowdash.export import ExportUnavailable, export_report_async

        editor = self.editor
        self.exporting = True
        self._confirm.param.update(loading=True, disabled=True)
        try:
            file = await export_report_async(
                editor,
                self._format.value,
                include_pending=self._pending.visible and self._pending.value,
            )
        except ExportUnavailable as exc:
            editor._notify("warning", str(exc), duration=8000)
            return
        except Exception:
            logger.exception("Exporting the report failed")
            editor._notify("error", "The report could not be exported.", duration=5000)
            return
        finally:
            self.exporting = False
            self._confirm.param.update(loading=False, disabled=False)
        self.send(file)
        self.close()
        editor._notify("success", f"Exported {file.filename}", duration=3000)

"""Config edits held back until the component's ``check_config`` passes.

The inspector edits a draft of a node's config when its component declares a
check. Each change runs the check on the draft, the latest change winning; a
draft that passes is applied to the node, one that fails stays in the form with
the problems shown beside it.
"""

from __future__ import annotations

import typing as t
import weakref
from functools import partial

import panel as pn
import param

from panel_flowdash.config_check import check_node, node_config, same_config
from panel_flowdash.ui.text import text_alert

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash


class ConfigDraft:
    """Changes to one node's config that wait for its component's check.

    Parameters
    ----------
    editor
        The editor holding the node.
    instance_id
        The node.
    runs
        The latest check run per node, shared by every draft of an inspector
        so a check started from a form that has since been rebuilt does not
        overwrite a newer one.
    results
        The node config last checked and the problems found, per node, shared
        the same way, so selecting a node again does not run its check again
        while its config is unchanged.
    """

    def __init__(
        self,
        editor: FlowDash,
        instance_id: str,
        runs: dict[str, int],
        results: dict[str, tuple[dict, list[str]]] | None = None,
    ):
        self._editor = weakref.ref(editor)
        self.instance_id = instance_id
        self._runs = runs
        self._results = {} if results is None else results
        self._pending: dict[str, t.Any] = {}
        self._syncing = False
        self._applying = False
        self.alert = pn.Column(margin=0, sizing_mode="stretch_width")
        self.state: param.Parameterized | None = None

    def bind(self, config_state: param.Parameterized, names: list[str]) -> list[t.Callable]:
        """Make :attr:`state`, a copy of *config_state* for a form to edit, and return its releases.

        Values the node gets from elsewhere, e.g. from the agent, are copied
        into the draft, replacing a pending change to the same field. A check
        still running is then dropped, and the node's config with the rest of
        the draft, if any, is checked again.
        """
        values = {name: getattr(config_state, name) for name in names}
        self.state = state = type(config_state)(**values)

        def propose(*events):
            if not self._syncing:
                self.propose({e.name: e.new for e in events})

        def sync(*events):
            self._syncing = True
            try:
                for event in events:
                    self._pending.pop(event.name, None)
                state.param.update({e.name: e.new for e in events})
            finally:
                self._syncing = False
            if not self._applying:
                self.propose({})

        draft_watcher = state.param.watch(propose, names)
        node_watcher = config_state.param.watch(sync, names)
        return [
            partial(state.param.unwatch, draft_watcher),
            partial(config_state.param.unwatch, node_watcher),
        ]

    def _config(self) -> dict[str, t.Any] | None:
        editor = self._editor()
        if editor is None:
            return None
        item = next((i for i in editor._tile_items if i["instance_id"] == self.instance_id), None)
        spec = editor._component_specs.get(item["component_id"]) if item else None
        return None if spec is None else node_config(editor, self.instance_id, spec)

    def show_current(self):
        """Show what the check says about the node's config, checking it unless it was already."""
        config = self._config()
        if config is None:
            return
        cached = self._results.get(self.instance_id)
        if cached is not None and same_config(cached[0], config):
            self._show(cached[1], {})
        else:
            self.propose({})

    def _show(self, problems: list[str], pending: dict[str, t.Any]):
        if not problems:
            self.alert[:] = []
            return
        self.alert[:] = [
            text_alert(
                "\n\n".join(problems),
                severity="error",
                title="Not applied" if pending else "Config refused",
                margin=(8, 0, 0, 0),
                sizing_mode="stretch_width",
                css_classes=["fd-config-check-alert"],
            )
        ]

    def propose(self, patch: dict[str, t.Any]):
        """Add *patch* to the draft and check it, applying the draft if the check passes."""
        self._pending.update(patch)
        run = self._runs.get(self.instance_id, 0) + 1
        self._runs[self.instance_id] = run
        param.parameterized.async_executor(partial(self.check, run, dict(self._pending)))

    async def check(self, run: int, pending: dict[str, t.Any]) -> list[str]:
        """Check *pending* and apply it if it passes and no newer change was made meanwhile."""
        editor = self._editor()
        if editor is None:
            return []
        problems = await check_node(editor, self.instance_id, pending)
        if self._runs.get(self.instance_id) != run:
            return problems
        self._show(problems, pending)
        if problems:
            if not pending and (config := self._config()) is not None:
                self._results[self.instance_id] = (config, problems)
            return problems
        self._pending.clear()
        if any(item["instance_id"] == self.instance_id for item in editor._tile_items):
            self._applying = True
            try:
                editor._apply_config_patch(self.instance_id, pending)
            finally:
                self._applying = False
            if (config := self._config()) is not None:
                self._results[self.instance_id] = (config, [])
        return problems

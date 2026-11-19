"""Which tiles of a dashboard failed or wait for input, kept current as the dataflow runs."""

from __future__ import annotations

import typing as t
import weakref

import param

from panel_flowdash.ui import tile_layout
from panel_flowdash.util import _blocking_ports, missing_inputs

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

OK, WAITING, FAILED = "ok", "waiting", "failed"

_SEVERITY = {OK: 0, WAITING: 1, FAILED: 2}


def worst(statuses: t.Iterable[str]) -> str:
    """Return the most severe of *statuses*, ``"ok"`` when there are none."""
    return max(statuses, key=_SEVERITY.__getitem__, default=OK)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


class DashboardHealth(param.Parameterized):
    """The state of an editor's tiles, derived from the dataflow graph.

    A tile *failed* while one of its inputs holds a failure carried from
    upstream (see ``FlowDash.propagate_failures``), and *waits* while a
    required, blocking input is unset. :meth:`track` follows the nodes on the
    canvas; call it whenever components are added or removed.
    """

    failed = param.List(default=[], doc="Instance ids of the tiles that failed.")

    origins = param.List(
        default=[],
        doc="Instance ids of the nodes whose outputs carry the failures downstream.",
    )

    tiles = param.List(default=[], doc="Instance ids of the tiles on the dashboard.")

    waiting = param.List(default=[], doc="Instance ids of the tiles waiting for input.")

    def __init__(self, editor: FlowDash, **params):
        super().__init__(**params)
        self._editor = editor
        self._watchers: list[tuple[param.Parameterized, t.Any]] = []
        self._tracked: tuple = ()

    def _nodes(self) -> list[tuple[str, t.Any]]:
        """Return the placed items with the spec of their component, if it is loaded."""
        editor = self._editor
        return [
            (item, editor._component_specs.get(item["component_id"]))
            for item in editor._tile_items
        ]

    def _is_tile(self, item: dict) -> bool:
        area = tile_layout.area_of(self._editor, item)
        return area is None or area == tile_layout.GRID

    def track(self):
        """Watch the failures and blocking inputs of the nodes on the canvas."""
        graph = self._editor._dataflow_graph
        nodes = self._nodes()
        key = tuple(
            (item["instance_id"], id(graph.get_state(item["instance_id"]))) for item, _ in nodes
        )
        if key != self._tracked:
            self.release()
            self._tracked = key
            for item, spec in nodes:
                instance_id = item["instance_id"]
                failures = graph.get_failures(instance_id)
                if failures is not None:
                    self._watch(failures, ["failed"])
                state = graph.get_state(instance_id)
                if state is not None and spec is not None:
                    ports = [p.name for p in _blocking_ports(state, spec.inputs)]
                    if ports:
                        self._watch(state, ports)
        self.update()

    def _watch(self, owner: param.Parameterized, names: list[str]):
        # Graph nodes can outlive the editor (a session may keep a component), so
        # they must not hold this object, and through it the editor, alive.
        method = weakref.WeakMethod(self.update)

        def update(*events):
            if (bound := method()) is not None:
                bound(*events)

        self._watchers.append((owner, owner.param.watch(update, names)))

    def release(self):
        """Stop following the graph."""
        for owner, watcher in self._watchers:
            owner.param.unwatch(watcher)
        self._watchers = []
        self._tracked = ()

    def update(self, *_events):
        """Recompute the state of every tile."""
        graph = self._editor._dataflow_graph
        tiles, failed, waiting, origins = [], [], [], []
        for item, spec in self._nodes():
            instance_id = item["instance_id"]
            failures = graph.get_failures(instance_id)
            held = failures.failed if failures is not None else {}
            for failure in held.values():
                if failure.source_id not in origins:
                    origins.append(failure.source_id)
            state = graph.get_state(instance_id)
            blocked = bool(
                state is not None and spec is not None and missing_inputs(state, spec.inputs)
            )
            if not self._is_tile(item):
                continue
            tiles.append(instance_id)
            if held:
                failed.append(instance_id)
            elif blocked:
                waiting.append(instance_id)
        self.param.update(
            {
                name: value
                for name, value in (
                    ("failed", failed),
                    ("origins", origins),
                    ("tiles", tiles),
                    ("waiting", waiting),
                )
                if getattr(self, name) != value
            }
        )

    def status(self, instance_id: str) -> str:
        """Return ``"failed"``, ``"waiting"`` or ``"ok"`` for one node."""
        if instance_id in self.failed or instance_id in self.origins:
            return FAILED
        if instance_id in self.waiting:
            return WAITING
        graph = self._editor._dataflow_graph
        failures = graph.get_failures(instance_id)
        if failures is not None and failures.failed:
            return FAILED
        spec = next(
            (spec for item, spec in self._nodes() if item["instance_id"] == instance_id), None
        )
        state = graph.get_state(instance_id)
        if state is not None and spec is not None and missing_inputs(state, spec.inputs):
            return WAITING
        return OK

    @property
    def affected(self) -> list[str]:
        """The tiles that failed or wait, failed ones first."""
        return [*self.failed, *self.waiting]

    def summary(self) -> str:
        """Return the chip label: "All 5 tiles up to date" or "1 failed, 1 waiting"."""
        if not self.tiles:
            return ""
        parts = []
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.waiting:
            parts.append(f"{len(self.waiting)} waiting")
        if parts:
            return ", ".join(parts)
        if len(self.tiles) == 1:
            return "1 tile up to date"
        return f"All {len(self.tiles)} tiles up to date"

    def sentence(self) -> tuple[str, str]:
        """Return the issue banner's message as a bold lead and the rest, empty without issues."""
        failed, waiting = len(self.failed), len(self.waiting)
        if failed and waiting:
            verb = "is" if waiting == 1 else "are"
            lead = f"{_count(failed, 'tile')} failed and {waiting} {verb} waiting for input."
        elif failed:
            lead = f"{_count(failed, 'tile')} failed."
        elif waiting:
            verb = "is" if waiting == 1 else "are"
            lead = f"{_count(waiting, 'tile')} {verb} waiting for input."
        else:
            return "", ""
        rest = "Other tiles show current data." if failed + waiting < len(self.tiles) else ""
        return lead, rest

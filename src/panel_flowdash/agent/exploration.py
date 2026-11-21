"""Explore's own editor: answers are built in a hidden editor beside the dashboard, never on it.

Each session keeps an :class:`Exploration` per open dashboard: a FlowDash
editor nobody sees and nothing saves, built like
``DashboardOps._scratch_editor``. It holds a **mirror** of every dashboard
node: the node's name, component and config, so tools reading the
exploration see the dashboard's nodes, but no component behind it. Each of a
mirror's outputs follows the dashboard node's output one way, so an answer
built on a mirror follows the dashboard's data, widgets and filters. When the
dashboard node is removed or replaced, its mirror keeps its last values and
stops following, and follows again if the node comes back, e.g. on Undo.

The Explore view's agent works through :class:`ExplorationOps`, which reads
the dashboard through the mirrors, adds and wires nodes of its own, and
cannot change mirrors, add data sources the host does not offer as they are,
or lay out, save or share anything. :meth:`ExplorationOps.add_to_dashboard`
copies an answer onto the dashboard.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import typing as t
import uuid
import weakref
from dataclasses import dataclass, field

import panel as pn
import param

from panel_flowdash.config_check import CheckResult
from panel_flowdash.turns import agent_edits
from panel_flowdash.ui import tile_layout

from .ops import DashboardOps, OperationError
from .yaml_format import node_names

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

    from .tools import AgentEditLock

logger = logging.getLogger(__name__)

#: A mirror that follows its dashboard node.
LIVE = "live"
#: A mirror whose dashboard node was removed; it keeps the last values it saw.
REMOVED = "removed"
#: A mirror whose dashboard node now uses another component; it keeps the last values it saw.
REPLACED = "replaced"

#: What the Add to dashboard edits wait as, for a request waiting on them.
ADD_LABEL = "an answer being added from Explore"


def config_hash(component: str, config: t.Any) -> str:
    """Return a short hash of a node's component and config, to tell when either changed."""
    from .tools import _dumps

    return hashlib.sha1(_dumps([component, config]).encode()).hexdigest()[:16]


@dataclass
class Mirror:
    """A dashboard node as the exploration sees it.

    *node* is the mirror's id in the exploration, usually the dashboard
    node's own; *source* is the dashboard node's id.
    """

    node: str
    source: str
    component: str
    config_hash: str
    status: str = LIVE
    #: The dashboard node's state the mirror's outputs follow.
    state: t.Any = field(default=None, repr=False)
    release: t.Callable[[], None] | None = field(default=None, repr=False)

    def unbind(self):
        release, self.release = self.release, None
        self.state = None
        if release is not None:
            release()


class _ExplorationEditor:
    """Mixed into the exploration's editor class: a mirror is never instantiated, only bound."""

    _exploration: t.Any = None

    def _instantiate_for_node(self, entry, node_state, config_state=None):
        exploration = self._exploration() if self._exploration is not None else None
        if exploration is None or node_state.name not in exploration.mirrors:
            return super()._instantiate_for_node(entry, node_state, config_state)
        exploration._bind(node_state.name, node_state)
        return pn.Spacer(height=0)


_EDITOR_CLASSES: dict[type, type] = {}


def _editor_class(base: type) -> type:
    cls = _EDITOR_CLASSES.get(base)
    if cls is None:
        cls = _EDITOR_CLASSES[base] = type(
            f"Exploration{base.__name__}", (_ExplorationEditor, base), {}
        )
    return cls


class Exploration:
    """The hidden editor of one session's Explore runs on one dashboard, with its mirrors."""

    def __init__(self, dashboard: DashboardOps):
        self.dashboard = dashboard
        source = dashboard.editor
        current = source.dashboard
        self.dashboard_id = current.dashboard_id if current is not None else None
        self.mirrors: dict[str, Mirror] = {}
        #: The dashboard node each added answer became, by (run, node).
        self.added: dict[tuple[str, str], str] = {}
        #: The answers the dashboard had already when they were added, by (run, node).
        self.already: set[tuple[str, str]] = set()
        #: The config hashes of the mirrors as each run ended, by run.
        self.seen: dict[str, dict[str, str]] = {}
        editor = dashboard._scratch_editor(_editor_class(type(source)))
        editor._exploration = weakref.ref(self)
        self.editor = editor
        model = source.to_model()
        model.turns = []
        for item in model.items:
            item.origin = None
            self.mirrors[item.instance_id] = Mirror(
                item.instance_id,
                item.instance_id,
                item.component_id,
                config_hash(item.component_id, item.config),
            )
        editor.load_model(model)
        for instance_id, item in self._items().items():
            if "placeholder" in item:
                self.mirrors.pop(instance_id, None)

    # ------------------------------------------------------------------
    # Mirrors
    # ------------------------------------------------------------------

    def _items(self, editor: FlowDash | None = None) -> dict[str, dict]:
        return {i["instance_id"]: i for i in (editor or self.editor)._tile_items}

    def _bind(self, instance_id: str, target: param.Parameterized | None = None):
        """Make the mirror's outputs follow the dashboard node's, one way."""
        mirror = self.mirrors[instance_id]
        mirror.unbind()
        source = self.dashboard.editor.graph.get_state(mirror.source)
        target = target or self.editor.graph.get_state(instance_id)
        spec = self.editor._component_specs.get(mirror.component)
        if source is None or target is None or spec is None:
            return
        inputs = {p.name for p in spec.inputs}
        ports = [
            p.name
            for p in spec.outputs
            if p.name not in inputs and p.name in source.param and p.name in target.param
        ]
        if not ports:
            mirror.state = source
            return
        target.param.update({port: getattr(source, port) for port in ports})

        def forward(*events):
            for event in events:
                if event.type == "triggered":
                    # Changed in place: the value is the same object, so only a trigger tells.
                    with param.parameterized.discard_events(target):
                        setattr(target, event.name, event.new)
                    target.param.trigger(event.name)
                else:
                    setattr(target, event.name, event.new)

        watcher = source.param.watch(forward, ports)
        released = []

        def release():
            if not released:
                released.append(True)
                source.param.unwatch(watcher)

        mirror.state, mirror.release = source, release
        # Released too when the exploration's editor drops the node or is disposed.
        self.editor._component_wiring.setdefault(instance_id, []).append(release)

    def is_mirror(self, instance_id: str) -> bool:
        return instance_id in self.mirrors

    def _same_component(self, item: dict, mirror: Mirror) -> bool:
        resolve = self.dashboard.editor._resolve_component_id
        return resolve(item["component_id"]) == resolve(mirror.component)

    def sync(self):
        """Bring the mirrors up to date with the dashboard: new nodes, edges, configs, removals.

        A mirror whose dashboard node was removed or replaced stops following
        it and keeps its last values; it follows again once the node is back
        with its component, e.g. after an Undo. A dashboard node whose id an
        exploration node or a frozen mirror holds is mirrored under another
        name, ``dashboard_<id>``.
        """
        source = self.dashboard.editor
        current = source.dashboard
        if current is None or current.dashboard_id != self.dashboard_id:
            return
        editor = self.editor
        theirs = self._items(source)
        ours = self._items()
        mirrors = list(self.mirrors.values())
        for mirror in mirrors:
            if mirror.status != LIVE:
                continue
            item = theirs.get(mirror.source)
            if item is None or "placeholder" in item:
                mirror.status = REMOVED
                mirror.unbind()
            elif not self._same_component(item, mirror):
                mirror.status = REPLACED
                mirror.unbind()
        followed = {m.source for m in mirrors if m.status == LIVE}
        for mirror in mirrors:
            item = theirs.get(mirror.source)
            if mirror.status == LIVE or mirror.source in followed or item is None:
                continue
            if "placeholder" not in item and self._same_component(item, mirror):
                mirror.status = LIVE
                followed.add(mirror.source)
        for mirror in self.mirrors.values():
            if mirror.status != LIVE:
                continue
            item = theirs[mirror.source]
            digest = config_hash(item["component_id"], item.get("config") or {})
            if digest != mirror.config_hash:
                ours[mirror.node]["config"] = copy.deepcopy(item.get("config") or {})
                mirror.config_hash = digest
            if source.graph.get_state(mirror.source) is not mirror.state:
                self._bind(mirror.node)
        taken = set(ours) | set(self.mirrors)
        for instance_id, item in theirs.items():
            if instance_id in followed or "placeholder" in item:
                continue
            component = item["component_id"]
            if component not in editor._component_specs:
                continue
            node = instance_id
            if node in taken:
                node = _free_name(f"dashboard_{instance_id}", taken)
            taken.add(node)
            self.mirrors[node] = Mirror(
                node, instance_id, component, config_hash(component, item.get("config") or {})
            )
            try:
                editor._place(component, node, copy.deepcopy(item.get("config") or {}))
            except Exception:
                logger.exception("Could not mirror the dashboard node %s", instance_id)
                self.mirrors.pop(node)
        nodes = {m.source: m.node for m in self.mirrors.values() if m.status == LIVE}
        live = set(nodes.values())
        wanted = {}
        for edge in source.graph.edges:
            if edge["source"] in nodes and edge["target"] in nodes:
                mapped = {**edge, "source": nodes[edge["source"]], "target": nodes[edge["target"]]}
                wanted[_edge_key(mapped)] = mapped
        have = {_edge_key(e) for e in editor.graph.edges if e["target"] in live}
        for key in have - set(wanted):
            editor.disconnect(*key[:4])
        for key in sorted(set(wanted) - have):
            editor.connect(*key[:4], wanted[key].get("transform") or None)
        for dashboard_id, node in nodes.items():
            literals = source.graph.literals(dashboard_id)
            if literals != editor.graph.literals(node):
                editor._set_inputs(node, literals)

    def mirror_of(self, dashboard_id: str) -> Mirror | None:
        """Return the live mirror of the dashboard node *dashboard_id*, if there is one."""
        return next(
            (m for m in self.mirrors.values() if m.source == dashboard_id and m.status == LIVE),
            None,
        )

    def dispose(self):
        for mirror in self.mirrors.values():
            mirror.unbind()
        self.editor.dispose()

    # ------------------------------------------------------------------
    # Runs and their answers
    # ------------------------------------------------------------------

    def made_by(self, run: str) -> list[str]:
        """Return the nodes run *run* made, in the order they were placed."""
        turns = {turn.id for turn in self.editor.turns if turn.run == run}
        return [
            iid
            for iid in self._items()
            if iid not in self.mirrors and self.editor.node_origin(iid) in turns
        ]

    def run_of(self, instance_id: str) -> str | None:
        origin = self.editor.node_origin(instance_id)
        turn = next((turn for turn in self.editor.turns if turn.id == origin), None)
        return turn.run if turn is not None else None

    def leaves(self, run: str) -> list[str]:
        """Return the nodes run *run* made that no other node it made reads: its answers."""
        made = self.made_by(run)
        fed = {e["source"] for e in self.editor.graph.edges if e["target"] in made}
        return [iid for iid in made if iid not in fed]

    def upstream(self, instance_id: str) -> list[str]:
        """Return the nodes *instance_id* is built from, nearest first, stopping at live mirrors."""
        edges = self.editor.graph.edges
        found: list[str] = []
        queue = [instance_id]
        while queue:
            node = queue.pop(0)
            for edge in edges:
                if edge["target"] != node or edge["source"] in found:
                    continue
                found.append(edge["source"])
                mirror = self.mirrors.get(edge["source"])
                if mirror is None or mirror.status != LIVE:
                    queue.append(edge["source"])
        return found

    def runs(self) -> list[str]:
        """Return the runs that made nodes here, oldest first."""
        return list(dict.fromkeys(turn.run for turn in self.editor.turns if turn.run))

    def note_run(self, run: str, keep: int | None = None):
        """Remember the dashboard nodes as run *run* saw them, to tell later when one changed.

        With *keep*, only the answers of the latest *keep* runs stay; the
        nodes of older runs are removed, unless a kept answer is built on them.
        """
        self.seen[run] = {iid: m.config_hash for iid, m in self.mirrors.items()}
        runs = self.runs()
        if keep is None or len(runs) <= keep:
            return
        dropped = set(runs[:-keep])
        needed = set()
        for kept in runs[-keep:]:
            for node in self.made_by(kept):
                needed.update(self.upstream(node))
        for run_id in dropped:
            for node in self.made_by(run_id):
                if node not in needed:
                    self.editor.remove_component(node)
            self.seen.pop(run_id, None)
        self.added = {k: v for k, v in self.added.items() if k[0] not in dropped}

    def changed_since(self, run: str, instance_id: str) -> list[tuple[str, str]]:
        """Return the dashboard nodes an answer is built on that changed since its run, with how."""
        seen = self.seen.get(run, {})
        names = node_names(self.editor._tile_items)
        changed = []
        for iid in self.upstream(instance_id):
            mirror = self.mirrors.get(iid)
            if mirror is None:
                continue
            if mirror.status != LIVE:
                changed.append((names.get(iid, iid), mirror.status))
            elif iid in seen and seen[iid] != mirror.config_hash:
                changed.append((names.get(iid, iid), "changed"))
        return changed


def _edge_key(edge: dict) -> tuple:
    from .tools import _dumps

    return (
        edge["source"],
        edge["source_port"],
        edge["target"],
        edge["target_port"],
        _dumps(edge.get("transform") or []),
    )


class ExplorationOps(DashboardOps):
    """:class:`DashboardOps` on the exploration of the dashboard open in *dashboard*'s editor.

    The Explore view's agent works through it. It reads the dashboard's
    nodes through their mirrors, adds nodes of its own and wires them to
    the mirrors, and is refused anything else: changing a mirror, adding a
    data source, laying out, the dashboard's settings and parameters,
    replacing, saving, opening, deleting and sharing dashboards.

    Parameters
    ----------
    dashboard
        The operations on the dashboard the user has open, e.g. the
        session's :class:`~panel_flowdash.agent.live.LiveDashboardOps`.
    freeze_answers
        Whether the nodes of earlier runs stay as they are, so a follow-up
        adds nodes of its own and each answer keeps showing what it showed.
        False lets a follow-up change them. Defaults to :attr:`FREEZE_ANSWERS`.
    allow_source
        Called with a data source's component id and config to tell whether
        Explore may add it. By default a source the host offers may be added
        as it is, without config of its own, so new locations or uploads are
        refused. A source the dashboard already has is never added again:
        the agent is told to build on the dashboard's node instead.

    Only the open dashboard's exploration is kept: opening another drops it,
    and only the answers of the latest :attr:`MAX_RUNS` requests stay live.
    """

    #: Whether earlier runs' nodes are frozen by default; see ``freeze_answers``.
    FREEZE_ANSWERS: t.ClassVar[bool] = True

    #: How many requests' answers an exploration keeps live; older ones are removed.
    MAX_RUNS: t.ClassVar[int] = 20

    #: Edits here are not versions of the dashboard; the tools return their message only.
    versioned = False

    def __init__(
        self,
        dashboard: DashboardOps,
        *,
        freeze_answers: bool | None = None,
        allow_source: t.Callable[[str, dict[str, t.Any]], bool] | None = None,
    ):
        self.dashboard = dashboard
        self.freeze_answers = self.FREEZE_ANSWERS if freeze_answers is None else freeze_answers
        self.allow_source = allow_source or (lambda _component, config: not config)
        self.last_check = CheckResult()
        self._explorations: dict[str | None, Exploration] = {}
        self._adding: dict[tuple[str, str], asyncio.Future] = {}
        dashboard.editor.param.watch(self._on_dashboard, "dashboard")

    def _on_dashboard(self, event):
        current = event.new.dashboard_id if event.new is not None else None
        for dashboard_id in [d for d in self._explorations if d != current]:
            self._explorations.pop(dashboard_id).dispose()

    @property
    def editor(self) -> FlowDash:
        """The exploration's editor of the dashboard open now."""
        return self.exploration().editor

    def exploration(self, dashboard_id: str | None = ...) -> Exploration:  # type: ignore[assignment]
        """Return the exploration of the open dashboard, or of *dashboard_id*, made on first use."""
        if dashboard_id is ...:
            current = self.dashboard.editor.dashboard
            dashboard_id = current.dashboard_id if current is not None else None
        found = self._explorations.get(dashboard_id)
        if found is None:
            found = self._explorations[dashboard_id] = Exploration(self.dashboard)
        return found

    def explorations(self) -> dict[str | None, Exploration]:
        """Return the explorations made so far, by dashboard id."""
        return dict(self._explorations)

    def sync(self):
        """Bring the open dashboard's exploration up to date with the dashboard."""
        self.exploration().sync()

    def dispose(self):
        for exploration in self._explorations.values():
            exploration.dispose()
        self._explorations.clear()

    # ------------------------------------------------------------------
    # Rights
    # ------------------------------------------------------------------

    def _require_edit(self):
        editor = self.dashboard.editor
        if editor.dashboard is None:
            raise OperationError("No dashboard is open.")
        if editor.read_only:
            raise PermissionError("You may only view this dashboard, so Explore cannot add to it.")

    def _can_sample(self) -> bool:
        return self.dashboard._can_sample()

    def _source_visible(self) -> bool:
        return self.dashboard._source_visible()

    def _frozen(self, instance_id: str, name: str) -> str | None:
        """Return why the node may not be changed, or None when the current run may change it."""
        exploration = self.exploration()
        if exploration.is_mirror(instance_id):
            return (
                f"'{name}' is a node of the dashboard, which Explore cannot change. Build on it: "
                "connect its outputs to nodes of your own."
            )
        if not self.freeze_answers:
            return None
        from panel_agents import current_run_id

        run = exploration.run_of(instance_id)
        if run is not None and run != current_run_id():
            return (
                f"'{name}' is part of an earlier answer, and earlier answers stay as they are. "
                "Build on it instead: add nodes of your own and connect its outputs to them."
            )
        return None

    def _refuse_frozen(self, name: str) -> str:
        instance_id = self._instance(name)
        reason = self._frozen(instance_id, name)
        if reason is not None:
            raise PermissionError(reason)
        return instance_id

    def _new_node(self, component: str, config: dict[str, t.Any] | None):
        spec, resolved = super()._new_node(component, config)
        if self.editor._node_role(spec.component_id) != "source":
            return spec, resolved
        from .tools import _dumps

        exploration = self.exploration()
        items = exploration._items()
        wanted = _dumps(resolved)
        for mirror in exploration.mirrors.values():
            item = items.get(mirror.node)
            if (
                mirror.status == LIVE
                and item is not None
                and self.editor._resolve_component_id(item["component_id"]) == spec.component_id
                and _dumps(item.get("config") or {}) == wanted
            ):
                raise OperationError(
                    f"The dashboard's node '{mirror.node}' is this source already; connect "
                    "from its outputs instead of adding it again."
                )
        if not self.allow_source(spec.component_id, resolved):
            raise PermissionError(
                f"'{spec.title}' with this config is a data source the dashboard does not "
                "offer, which Explore cannot add. Use the dashboard's data or a source as the "
                "host offers it, or ask the user to add it with the assistant."
            )
        return spec, resolved

    def _config_patch(self, name: str, config: dict[str, t.Any]):
        self._require_edit()
        self._refuse_frozen(name)
        return super()._config_patch(name, config)

    def _inputs_patch(self, name: str, inputs: dict[str, t.Any]) -> dict[str, t.Any]:
        self._require_edit()
        self._refuse_frozen(name)
        return super()._inputs_patch(name, inputs)

    def _port(self, ref: str, direction: str) -> tuple[str, str]:
        instance_id, port = super()._port(ref, direction)
        if direction == "target":
            reason = self._frozen(instance_id, ref.partition(".")[0])
            if reason is not None:
                raise PermissionError(reason)
        return instance_id, port

    def remove_node(self, name: str) -> None:
        self._require_edit()
        self._refuse_frozen(name)
        super().remove_node(name)

    def _checked_nodes(self, editor: FlowDash) -> list[str]:
        # A mirror's config is the dashboard's to check; checking it here could run its queries again.
        exploration = self.exploration()
        return [iid for iid in super()._checked_nodes(editor) if not exploration.is_mirror(iid)]

    def preview(self, text: str | None = None, *, rows: int = 5) -> dict[str, t.Any]:
        self._refuse_draft(text)
        return super().preview(rows=rows)

    async def preview_async(self, text: str | None = None, *, rows: int = 5, **kwargs):
        self._refuse_draft(text)
        return await super().preview_async(rows=rows, **kwargs)

    @staticmethod
    def _refuse_draft(text: t.Any):
        if text is not None:
            raise OperationError(
                "Explore previews its nodes as they are; call preview_dashboard without YAML."
            )

    def _refused(self, *_args, **_kwargs):
        raise PermissionError(
            "Explore answers questions and cannot change the dashboard's layout, settings or "
            "parameters, replace, save, open, delete or share dashboards. Tell the user they "
            "can add an answer from its card, or ask the assistant."
        )

    place_tile = group_nodes = ungroup = arrange_canvas = arrange_layout = _refused
    set_settings = declare_parameter = remove_parameter = bind_parameters = _refused
    replace_yaml = replace_yaml_async = save = open_dashboard = _refused
    open_dashboard_async = delete_dashboard = share_dashboard = _refused

    # ------------------------------------------------------------------
    # Add to dashboard
    # ------------------------------------------------------------------

    async def add_to_dashboard(
        self,
        run: str,
        node: str,
        *,
        question: str | None = None,
        edit_lock: AgentEditLock | None = None,
        on_wait: t.Callable[[str | None], None] | None = None,
    ) -> str:
        """Copy the answer *node* of Explore run *run* onto the dashboard; return its name there.

        The node and the nodes it is built from are added, down to the
        dashboard nodes it reads, which it is connected to. A node the
        dashboard already has, with the same component, config and inputs,
        is reused; a name the dashboard uses for another node gets a
        ``_2`` suffix. Everything is one turn of run *run* and one version
        of the edit history, so one Undo takes it back. Only the answer's
        tile is shown; the nodes it is built from are laid out as the
        dashboard lays out what feeds its tiles.

        When the dashboard shows the answer already, nothing changes. When a
        write fails, the nodes written so far are removed again. Asking for
        the same answer while it is being added waits for that addition.

        Raises
        ------
        OperationError
            If the exploration has no such node, or another dashboard is open.
        PermissionError
            If the user may not edit the dashboard, or the dashboard is busy.
        """
        key = (run, node)
        pending = self._adding.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future = asyncio.get_running_loop().create_future()
        self._adding[key] = future
        try:
            name = await self._add(run, node, question, edit_lock, on_wait)
        except BaseException as exc:
            future.set_exception(exc)
            # Retrieved, so a failure nobody else waited for is not reported as unhandled.
            future.exception()
            raise
        else:
            future.set_result(name)
            return name
        finally:
            self._adding.pop(key, None)

    async def _add(self, run, node, question, edit_lock, on_wait) -> str:
        target = self.dashboard
        current = target.editor.dashboard
        if current is None:
            raise OperationError("No dashboard is open.")
        exploration = self._explorations.get(current.dashboard_id)
        if exploration is None or node not in exploration._items():
            raise OperationError(f"No answer '{node}' to add on this dashboard.")
        if exploration.is_mirror(node):
            raise OperationError(f"'{node}' is already a node of the dashboard.")
        if target.editor.read_only:
            raise PermissionError("You may only view this dashboard, so nothing can be added.")
        key = (run, node)
        token = f"add-{uuid.uuid4().hex[:12]}"
        if edit_lock is not None:
            refusal = await edit_lock.acquire(current.dashboard_id, token, ADD_LABEL, on_wait)
            if refusal is not None:
                raise PermissionError(refusal)
        try:
            exploration.sync()
            editor = target.editor
            if editor.dashboard is None or editor.dashboard.dashboard_id != current.dashboard_id:
                raise OperationError("Another dashboard was opened; nothing was added.")
            names = target._names()
            added = exploration.added.get(key)
            if added is not None and added in names.values() and _placed(editor, added):
                return added
            plan = self._plan(exploration, node)
            leaf = plan.on_dashboard.get(node)
            if not plan.create and leaf is not None and _placed(editor, leaf):
                # The dashboard shows this answer already: nothing to add, and nothing to undo.
                exploration.added[key] = names[leaf]
                exploration.already.add(key)
                return names[leaf]
            editor.end_turn()
            editor._defer_tile_layout(run)
            try:
                with agent_edits(run, question):
                    name = await self._write(exploration, plan, node)
            except BaseException:
                # Its nodes were taken back, so the turn and the version would record nothing.
                editor._discard_agent_turn(run)
                raise
            finally:
                editor.end_agent_turns(run)
        finally:
            if edit_lock is not None:
                edit_lock.release(token)
        exploration.added[key] = name
        exploration.already.discard(key)
        return name

    def _plan(self, exploration: Exploration, node: str) -> _Plan:
        """Work out which nodes of an answer the dashboard has, and which to add under which names."""
        target = self.dashboard
        source = exploration.editor
        names = node_names(source._tile_items)
        closure = [node, *exploration.upstream(node)]
        live = {
            iid: exploration.mirrors[iid].source
            for iid in closure
            if exploration.is_mirror(iid) and exploration.mirrors[iid].status == LIVE
        }
        copied = [iid for iid in closure if iid not in live]
        incoming: dict[str, list[dict]] = {iid: [] for iid in copied}
        for edge in source.graph.edges:
            if edge["target"] in incoming:
                incoming[edge["target"]].append(edge)
        # Upstream first, so each node's inputs are on the dashboard before it.
        order: list[str] = []
        pending = list(reversed(copied))
        while pending:
            ready = next(
                iid
                for iid in pending
                if all(e["source"] in order or e["source"] not in copied for e in incoming[iid])
            )
            pending.remove(ready)
            order.append(ready)
        on_dashboard = dict(live)
        taken = set(target._names().values()) | {
            i["instance_id"] for i in target.editor._tile_items
        }
        create: list[tuple[str, str]] = []
        ours = exploration._items()
        for iid in order:
            literals = source._dataflow_graph.literals(iid)
            found = self._same_node(target, ours[iid], incoming[iid], literals, on_dashboard)
            if found is not None:
                on_dashboard[iid] = found
                continue
            name = _free_name(names.get(iid, iid), taken)
            taken.add(name)
            on_dashboard[iid] = name
            create.append((iid, name))
        return _Plan(create, incoming, on_dashboard)

    async def _write(self, exploration: Exploration, plan: _Plan, node: str) -> str:
        """Add the nodes *plan* creates and show the answer's tile, or remove them all on failure."""
        target = self.dashboard
        source = exploration.editor
        ours = exploration._items()
        written: list[str] = []
        try:
            for iid, name in plan.create:
                item = ours[iid]
                added = await target.add_node_async(
                    item["component_id"], copy.deepcopy(item.get("config") or {}), name=name
                )
                written.append(added)
                for edge in plan.incoming[iid]:
                    upstream = target._names()[plan.on_dashboard[edge["source"]]]
                    await target.connect_async(
                        f"{upstream}.{edge['source_port']}",
                        f"{added}.{edge['target_port']}",
                        edge.get("transform") or None,
                    )
                literals = source._dataflow_graph.literals(iid)
                if literals:
                    await target.set_inputs_async(added, copy.deepcopy(literals))
            name = target._names()[plan.on_dashboard[node]]
            target.place_tile(name, visible=True)
        except BaseException:
            for added in reversed(written):
                try:
                    target.remove_node(added)
                except Exception:
                    logger.exception("Could not take back %s after a failed Add", added)
            raise
        return name

    @staticmethod
    def _same_node(
        target: DashboardOps,
        item: dict,
        edges: list[dict],
        literals: dict[str, t.Any],
        on_dashboard: dict[str, str],
    ) -> str | None:
        """Return the dashboard node with *item*'s component, config and inputs, if there is one.

        Inputs are compared as a set, edges and literal values alike, since
        the order they were made in does not change what the node computes.
        """
        from .tools import _dumps

        editor = target.editor
        component = editor._resolve_component_id(item["component_id"])
        config = _dumps(item.get("config") or {})
        wanted = {
            (
                on_dashboard.get(e["source"]),
                e["source_port"],
                e["target_port"],
                _dumps(e.get("transform") or []),
            )
            for e in edges
        } | {(None, None, port, _dumps(value)) for port, value in literals.items()}
        for other in editor._tile_items:
            if "placeholder" in other:
                continue
            if editor._resolve_component_id(other["component_id"]) != component:
                continue
            if _dumps(other.get("config") or {}) != config:
                continue
            inputs = {
                (e["source"], e["source_port"], e["target_port"], _dumps(e.get("transform") or []))
                for e in editor.graph.edges
                if e["target"] == other["instance_id"]
            } | {
                (None, None, port, _dumps(value))
                for port, value in editor.graph.literals(other["instance_id"]).items()
            }
            if inputs == wanted:
                return other["instance_id"]
        return None


@dataclass
class _Plan:
    """What adding an answer writes: the nodes to create with their names and inputs."""

    create: list[tuple[str, str]]
    incoming: dict[str, list[dict]]
    #: Each node of the closure as the dashboard names it, or will once created.
    on_dashboard: dict[str, str]


def _placed(editor: FlowDash, instance_id: str) -> bool:
    """Whether the dashboard shows the node: as a visible tile, above the grid or in its sidebar."""
    item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
    if item is None:
        return False
    if tile_layout.area_of(editor, item) in (tile_layout.TOP, tile_layout.SIDEBAR):
        return True
    return any(i["instance_id"] == instance_id for i in tile_layout.grid_tiles(editor))


def _free_name(name: str, taken: t.Collection[str]) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


__all__ = ["Exploration", "ExplorationOps", "Mirror"]

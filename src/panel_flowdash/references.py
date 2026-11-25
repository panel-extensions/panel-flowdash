"""References: nodes that show a node of another dashboard, read only.

A reference is an ordinary node whose component is the built-in
``Data/Reference``, configured with ``{dashboard: <dashboard id>, node: <node
name or instance id>}``, so it needs nothing new in the dashboard model or its
YAML. Its outputs are the source node's outputs and its tile shows the source
node as its own dashboard shows it: evaluated against that dashboard as it is
saved, with its own filters, parameters and settings. The node is edited where
it lives; saving its dashboard updates every reference to it.

Each session evaluates a source dashboard once, in a :class:`SourceGraph`
that every reference to it in the session shares. It holds an editor that is
never put on a page, loaded with only the referenced nodes and what feeds
them. Components get the editor they expect from ``current_dashboard()``,
with the source's settings and context, so a reference behaves like the node
in its own dashboard. A view can only be on screen once, so each reference
asks the source graph for a view of its own (:meth:`FlowDash.render_node`),
and a node shown in its dashboard and through a reference in another, side by
side, is two views of one computation per session.

A reference to a node or dashboard that was deleted shows "Missing node:
<name>", and one to a dashboard the viewer may not open shows "No access",
on the tile and as its provenance chip, which otherwise says "From <source
dashboard>". The store tells the source graphs of a process when a dashboard
is saved, deleted, renamed or reshared, see
:meth:`~panel_flowdash.BaseDashboardStore.watch_changes`.
"""

from __future__ import annotations

import copy
import logging
import typing as t
import weakref
from contextvars import ContextVar
from dataclasses import replace
from functools import partial

import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash.node_actions import Provenance
from panel_flowdash.placeholders import Placeholder, placeholder_view
from panel_flowdash.registry import register
from panel_flowdash.util import ReactiveError

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import ComponentSpec
    from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
    from panel_flowdash.editor import FlowDash

logger = logging.getLogger(__name__)

REFERENCE_COMPONENT = "Data/Reference"

# The source graphs of each session, or of each editor outside a session, by
# store, user and dashboard id.
_SOURCES: weakref.WeakKeyDictionary[t.Any, dict[tuple[int, str, str], SourceGraph]] = (
    weakref.WeakKeyDictionary()
)

# The references being resolved in this context, so one that leads back to itself stops.
_RESOLVING: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("_RESOLVING", default=())

# References that lead through more dashboards than this are refused as circular.
_MAX_DEPTH = 8


def _session_doc() -> t.Any:
    doc = pn.state.curdoc
    return doc if doc is not None and doc.session_context is not None else None


def _doc_ref() -> weakref.ref | None:
    doc = _session_doc()
    return weakref.ref(doc) if doc is not None else None


def _schedule(doc_ref: weakref.ref | None, callback: t.Callable[[], t.Any]) -> bool:
    """Run *callback* on the next tick of the session of *doc_ref*, as that session.

    A store change can come from another session's thread, whose context the
    callback would otherwise run in. Returns False when there is no session
    to run it in any more.
    """
    from panel.io.state import set_curdoc

    doc = doc_ref() if doc_ref is not None else None
    if doc is None:
        return False

    def run():
        with set_curdoc(doc):
            callback()

    try:
        doc.add_next_tick_callback(run)
    except Exception:
        logger.debug("The session of a reference is gone", exc_info=True)
        return False
    return True


def _session_destroyed(doc_ref: weakref.ref, session_context: t.Any):
    """Dispose the source graphs of a session that ended, right away rather than on a tick that never comes."""
    doc = doc_ref()
    graphs = _SOURCES.pop(doc, {}) if doc is not None else {}
    for graph in list(graphs.values()):
        graph.dispose(ended=True)


def may_inspect(editor: FlowDash, model: DashboardModel) -> bool:
    """Whether the editor's user may see every node of *model*, not only its tiles.

    They may if they may edit it, or if it lets its viewers inspect outputs
    (its ``viewers_can_inspect`` setting).
    """
    return editor._dashboard_authorized(model, "edit") or bool(
        model.settings.get("viewers_can_inspect", False)
    )


def visible_nodes(editor: FlowDash, model: DashboardModel) -> set[str]:
    """Return the nodes of *model* a viewer sees: all but those whose tile is hidden."""
    from panel_flowdash.editor import _key_saved_layout

    layout = _key_saved_layout(model.tile_layout, editor._legacy_layout_ids(model))
    hidden = {entry["instance_id"] for entry in layout if entry.get("visible") is False}
    return {item.instance_id for item in model.items} - hidden


def access(
    editor: FlowDash, model: DashboardModel | None, dashboard_id: str, node: str
) -> tuple[str, str | None]:
    """Return whether the editor's user may reference *node* of *model*: a status and its instance id.

    The status is ``"ok"``, ``"missing"`` (the dashboard or node does not
    exist) or ``"denied"``: the user may not view the dashboard, the node
    has no visible tile and they may not inspect the dashboard, or they may
    not use the node's component. A dashboard they may not view whose id
    could be guessed is answered like a missing one, as the app does.
    """
    if model is None:
        return "missing", None
    if not editor._dashboard_authorized(model, "view"):
        guessable = editor.store is not None and editor.store.guessable_id(dashboard_id)
        return ("missing" if guessable else "denied"), None
    item = find_item(model, node)
    if item is None:
        return "missing", None
    if item.instance_id not in visible_nodes(editor, model) and not may_inspect(editor, model):
        return "denied", None
    component_id = editor._resolve_component_id(item.component_id, editor._component_aliases())
    if component_id in editor._component_entries and not editor._component_authorized(
        component_id
    ):
        return "denied", None
    return "ok", item.instance_id


def find_item(model: DashboardModel, node: str) -> DashboardItem | None:
    """Return the item of *model* that *node* names, by instance id or by its name in the YAML."""
    from panel_flowdash.agent.yaml_format import node_names

    for item in model.items:
        if item.instance_id == node:
            return item
    names = node_names({"instance_id": item.instance_id} for item in model.items)
    by_name = {name: iid for iid, name in names.items()}
    return next((item for item in model.items if item.instance_id == by_name.get(node)), None)


def _upstream(model: DashboardModel, nodes: t.Iterable[str]) -> set[str]:
    """Return *nodes* and every node of *model* feeding them, directly or not."""
    sources: dict[str, list[str]] = {}
    for edge in model.edges:
        sources.setdefault(edge.target, []).append(edge.source)
    found = set(nodes)
    frontier = list(found)
    while frontier:
        for source in sources.get(frontier.pop(), ()):
            if source not in found:
                found.add(source)
                frontier.append(source)
    return found


def _pruned(model: DashboardModel, keep: set[str]) -> DashboardModel:
    """Return the part of *model* that computes the nodes in *keep*, without its presentation."""
    from panel_flowdash.global_filters import map_filters

    pruned = copy.deepcopy(model)
    pruned.items = [item for item in pruned.items if item.instance_id in keep]
    pruned.edges = [e for e in pruned.edges if e.source in keep and e.target in keep]
    pruned.parameters = {n: p for n, p in pruned.parameters.items() if p.node in keep}
    pruned.filters = map_filters(pruned.filters, {iid: iid for iid in keep})
    pruned.turns = []
    pruned.groups = []
    pruned.report = None
    pruned.tile_layout = []
    pruned.responsive_layouts = {}
    for item in pruned.items:
        item.origin = None
    return pruned


class SourceGraph:
    """A source dashboard evaluated without a page, for the references to it in one session.

    It holds a :class:`~panel_flowdash.FlowDash` that is never rendered,
    loaded with the referenced nodes and their upstream only, read only and
    without history. A reference to a node it has not loaded yet loads that
    node's upstream too, patching the nodes already there. When the store
    says the dashboard changed, the graph reads it again on its session's
    event loop, patches what changed and tells its references.
    """

    def __init__(self, host: FlowDash, dashboard_id: str):
        self.dashboard_id = dashboard_id
        self.store = host.store
        # The editors its references are on, all of one user; any one of them answers for access.
        self._hosts: weakref.WeakSet[FlowDash] = weakref.WeakSet([host])
        self._doc = _doc_ref()
        self.editor: FlowDash | None = None
        self.model: DashboardModel | None = None
        self.disposed = False
        # The nodes references show, and the nodes the editor holds for them.
        self._wanted: dict[str, int] = {}
        self._loaded: set[str] = set()
        self._references: weakref.WeakSet[Reference] = weakref.WeakSet()
        self._refresh_pending = False
        self._read()
        self.store.watch_changes(self._on_store_change)

    @property
    def title(self) -> str | None:
        return self.model.title if self.model is not None else None

    def _read(self):
        self.model = self.store._load_any(self.dashboard_id)

    def instance_id(self, node: str) -> str | None:
        """Return the instance id of the node *node* names in the stored dashboard, if it has one."""
        if self.model is None:
            return None
        item = find_item(self.model, node)
        return item.instance_id if item is not None else None

    @property
    def host(self) -> FlowDash | None:
        return next(iter(self._hosts), None)

    def attach(self, reference: Reference, instance_id: str):
        """Have the graph compute *instance_id* for *reference*."""
        self._references.add(reference)
        if reference._host is not None:
            self._hosts.add(reference._host)
        self._wanted[instance_id] = self._wanted.get(instance_id, 0) + 1
        if instance_id not in self._loaded:
            self._load()

    def detach(self, reference: Reference, instance_id: str | None):
        """Stop computing *instance_id* for *reference*; the graph goes with its last reference."""
        if self.disposed:
            return
        if reference._source is not self:
            self._references.discard(reference)
        if instance_id is not None and instance_id in self._wanted:
            self._wanted[instance_id] -= 1
            if not self._wanted[instance_id]:
                del self._wanted[instance_id]
        if self._references:
            return
        # A node placed again, e.g. with new ports, takes the graph over on this tick.
        if not _schedule(self._doc, self._dispose_unused):
            self.dispose()

    def _dispose_unused(self):
        if not self._references:
            self.dispose()

    def state(self, instance_id: str) -> param.Parameterized | None:
        if self.editor is None:
            return None
        return self.editor._dataflow_graph.get_state(instance_id)

    def render(self, instance_id: str):
        """Return a new view of a node of the source, and what releases it, see :meth:`FlowDash.render_node`."""
        return self.editor.render_node(instance_id)

    def _make_editor(self) -> FlowDash:
        from panel_flowdash.editor import _SESSION_EDITORS, FlowDash

        host = self.host
        doc = pn.state.curdoc
        previous = _SESSION_EDITORS.get(doc) if doc is not None else None
        try:
            editor = FlowDash(
                components=host._registry,
                include_builtin_components=False,
                context=host.context,
                settings_class=host.settings_class,
                propagate_failures=host.propagate_failures,
                authorize_component=host.authorize_component,
                authorize_dashboard=host.authorize_dashboard,
                project=host.project,
                user=host.user,
                store=host.store,
                read_only=True,
                history=False,
                notifications=False,
                node_previews=False,
                include_page_dialog=False,
                toolbar=False,
            )
        finally:
            # The session's own editor stays the one `current_dashboard` falls back to.
            if doc is not None:
                if previous is not None:
                    _SESSION_EDITORS[doc] = previous
                else:
                    _SESSION_EDITORS.pop(doc, None)
        return editor

    def _load(self):
        """Load the wanted nodes and their upstream, patching what the editor already holds.

        Only the nodes the user may reference now are loaded, checked
        against the very model that is loaded, so a dashboard whose
        sharing changed is never computed for someone it is no longer
        shared with.
        """
        from panel_flowdash.editor import _placing

        host = self.host
        if self.model is None or host is None:
            return
        allowed = [
            iid
            for iid in self._wanted
            if access(host, self.model, self.dashboard_id, iid)[0] == "ok"
        ]
        if not allowed:
            if self.editor is not None and self._loaded:
                self.editor._reset_canvas()
            self._loaded = set()
            return
        keep = _upstream(self.model, allowed)
        model = _pruned(self.model, keep)
        if self.editor is None:
            self.editor = self._make_editor()
        editor = self.editor
        editor.ensure_components_loaded(editor._model_component_ids(model))
        with editor._quiet_history(), _placing("load"):
            patched = False
            if editor.dashboard is not None:
                editor._hydrating = True
                try:
                    patched = editor._patch_canvas(model)
                finally:
                    editor._hydrating = False
            if not patched:
                editor._hydrate_model(model)
        self._loaded = keep

    def _on_store_change(self, dashboard_id: str):
        if dashboard_id != self.dashboard_id or self.disposed or self._refresh_pending:
            return
        if self._doc is None:
            self.refresh()
            return
        self._refresh_pending = True
        if not _schedule(self._doc, self.refresh):
            self.dispose(ended=True)

    def refresh(self):
        """Read the dashboard again, load what changed and tell the references to it."""
        self._refresh_pending = False
        if self.disposed:
            return
        self._read()
        if self.model is not None and self._wanted:
            try:
                self._load()
            except Exception:
                logger.exception(
                    "Loading the dashboard %r for its references failed", self.dashboard_id
                )
        for reference in list(self._references):
            try:
                reference._source_changed()
            except Exception:
                logger.exception("Updating a reference to %r failed", self.dashboard_id)

    def dispose(self, ended: bool = False):
        """Stop the graph's components and stop following the store.

        With *ended*, the session is gone, so its components are disposed
        without updating views that are no longer anywhere.
        """
        if self.disposed:
            return
        self.disposed = True
        self.store.unwatch_changes(self._on_store_change)
        editor, self.editor = self.editor, None
        if editor is not None:
            if ended:
                editor._stop_refresh_timer()
                editor._dispose_components()
            else:
                editor.dispose()
        for graphs in list(_SOURCES.values()):
            for key, graph in list(graphs.items()):
                if graph is self:
                    del graphs[key]


def source_graph(host: FlowDash, dashboard_id: str) -> SourceGraph:
    """Return the source graph of *dashboard_id* for *host*'s session and user, creating it if needed.

    In a session the graphs are shared by the session's editors of one user;
    outside a session each editor has its own, so editors of different users
    or contexts never share one.
    """
    doc = _session_doc()
    owner = doc if doc is not None else host
    graphs = _SOURCES.get(owner)
    if graphs is None:
        graphs = _SOURCES[owner] = {}
        if doc is not None:
            doc.on_session_destroyed(partial(_session_destroyed, weakref.ref(doc)))
    key = (id(host.store), host.user, dashboard_id)
    graph = graphs.get(key)
    if graph is None or graph.disposed:
        graph = graphs[key] = SourceGraph(host, dashboard_id)
    return graph


def _source_spec(editor: FlowDash, dashboard: str, node: str) -> ComponentSpec | None:
    """Return the spec of the node *node* of the dashboard *dashboard*, if the editor's user may read it."""
    if not (dashboard and node) or editor.store is None:
        return None
    resolving = _RESOLVING.get()
    if (dashboard, node) in resolving or len(resolving) >= _MAX_DEPTH:
        return None
    model = editor.store._load_any(dashboard)
    if access(editor, model, dashboard, node)[0] != "ok":
        return None
    item = find_item(model, node)
    component_id = editor._resolve_component_id(item.component_id, editor._component_aliases())
    editor.ensure_components_loaded([component_id])
    spec = editor._component_specs.get(component_id)
    hook = getattr(editor._component_entries.get(component_id), "app", None)
    hook = getattr(hook, "__flowdash_instance_spec__", None)
    if spec is not None and hook is not None:
        token = _RESOLVING.set((*resolving, (dashboard, node)))
        try:
            spec = hook(spec, item.config, editor) or spec
        finally:
            _RESOLVING.reset(token)
    return spec


def reference_choices(config: t.Mapping[str, t.Any], inputs: t.Mapping[str, t.Any]):
    """Offer the other dashboards the user may open, and the nodes of the one picked."""
    from panel_flowdash.agent.yaml_format import node_names
    from panel_flowdash.editor import current_dashboard

    editor = current_dashboard()
    if editor is None or editor.store is None:
        return {}
    own = editor.dashboard.dashboard_id if editor.dashboard is not None else None
    dashboards = {
        model.title: model.dashboard_id
        for model in editor.store._all_dashboards()
        if model.dashboard_id != own and editor._dashboard_authorized(model, "view")
    }
    choices: dict[str, t.Any] = {"dashboard": dashboards}
    dashboard = config.get("dashboard")
    if dashboard in dashboards.values():
        model = editor.store._load_any(dashboard)
        if model is not None:
            names = node_names({"instance_id": item.instance_id} for item in model.items)
            choices["node"] = sorted(
                name
                for iid, name in names.items()
                if access(editor, model, dashboard, iid)[0] == "ok"
            )
    return choices


@register(
    page=False,
    component=True,
    title="Reference",
    icon="link",
    tags=["view"],
    description=(
        "A node of another dashboard, shown read only and kept in step with it: "
        "its outputs and its view, computed with that dashboard's own filters. "
        "Change the node in its own dashboard."
    ),
    config=["dashboard", "node"],
    config_choices=reference_choices,
)
class Reference(Viewer):
    """A node of another dashboard, shown and passed on read only."""

    dashboard = param.String(
        default="", label="Dashboard", doc="The id of the dashboard the node lives in."
    )

    node = param.String(
        default="",
        label="Node",
        doc="The node's name, as in the dashboard YAML, or its instance id.",
    )

    @classmethod
    def __flowdash_instance_spec__(cls, spec, config, editor):
        """Give a reference the outputs, and the size hints, of the node it shows."""
        source = _source_spec(editor, config.get("dashboard") or "", config.get("node") or "")
        if source is None:
            return spec
        return replace(
            spec,
            outputs=list(source.outputs),
            default_size=source.default_size or spec.default_size,
            min_size=source.min_size or spec.min_size,
            max_size=source.max_size or spec.max_size,
            framed=source.framed,
        )

    def __init__(self, **params):
        from panel_flowdash.editor import current_dashboard, current_node_id

        editor = current_dashboard()
        self._editor = weakref.ref(editor) if editor is not None else None
        self._node_id = current_node_id()
        self._depth = _RESOLVING.get()
        self._source: SourceGraph | None = None
        self._target: str | None = None
        self._bound: param.Parameterized | None = None
        self._release_view: t.Callable[[], None] | None = None
        self._holder = pn.Column(sizing_mode="stretch_both", margin=0)
        self._doc = _doc_ref()
        self._resolve_pending = False
        self.status = "unset"
        self.source_title: str | None = None
        super().__init__(**params)
        if editor is not None and editor.store is not None:
            # While it shows no source graph, e.g. a missing node, the store says when to look again.
            editor.store.watch_changes(self._on_store_change)
        self._resolve()

    def __panel__(self):
        """Return the view of the source node, or the placeholder saying why there is none."""
        return self._holder

    def __flowdash_provenance__(self) -> Provenance | None:
        """Say which dashboard the node comes from, or why it cannot be shown."""
        if self.status == "ok":
            return Provenance(
                label=f"From {self.source_title}",
                detail=(
                    f"{self.node} of the dashboard {self.source_title}, shown read only. "
                    "Change it there; this follows its saved version."
                ),
                kind="reference",
                icon="link",
            )
        if self.status == "missing":
            return Provenance(
                label=f"Missing node: {self.node}",
                detail="The node, or the dashboard it was in, was deleted.",
                kind="missing",
                icon="error_outline",
            )
        if self.status == "denied":
            return Provenance(
                label="No access",
                detail="You do not have access to the dashboard this node comes from.",
                kind="denied",
                icon="lock",
            )
        return None

    @property
    def _host(self) -> FlowDash | None:
        return self._editor() if self._editor is not None else None

    @param.depends("dashboard", "node", watch=True)
    def _resolve(self):
        """Find the node, attach to its dashboard's source graph and show it, or say why not.

        The graph shown before is let go only afterwards, so a graph the
        reference keeps using is not disposed and built again in between.
        """
        previous = (self._source, self._target)
        self._release()
        self._source = self._target = self._bound = None
        try:
            self._attach()
        finally:
            if previous[0] is not None:
                previous[0].detach(self, previous[1])
        if self.status == "ok":
            self._check_ports()

    def _attach(self):
        host = self._host
        if host is None or host.store is None or not (self.dashboard and self.node):
            self._set_status("unset")
            return
        key = (self.dashboard, self.node)
        if key in self._depth or len(self._depth) >= _MAX_DEPTH:
            self._set_status("missing")
            return
        status, _ = access(host, host.store._load_any(self.dashboard), self.dashboard, self.node)
        if status != "ok":
            self._set_status(status)
            return
        token = _RESOLVING.set((*self._depth, key))
        try:
            source = source_graph(host, self.dashboard)
            # Checked again on the model the graph loads, read now, since the
            # store may tell this reference of a change before the graph.
            source._read()
            status, target = access(host, source.model, self.dashboard, self.node)
            if status != "ok":
                if not source._references:
                    source.dispose()
                self._set_status(status)
                return
            self._source, self._target = source, target
            source.attach(self, target)
            if source.state(target) is None:
                self._set_status("missing")
                return
            self._show(source, target)
        except Exception:
            logger.exception("Showing %s of the dashboard %r failed", self.node, self.dashboard)
            self._set_status("missing")
        finally:
            _RESOLVING.reset(token)

    def _show(self, source: SourceGraph, target: str):
        state = source.state(target)
        self._bind_outputs(state)
        view, release = source.render(target)
        self._release_view = release
        self._bound = state
        self.source_title = source.title
        self._holder.objects = [view]
        self.status = "ok"

    def _check_ports(self):
        """Have the editor place the node again if the source node's outputs are no longer its ports.

        Only for a source that resolves: a node missing for now keeps its
        ports, so the edges from it stay and carry the failure.
        """
        from panel_flowdash.editor import current_node_id

        host = self._host
        if host is None or self._node_id is None or current_node_id() == self._node_id:
            return
        current = host._node_spec(self._node_id)
        base = host._component_specs.get(REFERENCE_COMPONENT)
        if current is None or base is None:
            return
        wanted = type(self).__flowdash_instance_spec__(
            base, {"dashboard": self.dashboard, "node": self.node}, host
        )
        ports = [(p.name, p.type) for p in wanted.outputs]
        if ports == [(p.name, p.type) for p in current.outputs]:
            return
        node_id = self._node_id
        if not _schedule(self._doc, partial(host._respec, node_id)):
            host._respec(node_id, {"dashboard": self.dashboard, "node": self.node})

    def _bind_outputs(self, state: param.Parameterized | None, failure: str | None = None):
        host = self._host
        if host is None or self._node_id is None:
            return
        node_state = host._dataflow_graph.get_state(self._node_id)
        spec = host._node_spec(self._node_id)
        if node_state is None or spec is None:
            return
        missing = (
            ReactiveError(LookupError(failure))
            if failure and host.propagate_failures and ReactiveError is not None
            else None
        )
        values = {}
        for port in spec.outputs:
            if port.name not in node_state.param:
                continue
            if state is not None and port.name in state.param:
                values[port.name] = state.param[port.name]
            else:
                values[port.name] = missing
        if values:
            node_state.param.update(values)

    def _set_status(self, status: str):
        self.status = status
        self.source_title = None
        if status == "missing":
            placeholder = Placeholder(
                "missing",
                f"Missing node: {self.node}",
                "The node, or the dashboard it was in, was deleted. "
                "Remove this reference or point it at another node.",
            )
            self._bind_outputs(None, placeholder.title)
        elif status == "denied":
            placeholder = Placeholder(
                "denied",
                "No access",
                "You do not have access to the dashboard this node comes from.",
                private=True,
            )
            self._bind_outputs(None, placeholder.title)
        else:
            placeholder = Placeholder(
                "unset",
                "Reference",
                "Pick the dashboard and the node to show.",
            )
            self._bind_outputs(None)
        self._holder.objects = [placeholder_view(placeholder)]

    def _release(self):
        if self._release_view is not None:
            release, self._release_view = self._release_view, None
            release()

    def _detach(self):
        self._release()
        source, target = self._source, self._target
        self._source = self._target = self._bound = None
        if source is not None:
            source.detach(self, target)

    def _on_store_change(self, dashboard_id: str):
        if dashboard_id != self.dashboard or self._source is not None or self._resolve_pending:
            return
        if self._doc is None:
            self._resolve_again()
            return
        self._resolve_pending = True
        if not _schedule(self._doc, self._resolve_again):
            self._resolve_pending = False

    def _resolve_again(self):
        self._resolve_pending = False
        host = self._host
        if host is None:
            return
        self._resolve()
        if self._node_id is not None:
            host.refresh_provenance([self._node_id])

    def _source_changed(self):
        """Follow a change to the source dashboard: keep the view if its node is the same."""
        host = self._host
        source = self._source
        if host is None:
            return
        model = source.model if source is not None else None
        status, target = access(host, model, self.dashboard, self.node)
        same = (
            status == "ok" and target == self._target and source.state(self._target) is self._bound
        )
        if same:
            self.source_title = source.title
        else:
            self._resolve()
        if self._node_id is not None:
            host.refresh_provenance([self._node_id])

    def dispose(self):
        host = self._host
        if host is not None and host.store is not None:
            host.store.unwatch_changes(self._on_store_change)
        self._detach()

# Enable the AI Agent

A FlowDash app can come with an AI agent that builds and changes dashboards for the
person using it: "add a map of the filtered turbines", "show the top 10 projects in a
table", "why is the capacity tile empty?". The agent reads dashboards in the
[YAML format](../reference/dashboard-yaml.md), edits the dashboard that is open, and
can do what the command palette offers. It always acts with the rights of the
signed-in user.

---

## Install and choose a model

FlowDash always installs [panel-agents](https://github.com/panel-extensions/panel-agents),
whose components it builds on. The agent also needs the `agent` extra, which adds
[pydantic-ai](https://ai.pydantic.dev), plus the client of the model provider you use:

```bash
pip install "panel-flowdash[agent]" "pydantic-ai-slim[openai]"
```

Name the model in the environment and serve the project as usual:

```bash
export OPENAI_API_KEY=...
export FLOWDASH_AGENT_MODEL=openai:<model>
flowdash serve my_project/
```

Any model pydantic-ai supports works, written as `<provider>:<model>`. Each provider
needs its client; for [OpenRouter](https://openrouter.ai), for example:

```bash
pip install "panel-flowdash[agent]" "pydantic-ai-slim[openrouter]"
export OPENROUTER_API_KEY=...
export FLOWDASH_AGENT_MODEL=openrouter:anthropic/<model>
```

Without a model configured, the app runs without an agent. If the extra is missing,
the server logs what to install instead of starting the agent. When a request to the
model fails, for example because the key is wrong or the account is out of credit,
the assistant answers with the provider's error in the chat and the server logs it
with its traceback.

---

## Let people pick the model

Offer several models and a small picker below the assistant's input lets each person
pick the one their next request runs with. List them in `FLOWDASH_AGENT_MODELS`,
comma-separated, each optionally with a label after `=`:

```bash
export FLOWDASH_AGENT_MODELS="openai:<model>=Fast,anthropic:<model>=Thorough"
flowdash serve my_project/
```

or repeat `--agent-model` on the command line (it also takes a comma-separated list),
which overrides the project's `agent_model`, `agent_models` and an `agent` model string:

```bash
flowdash serve my_project/ --agent-model "openai:<model>=Fast" --agent-model anthropic:<model>
```

or set `agent_models` in the project's `__init__.py` (or on `FlowDashApp`), where a
model can also carry a description, e.g. a cost hint:

```python
# my_project/__init__.py
agent_model = "openai:<model>"
agent_models = [
    {"id": "openai:<model>", "label": "Fast", "description": "Cheap, good for small edits"},
    {"id": "anthropic:<model>", "label": "Thorough"},
]
```

The default is `agent_model` (or `FLOWDASH_AGENT_MODEL`), which is offered first if it
is not in the list, and otherwise the first model listed. Each id is offered once. With a
single model, whether configured or the model of an `agent` you pass in, the chat
names it in the picker's place, with its full id as a tooltip (panel-agents after
0.1.0b11; earlier versions show nothing). An `agent` you pass in keeps running its own
model, and the chat shows the label of a single model listed in `agent_models`, or of
`agent_model` when that is the agent's model. `flowdash serve` refuses to
start when an entry is not a model, naming the entry. A switch applies from the next request and never to one
that is running, each person's pick is their own, and the browser remembers it for
their next visit while that model is still offered. A model whose provider package or
API key is missing is listed disabled, with the reason under its name, and a model's
description shows under its name too; keys are never shown. People who do not get the
assistant (see `agent_authorize`) see no picker. Each request records the model that answered, so the usage line under an
answer and its Details show it.

---

## Configure it in the project

A project's `__init__.py` can set the same options as `FlowDashApp`:

```python
# my_project/__init__.py
agent_model = "anthropic:<model>"
```

| Option | Meaning |
| --- | --- |
| `agent_model` | The model of the default agent. Defaults to `FLOWDASH_AGENT_MODEL`. |
| `agent_models` | Models people may pick from, see [Let people pick the model](#let-people-pick-the-model). Defaults to `FLOWDASH_AGENT_MODELS`. |
| `agent_model_settings` | pydantic-ai model settings for every request, merged over the agent's own, e.g. `{"temperature": 0}`. |
| `agent` | `None` (default): the default agent when a model is configured. `True`: the default agent, and an error saying how to configure a model when none is. A string: the default agent with that model. `False`: no agent, even if a model is configured. A `pydantic_ai.Agent`: your own agent. |
| `agent_authorize` | Called with the signed-in user's `Identity`; only sessions for which it returns True get the agent. |
| `agent_sensitive_components` | Component ids or glob patterns (`"Sources/*"`) of components whose config can reach outside the dashboard, see [Untrusted text](#untrusted-text). |
| `agent_approval_rules` | More calls that need the user's approval: tool names such as `"remove_node"`, or `panel_agents.ApprovalRule("run_command", when=lambda args: ...)` for calls with particular arguments. Saving, deleting and sharing always need it. |

To give the default agent knowledge about your project, build it yourself and pass
extra instructions:

```python
# my_project/__init__.py
from panel_flowdash.agent import create_agent

agent = create_agent(
    "anthropic:<model>",
    instructions="Capacities are in MW. Prefer the turbine map for anything spatial.",
)
```

By default the agent's instructions list the components the user may place, with
their ports and config fields, so you do not need to describe those. `create_agent`
also takes pydantic-ai `model_settings`.

An agent added for one narrow job, e.g. answering questions about data with tools of
your own, does not need all the dashboard tools, and every tool and line of
instructions is sent with each request. `create_agent` takes three options for that:

- `tools`: the names of the dashboard tools to offer, e.g. `["set_config",
  "remove_node"]`. It selects tools; approvals and the user's rights apply as before.
- `default_instructions=False`: leave out the instructions about the YAML format and
  how to work on a dashboard, which name every dashboard tool. The agent still gets
  the rules about saving, refusals and untrusted text after your own `instructions`,
  and, when it is offered `replace_dashboard`, the rule to keep the `# revision:`
  line of the YAML `get_dashboard` returns, which `replace_dashboard` needs.
- `catalogue`: `"full"` (the default), `"compact"` for component ids and titles only,
  or `"none"`. Without `list_components` and with `"none"` the agent cannot discover
  components.

For such an agent in the app, also pass `FlowDashApp(agent_palette=False)` to leave
out the command palette tools:

```python
agent = create_agent(
    "anthropic:<model>",
    instructions="You answer questions about the data with the data tools ...",
    toolsets=[my_data_tools],
    tools=["set_config", "remove_node"],
    default_instructions=False,
    catalogue="none",
)
```

---

## What the agent can do

The robot button in the top bar opens the assistant: a drawer beside the page with
the chat, the versions of the dashboard the agent published and the calls waiting for
approval. It stays open while you move between dashboards. The command palette
(Ctrl+K) also offers "Ask the assistant" for whatever you typed; a question asked
while the agent is busy is sent once it is done. In full screen, a call waiting for
approval shows a notice whose Review button leaves full screen and opens the drawer.
The chat names each finished step in the dashboard's terms ("Connect filter to map",
"Change kpi: label") and folds it away. In windows narrower than 1200 pixels the
drawer opens over the page instead of beside it. The agent's edits appear on the user's canvas as they happen, as unsaved changes:
a dashboard shown in view mode switches to edit mode, and Save, Discard and the
unsaved-changes review work as for the user's own edits. The nodes and tiles the
agent added (green) or changed (amber) during a request stay outlined until the next
request or until the user leaves edit mode.

- **Read**: list the components and dashboards, get a dashboard as YAML, validate
  YAML and preview a dashboard without showing it, getting each node's status and
  small samples of its outputs.
- **Edit**: add and remove nodes, change config, connect and disconnect ports, set
  edge transforms, lay out tiles, change settings, or replace the whole dashboard
  with YAML.
- **Drive the page**: run any command the command palette offers at that moment,
  such as opening a dashboard, refreshing or switching to view mode.
- **Store**: save, delete and share dashboards, each only after the user approves
  the call. The save approval lists what is stored and calls out changes of who can
  see the dashboard's data. If the agent changes the dashboard between asking and
  saving, the save is refused and the agent has to ask again, so what the user
  approved is what is stored. Delete and share approvals name the dashboard's title.
- **Share**: add viewers and editors and take people off the list. A private
  dashboard stays private unless the call sets `everyone`; blocked people stay blocked
  unless the call names them in `unblock`; both show in the approval. Empty names
  and `group:` without a group are refused.

The drawer's artifact view shows the dashboard's
[edit history](persist-dashboards.md#undo-redo-and-the-edit-history): one version for
each request to the agent and one for each burst of the user's own edits. The user can compare
versions, see what changed in the dashboard's terms ("Connected filter.filtered →
map.turbines", "kpi: metric: turbines → capacity"), undo a whole request, and go back
to or forward to any version, with the same rights checks as the editor's own undo. A
restored version's config checks run afterwards, and a warning names the nodes that fail.
The agent cannot undo or redo through the command palette; those commands are the
user's. With `editor_options={"history": False}` each tool call publishes a version
instead, and applying one that predates the user's own edits asks first.

A node the agent added offers **How was this made?** in its tile's More menu in view
mode, and a *History* tab in the sidecar when it is selected while editing. Both show
the request that created the node, in a side sheet in view mode; a sheet still open
when you press Edit hands over to the *History* tab. They show
the question, every model call (model, tokens, the share read from the cache, duration
and failed attempts) and tool call with its arguments and result, and the versions of
the dashboard it published. Below it, "Changed later by 2 requests" counts the later
turns whose summary names the node, with a link to each request whose calls are still
in the session. The same view opens from the **Details** link under each answer in the
chat and from **How was this made?** on a request in the version history. A node knows
its request through its `origin` turn, which names the agent's run.

Model and tool calls are kept in memory for the session that made them. After a reload,
or in another session, the view shows what the dashboard file records instead: the
question, when it was asked and what it changed. Your own requests are always shown in
full. Someone else's are shown without their content (which tools and models ran, with
tokens and timings, but no question, summary, arguments, results, messages or errors)
to people who may only view the dashboard, and to anyone who may not use the component
of a node the request created. Set `editor_options={"provenance": False}` to leave the
action out.

### Explore

**Explore**, next to View and Edit (and first among the Dataflow, Layout and Split tabs
in edit mode), shows the dashboard as a conversation: one entry per turn, the question,
the assistant's steps and answer, and below it a card for each node the turn created,
showing that node live. A view or control shows what its tile shows; a data source or
transform shows its output as a table. **Add to dashboard** on a card places the node's
tile on the grid, and "On dashboard" says it is there already. A card whose node a later
turn changed says so ("Changed later by 1 manual edit"), and a batch of manual edits is
an entry of its own listing what changed. Nodes made before the dashboard recorded turns
are listed, collapsed, in a first entry, and open one at a time.

The question box at the bottom asks the Explore agent, which is separate from the
assistant in the drawer: Explore is for questions about the data, the drawer for
changing the dashboard. Each keeps its own conversation, so a question asked in one does
not show in the other, while a model picked in either is used by both. With an Explore
agent the feed holds the requests asked in Explore, including those of earlier
sessions; the dashboard's other turns are listed, collapsed, in an "Earlier requests and
edits" entry at the top. Requests made in
this session show every step; after a reload, or for requests made by others, an entry
shows what the dashboard file records, with the same redaction as **How was this
made?**. While Explore is shown the drawer stays closed, and an approval the Explore
agent asks for is answered in the feed. An approval waiting on the surface out of sight
shows a notice whose Review button brings it back, and leaving Explore with an approval
of the drawer's pending opens the drawer on it. People who may only view the dashboard get the feed
without the question box and without **Add to dashboard**.

The Explore agent's answers stay off the dashboard until someone adds them. Each
session builds them in an exploration of the open dashboard: a hidden editor holding
a mirror of each of the dashboard's nodes, whose outputs follow the dashboard node's,
so an answer built on a filtered table follows the dashboard's widgets. The agent can
read the dashboard's nodes and connect its own nodes to them, but cannot change them or
lay out, save or share anything, and an Explore request makes no turn of the dashboard
and no step of its Undo. It may add a data source the host offers as it is, without
config of its own (`ExplorationOps(allow_source=...)` changes the rule), and is told to
build on the dashboard's source when it has one with the same config rather than load
it again. A mirror follows
its dashboard node again when an Undo brings it back. Only the open dashboard's
exploration is kept, with the answers of its latest 20 requests; an older answer keeps
its card, which asks to ask the question again. Each answer of a request (a node it made
that none of its other nodes reads) gets a card, with the nodes it is built from
collapsed under it. Earlier answers stay as they are: a follow-up that changes one of
their nodes changes a copy of it, and of the earlier nodes it is built from, named like
`total_2`, which becomes an answer of its own with its own card
(`ExplorationOps(freeze_answers=False)` lets a follow-up change them in place). A card says when
a dashboard node it is built on changed or was removed since, and keeps showing that
node's last output once it is removed. Answers last as long as the session.

**Add to dashboard** on an answer's card copies the answer and the nodes it is built from
onto the dashboard, connected to the dashboard nodes it reads. A node the dashboard
already has, with the same component, config and inputs, is reused, and a name the
dashboard uses for another node gets a `_2` suffix. Only the answer's tile is placed on
the grid. The copy is one turn of the Explore request, with its question, and one step of
Undo; the card then says "On dashboard as `name`", and **Show** goes to the dashboard and
marks the tile. When the dashboard shows that answer already, nothing is added and the
card says "Already on dashboard as `name`". A failed addition takes back what it wrote.

Only one request at a time edits the dashboard: a request takes it at its first edit and
keeps it until it ends, so each request stays one step of Undo. Adding an answer from
Explore while the assistant's request is editing waits, saying "Waiting for the
assistant's edit to finish", and the assistant waits for an answer being added the same
way. After a few minutes the wait gives up and says so.

`FlowDashApp(explore_agent=...)` chooses the Explore agent. By default it is
`panel_flowdash.agent.create_explore_agent` on the assistant's model: it reads the
dashboard and adds nodes of its own in the exploration, and is told to answer from them.
Pass your own pydantic-ai `Agent` taking `FlowDashDeps` to use it as is (its
`ctx.deps.ops` is an `ExplorationOps`), or `False` to turn asking in Explore off.
Without an Explore agent (or without any agent) Explore shows every recorded turn of the
dashboard, as a record of how it was made.

### Report

The assistant writes the dashboard's report, the document the **Report** view shows,
with these tools: `get_report` returns its blocks as YAML, each numbered by its index,
`report_text` returns it as a reader sees it, in Markdown with the `{{ node.port }}`
placeholders filled in, `edit_report` inserts, updates, moves and removes blocks by
index in one call, `set_report` replaces the whole report, `ask_in_report` adds a
question without answering it, and `run_pending` answers such a question in the
current request. A dashboard without a report of its own starts from the one derived
from its groups and tiles. `get_report` starts with a `# revision:` line; `edit_report`
given that token, and `set_report` given YAML keeping the line, refuse the edit if the
report changed since, so a person's edit never shifts the indices under the agent. The
report's text is handed to the model between random markers, as data written by people.
The edits are checked like `replace_dashboard` (blocks must
name nodes, groups and turns the dashboard has; a placeholder naming no output is a
warning), need no approval, are one step of Undo per request, and are listed in the
request's summary ("Wrote a paragraph in the report. Added revenue_kpi to the
report."). Each block the agent writes or changes records its turn as `origin`. The
agent is told to put prose about a dashboard in the report, as text blocks with live
placeholders, rather than in text tiles, unless asked for a tile.

A question not run yet shows in the report with a **Run** button. For people who may
edit the dashboard, Run sends the question to the assistant as an ordinary request,
shown in the drawer's chat; the request answers the question's turn, so the nodes it
builds get that turn as their origin and fill the question's block. From the click until
the request ends the button reads "Running" and cannot be pressed again, so a question is
asked once. A request that changes nothing leaves the question waiting. Questions run only in the app. Without
an assistant, or for people who may only view the dashboard, Run is disabled;
`FlowDash(run_pending=...)` takes your own callable, which answers through
`FlowDash.answer_pending(turn_id, run_id)`.

`export_report(format, include_pending)` exports the report as a file the user's
browser saves, as **Export...** in the Report view does: `html`, `pdf`, `docx`, `md` or
`ipynb`. A file leaves the app, so every export waits for the user's approval, and
questions not run yet are never run for it.

An agent given a subset of the tools (`create_agent(tools=...)`) gets the report tools
by name; `panel_flowdash.agent.REPORT_TOOLS` lists them. When it is offered
`edit_report` or `set_report`, its own instructions are followed by the rule about
where prose goes.

Each request to the agent stays on the dashboard that was open when the user sent it:
if the user opens another dashboard while the agent works, every further tool call
and palette command of that request is refused, so the agent stops instead of
changing the dashboard on screen. Only a dashboard the agent opens itself, at the
user's request, becomes the one it works on. When the
agent rewrites a whole dashboard from YAML it read earlier and the user has edited the
dashboard since, the rewrite is refused and the agent reads it again.

---

## Rights

The agent can never do more than the user it works for. Its tools go through the same
checks as the app:

- Components the user may not use are left out of the agent's catalogue and cannot be
  placed. On a dashboard they appear as `flowdash:restricted` stand-ins: the agent
  cannot read or change their config or connections, sees which inputs they feed
  (and cannot connect anything else to those), but can remove such a node, as the
  user can on the canvas.
- Dashboards the user may not open look missing to the agent. On a dashboard the user
  may only view, every edit is refused. Edit rights revoked from another session apply
  to the next edit.
- A viewer's agent only gets the YAML, node details and output samples of a dashboard
  when the dashboard lets viewers inspect its outputs, as for Download and Inspect.
- Deleting and sharing need the same administrator rights as in the app.

---

## Untrusted text

Dashboard titles, descriptions, config values, column names and data reach the agent
as it works, and other people may have written them, so a dashboard can carry text
that tries to instruct the agent ("prompt injection"). The agent is told to treat all
of it as data, also with `default_instructions=False`, but plan for it to be fooled
sometimes:

- Saving, deleting and sharing always need the user's approval, and the approvals say
  what will happen. Read them before approving.
- Unapproved edits only change the open canvas, which the user sees and can discard.
- Components run as soon as they are placed, with the user's rights. If a component's
  config can reach outside the dashboard (a SQL source, a URL fetch, an expression),
  list it in `agent_sensitive_components`. Then each of these needs the user's
  approval: placing it with a tool or from the command palette, changing its config,
  connecting or transforming an edge into one of its inputs, and previewing or
  loading YAML that adds, removes or renames one or changes its component, config or
  incoming edges. YAML that leaves every such node as it is on the canvas runs without
  asking, however its values are written. Former ids of a renamed component (its
  `aliases`) count as the component.

---

## Use the tools in your own agent

The operations are plain Python, so they also work without the default agent or
without a page. `DashboardOps` edits through any `FlowDash` editor with that
editor's rights:

```python
from panel_flowdash import FlowDash
from panel_flowdash.agent import DashboardOps

editor = FlowDash("my_project/", store="dashboards.db", user="alice")
ops = DashboardOps(editor)
# Refuses while the editor has unsaved changes, unless discard=True.
ops.open_dashboard("wind-overview")
ops.add_node("Views/kpi", {"metric": "capacity"}, name="total_capacity")
ops.connect("filter.filtered", "total_capacity.turbines")
print(ops.preview()["nodes"]["total_capacity"]["status"])
```

Without a `position`, `add_node` lets the editor place the node by its dataflow and its
tile with the tiles of its kind, see
[Automatic arrangement](layout-and-sizing.md#automatic-arrangement), and
`ops.arrange_canvas()` and `ops.arrange_tiles()` rearrange everything on request;
`ops.arrange_tiles(order=[...], style="stack")` also puts named tiles first or stacks them.
Without `name`, `add_node` gives the node a generated name such as `kpi_3fa2c1` and
returns it. A chosen name follows the rules for
[node names](../reference/dashboard-yaml.md#nodes) and is at most 64 characters long,
and a name another node has is refused with a free variant suggested.

Tools of your own can read the dashboard through the same rights: `ops.nodes()` lists
each node's name and component, `ops.node_config(name)` returns a copy of a node's
config, and `ops.node_output("total_capacity.value")` the live value of an output,
which must not be changed in place. A node that waits for an input or whose input
failed raises `OutputUnavailable` with its `status`, as `preview` reports it; a
dashboard tool that raises it reports it to the model instead of ending the run.

`dashboard_toolset()` wraps them as a pydantic-ai toolset that runs against
`FlowDashDeps(ops)`. The editor's edit history publishes every edit as a version, so
give your `AgentRunner` the editor's `edit_history.state` to see them in its chat; `dashboard_toolset(["get_dashboard", "set_config"])` offers only
those tools:

```python
from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

agent = Agent("openai:<model>", deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
result = agent.run_sync("Add a KPI of the total capacity", deps=FlowDashDeps(ops))
```

Run it through a panel-agents `AgentRunner` to have the user approve every save,
delete and sharing change. `approval_policy` takes rules of your own, and with
`sensitive_components` also needs the operations, to resolve component ids:

```python
from panel_agents import RunState
from panel_agents.adapters.pydantic_ai import AgentRunner

from panel_flowdash.agent import approval_policy

deps = FlowDashDeps(ops)
runner = AgentRunner(
    agent,
    RunState(),
    deps=deps,
    approval_policy=approval_policy(sensitive_components=["Sources/sql"], ops=ops),
)
```

Call `deps.start_request()` when the user sends a request and `deps.end_request()`
when it is done, so each request keeps to the dashboard it was sent on.

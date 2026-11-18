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

## Configure it in the project

A project's `__init__.py` can set the same options as `FlowDashApp`:

```python
# my_project/__init__.py
agent_model = "anthropic:<model>"
```

| Option | Meaning |
| --- | --- |
| `agent_model` | The model of the default agent. Defaults to `FLOWDASH_AGENT_MODEL`. |
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
request.

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

Every edit publishes the dashboard's YAML as a new version. The user can compare
versions, see what changed in the dashboard's terms ("Connected filter.filtered →
map.turbines", "kpi: metric: turbines → capacity") and apply an earlier or later
version, which loads it like an imported file. If the dashboard has changes made
after the agent's last edit, the user confirms that applying the version drops them.

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
`FlowDashDeps(ops)`; `dashboard_toolset(["get_dashboard", "set_config"])` offers only
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

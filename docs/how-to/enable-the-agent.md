# Enable the AI Agent

A FlowDash app can come with an AI agent that builds and changes dashboards for the
person using it: "add a map of the filtered turbines", "show the top 10 projects in a
table", "why is the capacity tile empty?". The agent reads dashboards in the
[YAML format](../reference/dashboard-yaml.md), edits the dashboard that is open, and
can do what the command palette offers. It always acts with the rights of the
signed-in user.

---

## Install and choose a model

The agent needs the `agent` extra, which brings in
[panel-agents](https://github.com/panel-extensions/panel-agents) and
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

Any model pydantic-ai supports works, written as `<provider>:<model>`. Without a
model configured, the app runs without an agent.

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
| `agent` | `None` (default): the default agent when a model is configured. A string: the default agent with that model. `False`: no agent, even if a model is configured. A `pydantic_ai.Agent`: your own agent. |

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

The agent's instructions always list the components the user may place, with their
ports and config fields, so you do not need to describe those.

---

## What the agent can do

The robot button in the top bar opens the assistant: a drawer beside the page with
the chat, the versions of the dashboard the agent published and the calls waiting for
approval. It stays open while you move between dashboards. The agent's edits appear on the user's canvas as they happen, as unsaved changes:
a dashboard shown in view mode switches to edit mode, and Save, Discard and the
unsaved-changes review work as for the user's own edits.

- **Read**: list the components and dashboards, get a dashboard as YAML, validate
  YAML and preview a dashboard without showing it, getting each node's status and
  small samples of its outputs.
- **Edit**: add and remove nodes, change config, connect and disconnect ports, set
  edge transforms, lay out tiles, change settings, or replace the whole dashboard
  with YAML.
- **Drive the page**: run any command the command palette offers at that moment,
  such as opening a dashboard, refreshing or switching to view mode.
- **Store**: save, delete and share dashboards, each only after the user approves
  the call.

Every edit publishes the dashboard's YAML as a new version. The user can compare
versions, see what changed in the dashboard's terms ("Connected filter.filtered →
map.turbines", "kpi: metric: turbines → capacity") and apply an earlier or later
version, which loads it like an imported file.

---

## Rights

The agent can never do more than the user it works for. Its tools go through the same
checks as the app:

- Components the user may not use are left out of the agent's catalogue and cannot be
  placed. On a dashboard they appear as `flowdash:restricted` stand-ins whose config
  and connections the agent can neither read nor change.
- Dashboards the user may not open look missing to the agent. On a dashboard the user
  may only view, every edit is refused. Edit rights revoked from another session apply
  to the next edit.
- A viewer's agent only gets the YAML and output samples of a dashboard when the
  dashboard lets viewers inspect its outputs, as for Download and Inspect.
- Deleting and sharing need the same administrator rights as in the app.

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
ops.open_dashboard("wind-overview")
name = ops.add_node("Views/kpi", {"metric": "capacity"})
ops.connect("filter.filtered", f"{name}.turbines")
print(ops.preview()["nodes"][name]["status"])
```

`dashboard_toolset()` wraps them as a pydantic-ai toolset that runs against
`FlowDashDeps(ops)`:

```python
from pydantic_ai import Agent

from panel_flowdash.agent import FlowDashDeps, dashboard_toolset

agent = Agent("openai:<model>", deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
result = agent.run_sync("Add a KPI of the total capacity", deps=FlowDashDeps(ops))
```

Run it through a panel-agents `AgentRunner(agent, deps=..., approval_policy=approval_policy())`
to have the user approve every save, delete and sharing change.

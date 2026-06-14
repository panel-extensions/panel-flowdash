# Viewer Components

This example shows how to define FlowDash components as Panel `Viewer`
subclasses. Ports are introspected automatically from params and
`@param.output` decorators.

---

## Components

### Ticker Selector

A selector component that outputs a stock ticker symbol:

```python title="Finance/selector.py"
import param
import panel as pn
from panel_flowdash import register

@register(component=True, title="Ticker Selector")
class app(pn.viewable.Viewer):
    ticker = param.Selector(
        default="AAPL",
        objects=["AAPL", "GOOG", "MSFT", "AMZN"],
    )

    @param.output(param.String)
    def selected_ticker(self):
        return self.ticker

    def __panel__(self):
        return pn.widgets.Select.from_param(self.param.ticker)
```

**Ports discovered:**

- Inputs: `ticker` (Selector)
- Outputs: `selected_ticker` (String)

### Price Display

A display component that receives a ticker and shows a price:

```python title="Finance/price.py"
import param
import panel as pn
from panel_flowdash import register

@register(component=True, title="Price Display")
class app(pn.viewable.Viewer):
    ticker = param.String(default="")

    def __panel__(self):
        return pn.pane.Markdown(
            pn.rx("## {ticker}\n\nPrice: $---").format(ticker=self.param.ticker)
        )
```

**Ports discovered:**

- Inputs: `ticker` (String)
- Outputs: none

---

## Wiring

When placed on a dashboard, connecting `selector.selected_ticker` to
`price.ticker` will update the price display whenever the selector changes.

The dataflow engine validates the connection:

- Type check: `String` output matches `String` input
- No cycles: selector -> price is acyclic
- Single source: `price.ticker` can only have one incoming connection

---

## Running standalone

```python title="standalone.py"
from panel_flowdash import DataflowGraph, ComponentSpec, OutputPort, InputPort

specs = {
    "selector": ComponentSpec(
        component_id="selector",
        title="Selector",
        description=None,
        icon=None,
        tags=[],
        outputs=[OutputPort(name="selected_ticker", type="String")],
        inputs=[InputPort(name="ticker", type="Selector", required=False, blocking=False)],
        default_size=None,
    ),
    "price": ComponentSpec(
        component_id="price",
        title="Price",
        description=None,
        icon=None,
        tags=[],
        outputs=[],
        inputs=[InputPort(name="ticker", type="String", required=False, blocking=False)],
        default_size=None,
    ),
}

graph = DataflowGraph(specs)
graph.add_node("sel_1", "selector")
graph.add_node("price_1", "price")

result = graph.add_edge("sel_1", "selected_ticker", "price_1", "ticker")
assert result is True

# Propagation works
state = graph.get_state("sel_1")
state.selected_ticker = "GOOG"
assert graph.get_state("price_1").ticker == "GOOG"
```

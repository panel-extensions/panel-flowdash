# Simple Dataflow Example

Three components that can be wired together in the dashboard editor:

- **Category Selector**: outputs a category string
- **Item Counter**: outputs a numeric count
- **Category Display**: receives and displays a category

## Run

```bash
flowdash serve examples/simple_dataflow/
```

Open the Components route, add the selector and display to the canvas,
then draw an edge from the selector's `category` output to the display's
`category` input.

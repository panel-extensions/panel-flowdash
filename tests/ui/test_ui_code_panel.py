"""UI tests for the code panel docked under the canvas."""

import os

import pytest
from panel.tests.util import wait_until

from tests.test_code_panel import make_editor, make_lazy_editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

VIEWPORT = {"width": 1440, "height": 900}
SHOTS = os.environ.get("FLOWDASH_CODE_PANEL_SHOTS")


def _open(page, editor, query):
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    panel = page.locator(".fd-code-panel")
    expect(panel).to_be_visible(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    return panel, code


def _type_at_end(page, code, text):
    code.locator(".ace_content").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(text, delay=60)


_SIDEWAYS = """() => {
  const found = []
  function walk(root) {
    for (const el of root.querySelectorAll('*')) {
      const style = getComputedStyle(el)
      const scrolls = ['auto', 'scroll'].includes(style.overflowX)
      const inside = el.closest('.fd-code-result, .ace_editor, .ace_autocomplete')
      if (scrolls && !inside && el.scrollWidth > el.clientWidth + 1) {
        found.push(`${el.tagName}.${String(el.className).slice(0, 60)} ${el.scrollWidth}>${el.clientWidth}`)
      }
      if (el.scrollLeft > 0 && !inside) found.push(`scrolled ${el.tagName}.${String(el.className).slice(0, 60)}`)
      if (el.shadowRoot) walk(el.shadowRoot)
    }
  }
  walk(document)
  const doc = document.scrollingElement
  if (doc.scrollWidth > doc.clientWidth + 1) found.push('page')
  return found
}"""

_REACHABLE = """(selector) => {
  function find(root) {
    const el = root.querySelector(selector)
    if (el) return el
    for (const child of root.querySelectorAll('*')) {
      if (child.shadowRoot) { const hit = find(child.shadowRoot); if (hit) return hit }
    }
    return null
  }
  const el = find(document)
  if (!el) return 'missing'
  const box = el.getBoundingClientRect()
  if (!box.width || box.right > window.innerWidth || box.bottom > window.innerHeight) return 'outside'
  let hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    if (!inner || inner === hit) break
    hit = inner
  }
  let node = hit
  while (node) {
    if (node === el) return 'ok'
    node = node.parentNode || node.host
  }
  return 'covered'
}"""


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


def test_opened_from_the_inspector_under_the_canvas(page):
    editor, query = make_editor()
    panel, _code = _open(page, editor, query)
    assert editor.code_panel_open
    canvas = page.locator(".react-flow").first.bounding_box()
    box = panel.bounding_box()
    assert box["y"] >= canvas["y"] + canvas["height"] - 1
    assert box["x"] < canvas["x"] and box["x"] + box["width"] > canvas["x"] + canvas["width"]
    assert box["x"] + box["width"] <= page.viewport_size["width"] + 1
    assert box["y"] + box["height"] <= page.viewport_size["height"]
    wait_until(lambda: editor._flow.selection["nodes"] == [query], timeout=8000)
    expect(page.locator(".flowdash-inspector, .fd-edit-spec").first).to_be_visible()
    expect(page.locator(".fd-code-dialog .fd-code-field-large")).to_have_count(0)
    # The first result renders after the inspector and the panel, which takes a while under load.
    expect(page.locator(".fd-code-table")).to_contain_text("2016", timeout=15000)
    _shot(page, "open")


def _middle(box):
    return box["y"] + box["height"] / 2


def test_the_result_header_fits_the_toggle_beside_the_summary(page):
    editor, query = make_lazy_editor()
    _open(page, editor, query)
    result = page.locator(".fd-code-panel-result")
    strip = result.locator(".fd-cp-subheader").first
    toggle = result.locator(".fd-code-outputs .MuiToggleButtonGroup-root")
    summary = result.locator(".fd-cp-subheader .fd-cp-bar")
    expect(toggle).to_be_visible(timeout=8000)
    expect(summary).to_contain_text("5 rows", timeout=15000)
    strip_box, toggle_box = strip.bounding_box(), toggle.bounding_box()
    text = summary.bounding_box()
    assert toggle_box["y"] >= strip_box["y"] and (
        toggle_box["y"] + toggle_box["height"] <= strip_box["y"] + strip_box["height"]
    )
    assert text["x"] >= toggle_box["x"] + toggle_box["width"]
    assert abs(_middle(text) - _middle(toggle_box)) <= 1.5
    assert abs(_middle(toggle_box) - _middle(strip_box)) <= 1.5
    expect(toggle.get_by_role("button", name="Rows")).to_have_attribute("aria-pressed", "true")


def test_the_table_fits_its_columns_without_the_index(page):
    editor, query = make_editor()
    _open(page, editor, query)
    table = page.locator(".fd-code-table table")
    expect(table).to_contain_text("2016", timeout=15000)
    headers = table.locator("thead th")
    expect(headers).to_have_count(3)
    assert [h.strip() for h in headers.all_inner_texts()] == ["p_year", "p_name", "p_cap"]
    expect(table.locator("tbody th")).to_have_count(0)
    result = page.locator(".fd-code-panel-result").bounding_box()
    assert table.bounding_box()["width"] < result["width"] / 2


def test_the_code_footer_is_on_one_centred_line(page):
    editor, query = make_editor()
    _open(page, editor, query)
    footer = page.locator(".fd-code-footer-bar")
    icon = footer.locator(".fd-cp-ok .fd-cp-icon")
    expect(icon).to_be_visible(timeout=8000)
    middle = _middle(footer.bounding_box())
    label = footer.locator(".fd-cp-ok")
    position = footer.get_by_text("Ln ").filter(visible=True)
    for part in (icon, label, position):
        assert abs(_middle(part.bounding_box()) - middle) <= 1.5


def _drag_split(page, editor, by=200):
    """Drag the gutter between code and result by *by* pixels and wait for the sizes to sync."""
    code = page.locator(".fd-code-panel-code")
    expect(page.locator(".fd-code-table")).to_contain_text("2016", timeout=15000)
    gutter = page.locator(".fd-code-panel-body .gutter")
    expect(gutter).to_be_visible(timeout=15000)
    # The split shows once laid out; wait for its width to settle before measuring.
    wait_until(lambda: code.bounding_box() and code.bounding_box()["width"] > 200, timeout=15000)
    before = code.bounding_box()["width"]
    box = gutter.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(x + by * step / 10, y)
        page.wait_for_timeout(20)
    page.mouse.up()
    wait_until(lambda: code.bounding_box()["width"] > before + by * 0.75, timeout=15000)
    wait_until(lambda: editor.code_panel._body.sizes[0] > 45, timeout=20000)
    return code.bounding_box()["width"]


def test_the_split_between_code_and_result_is_dragged(page):
    editor, query = make_editor()
    _open(page, editor, query)
    code = page.locator(".fd-code-panel-code")
    result = page.locator(".fd-code-panel-result")
    expect(page.locator(".fd-code-table")).to_contain_text("2016", timeout=15000)
    assert result.bounding_box()["width"] > code.bounding_box()["width"]
    _drag_split(page, editor)
    assert page.evaluate(_SIDEWAYS) == []


def test_the_dragged_split_survives_switching_nodes_and_collapsing(page):
    editor, query = make_editor()
    other = editor.add_component("D/query", position=(320, 300))
    source = next(e["source"] for e in editor._dataflow_graph.edges if e["target"] == query)
    editor.connect(source, "data", other, "data")
    _open(page, editor, query)
    code = page.locator(".fd-code-panel-code")
    dragged = _drag_split(page, editor)
    editor.open_code_panel(other, "expr")
    wait_until(lambda: editor.code_panel.instance_id == other, timeout=15000)
    expect(page.locator(".fd-code-panel-header")).to_contain_text(
        editor._node_name(other), timeout=15000
    )
    wait_until(lambda: abs(code.bounding_box()["width"] - dragged) <= 2, timeout=15000)
    page.locator(".fd-code-collapse").click()
    expect(page.locator(".fd-code-expand-panel")).to_be_visible(timeout=15000)
    page.locator(".fd-code-expand-panel").click()
    expect(code).to_be_visible(timeout=15000)
    wait_until(lambda: abs(code.bounding_box()["width"] - dragged) <= 2, timeout=15000)


def test_typing_is_held_until_run(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_cap > 2")
    expect(page.locator(".fd-code-panel-result")).to_contain_text(
        "Edited since the last run", timeout=8000
    )
    page.wait_for_timeout(500)
    assert editor._component_instances[query].expr == "p_year > 2014"
    page.locator(".fd-code-run").click()
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap > 2",
        timeout=8000,
    )
    expect(page.locator(".fd-code-panel-result")).to_contain_text("2 rows", timeout=8000)


def test_ctrl_enter_runs(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, "5")
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(lambda: editor._component_instances[query].expr == "p_year > 20145", timeout=8000)
    expect(page.locator(".fd-code-panel-result")).to_contain_text("0 rows", timeout=8000)


def test_a_check_error_shows_under_its_line_and_keeps_the_result(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, "\nand p_yaer < 2016")
    page.keyboard.press("ControlOrMeta+Enter")
    message = code.locator(".fd-code-diagnostic")
    expect(message).to_contain_text("There is no column p_yaer.", timeout=8000)
    expect(code.locator(".fd-ace-error")).to_have_count(1)
    expect(code.locator(".ace_gutter-cell.ace_error")).to_have_count(1)
    wait_until(
        lambda: (
            message.bounding_box()["y"]
            >= code.locator(".ace_line").nth(1).bounding_box()["y"]
            + code.locator(".ace_line").nth(1).bounding_box()["height"]
            - 1
        ),
        timeout=8000,
    )
    result = page.locator(".fd-code-panel-result")
    expect(result).to_contain_text("Not run, the check failed", timeout=15000)
    expect(result.locator(".fd-code-result-stale")).to_have_count(1, timeout=8000)
    expect(result).to_contain_text("5 rows", timeout=8000)
    assert editor._component_instances[query].expr == "p_year > 2014"
    _shot(page, "error")
    code.locator(".fd-code-fix").click()
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014\nand p_year < 2016",
        timeout=8000,
    )
    expect(message).to_have_count(0, timeout=8000)


def _clear_of_the_next_line(code, message):
    """Whether *message*, under the first line, ends above the second line's text."""
    box = message.bounding_box()
    nxt = code.locator(".ace_line").nth(1).bounding_box()
    return box is not None and nxt is not None and box["y"] + box["height"] <= nxt["y"]


def test_a_wrapped_error_message_never_covers_the_next_line(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, "\nand p_year < 2016")
    # Every keystroke synced updates the problems shown, which would drop the one set here.
    wait_until(
        lambda: editor.code_panel.code.value_input == "p_year > 2014\nand p_year < 2016",
        timeout=8000,
    )
    long = "The query reads 'turbines', which is not a table of this source. " * 3
    editor.code_panel.tools.diagnostics = [{"row": 0, "column": 0, "text": long}]
    message = code.locator(".fd-code-diagnostic")
    expect(message).to_have_count(1, timeout=8000)
    wait_until(lambda: _clear_of_the_next_line(code, message), timeout=8000)
    height = message.bounding_box()["height"]

    def message_height():
        box = message.bounding_box()
        return box["height"] if box else 0

    # A narrower editor, as when the assistant drawer opens, wraps the message onto more lines.
    page.set_viewport_size({"width": 960, "height": VIEWPORT["height"]})
    wait_until(lambda: message_height() > height + 10, timeout=8000)
    wait_until(lambda: _clear_of_the_next_line(code, message), timeout=8000)
    page.set_viewport_size(VIEWPORT)
    wait_until(lambda: 0 < message_height() <= height + 1, timeout=8000)
    wait_until(lambda: _clear_of_the_next_line(code, message), timeout=8000)

    def gap():
        box = message.bounding_box()
        line = code.locator(".ace_line").nth(1).bounding_box()
        return line["y"] - box["y"] - box["height"] if box and line else 99

    # The space reserved shrinks with the message, so no gap is left either.
    wait_until(lambda: gap() < 12, timeout=8000)


def test_a_failing_run_keeps_the_last_good_result(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " >")
    page.keyboard.press("ControlOrMeta+Enter")
    result = page.locator(".fd-code-panel-result")
    expect(result.locator(".fd-code-run-error")).to_contain_text("SyntaxError", timeout=8000)
    expect(result.locator(".fd-code-table")).to_contain_text("2016")


def test_columns_are_completed(page):
    editor, query = make_editor()
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_c")
    popup = page.locator(".ace_autocomplete")
    expect(popup).to_be_visible(timeout=8000)
    expect(popup).to_contain_text("p_cap")
    expect(popup).to_contain_text("number")
    _shot(page, "completion")
    page.keyboard.press("Enter")
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap", timeout=8000
    )


def test_resize_collapse_and_close(page):
    editor, query = make_editor()
    panel, _code = _open(page, editor, query)
    before = panel.bounding_box()["height"]
    handle = page.get_by_role("separator", name="Resize the code panel")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 150, steps=8)
    page.mouse.up()
    wait_until(lambda: panel.bounding_box()["height"] > before + 100, timeout=8000)
    resized = panel.bounding_box()["height"]
    wait_until(lambda: editor._code_dock_height > 500, timeout=8000)
    handle.focus()
    page.keyboard.press("ArrowDown")
    wait_until(lambda: panel.bounding_box()["height"] < resized - 10, timeout=8000)
    page.locator(".fd-code-collapse").click()
    wait_until(lambda: panel.bounding_box()["height"] < 60, timeout=8000)
    canvas = page.locator(".react-flow").first.bounding_box()
    assert canvas["y"] + canvas["height"] <= panel.bounding_box()["y"] + 1
    page.locator(".fd-code-expand-panel").click()
    wait_until(lambda: panel.bounding_box()["height"] > before + 50, timeout=8000)
    page.locator(".fd-code-close").click()
    expect(page.locator(".fd-code-panel")).to_be_hidden(timeout=8000)
    assert not editor.code_panel_open


def test_viewers_get_no_panel(page):
    editor, query = make_editor()
    editor.open_code_panel(query, "expr")
    editor.editable = False
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.wait_for_timeout(500)
    expect(page.locator(".fd-code-panel")).to_be_hidden()


@pytest.mark.skipif(not SHOTS, reason="Screenshots for comparing with the design")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshots(page, scheme):
    import panel_material_ui as pmui

    editor, query = make_editor()
    page.set_viewport_size(VIEWPORT)
    serve_component(page, pmui.Page(main=[editor], dark_theme=scheme == "dark"))
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    _type_at_end(page, code, "\nand p_yaer < 2016")
    page.keyboard.press("ControlOrMeta+Enter")
    expect(code.locator(".fd-code-diagnostic")).to_have_count(1, timeout=8000)
    page.wait_for_timeout(500)
    _shot(page, f"error-{scheme}")
    page.keyboard.press("ControlOrMeta+End")
    for _ in range(len("p_yaer < 2016")):
        page.keyboard.press("Backspace")
    page.keyboard.type("p_c", delay=60)
    expect(page.locator(".ace_autocomplete")).to_contain_text("p_cap", timeout=8000)
    page.wait_for_timeout(300)
    _shot(page, f"completion-{scheme}")


@pytest.mark.parametrize("width", [1280, 1440])
def test_every_control_is_reachable_with_the_inspector_open(page, width):
    editor, query = make_editor()
    page.set_viewport_size({"width": width, "height": 900})
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    assert not editor._inspector.collapsed
    panel = page.locator(".fd-code-panel").bounding_box()
    assert panel["x"] + panel["width"] <= width + 1
    for selector in (
        ".fd-code-run-on-change",
        ".fd-code-run",
        ".fd-code-collapse",
        ".fd-code-close",
    ):
        wait_until(
            lambda selector=selector: page.evaluate(_REACHABLE, selector) == "ok", timeout=8000
        )
    assert page.evaluate(_SIDEWAYS) == []
    _type_at_end(page, code, "\nand p_yaer < 2016")
    page.keyboard.press("ControlOrMeta+Enter")
    expect(code.locator(".fd-code-diagnostic")).to_have_count(1, timeout=8000)
    code.locator(".fd-code-fix").click()
    page.keyboard.press("ControlOrMeta+Enter")
    expect(code.locator(".fd-code-diagnostic")).to_have_count(0, timeout=8000)
    page.wait_for_timeout(300)
    assert page.evaluate(_SIDEWAYS) == []
    _shot(page, f"inspector-open-{width}")


def test_split_tab_keeps_the_header_in_the_window(page):
    editor, query = make_editor()
    editor.mode = "split"
    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    expect(page.locator(".fd-code-panel")).to_be_visible(timeout=8000)
    for selector in (".fd-code-run", ".fd-code-close"):
        wait_until(
            lambda selector=selector: page.evaluate(_REACHABLE, selector) == "ok", timeout=8000
        )
    assert page.evaluate(_SIDEWAYS) == []
    _shot(page, "split-1440")


def test_a_held_draft_comes_back_after_closing(page):
    editor, query = make_editor()
    editor.dirty = False
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_cap > 2")
    expect(page.locator(".fd-code-discard")).to_be_visible(timeout=8000)
    page.locator(".fd-code-close").click()
    expect(page.locator(".fd-code-panel")).to_be_hidden(timeout=8000)
    expect(page.get_by_text("Edits not run")).to_be_visible(timeout=8000)
    page.locator(".fd-edit-spec").first.click()
    expect(code.locator(".ace_content")).to_contain_text("p_cap > 2", timeout=8000)
    page.locator(".fd-code-discard").click()
    expect(code.locator(".ace_content")).not_to_contain_text("p_cap", timeout=8000)
    assert not editor._has_unsent_edits()


def test_the_collapse_button_flips(page):
    editor, query = make_editor()
    _open(page, editor, query)
    page.locator(".fd-code-collapse").click()
    expect(page.locator(".fd-code-expand-panel")).to_be_visible(timeout=8000)
    expect(page.locator(".fd-code-collapse")).to_be_hidden()
    page.locator(".fd-code-expand-panel").click()
    expect(page.locator(".fd-code-collapse")).to_be_visible(timeout=8000)


@pytest.mark.parametrize("answer", ["Keep editing", "Leave without saving"])
def test_leaving_the_dashboard_asks_about_a_held_draft(page, answer):
    from panel_flowdash import DashboardItem, MemoryDashboardStore
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.auth import resolve_identity
    from panel_flowdash.component_library import normalize_components
    from tests.test_code_panel import COMPONENTS

    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Notes")
    model.items = [DashboardItem(instance_id="n1", component_id="D/note", x=0, y=0)]
    store.save_dashboard(model)
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    page.set_viewport_size(VIEWPORT)
    routes = {"/": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=f"/dash/{model.dashboard_id}?edit=true")
    page.get_by_role("button", name="Done").wait_for()
    editor = apps[0]._editor
    page.locator(".react-flow__node[data-id='n1']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("# Hi", timeout=8000)
    _type_at_end(page, code, " there")
    wait_until(lambda: editor.code_drafts, timeout=8000)
    assert not editor.dirty
    expect(page.get_by_text("Edits not run")).to_be_visible(timeout=8000)

    page.get_by_role("button", name="Dashboards", exact=True).last.click()
    dialog = page.get_by_role("dialog").filter(has_text="Save changes before leaving?")
    expect(dialog).to_be_visible(timeout=8000)
    expect(dialog.get_by_text("Edits in the code panel that were not run")).to_be_visible()
    dialog.get_by_role("button", name=answer).click()
    if answer == "Keep editing":
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert editor.code_drafts
        expect(code.locator(".ace_content")).to_contain_text("# Hi there")
    else:
        wait_until(lambda: not editor.code_drafts, timeout=8000)
        expect(page.get_by_role("dialog")).to_have_count(0, timeout=8000)
        expect(page.get_by_text("Edits not run")).to_have_count(0)


def test_view_mode_says_a_draft_is_held(page):
    editor, query = make_editor()
    editor.dirty = False
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_cap > 2")
    wait_until(lambda: editor.code_drafts, timeout=8000)
    page.get_by_role("button", name="Done").click()
    wait_until(lambda: not editor.editable, timeout=8000)
    expect(page.get_by_text("Edits not run")).to_be_visible(timeout=8000)


def test_the_panel_follows_the_selection_and_keeps_drafts(page):
    editor, query = make_editor()
    note = editor.add_component("D/note", position=(0, 300))
    source = next(
        i["instance_id"] for i in editor._tile_items if i["instance_id"] not in (query, note)
    )
    _panel, code = _open(page, editor, query)
    _type_at_end(page, code, " and p_cap > 2")
    wait_until(lambda: editor.code_panel.dirty, timeout=8000)
    # The panel keeps the edited node in view at its zoom, so the note below it is under the
    # panel; fit the canvas to its nodes, as a user would, rather than click through the panel.
    page.locator(".react-flow__controls-fitview").click()
    note_card = page.locator(f".react-flow__node[data-id='{note}']")
    dock_top = page.locator(".fd-code-dock").bounding_box()["y"]
    wait_until(
        lambda: note_card.bounding_box()["y"] + note_card.bounding_box()["height"] < dock_top, page
    )
    note_card.click()
    expect(code.locator(".ace_content")).to_contain_text("# Hi", timeout=8000)
    expect(page.locator(".fd-code-panel-editor")).to_be_visible()
    page.locator(f".react-flow__node[data-id='{source}']").click()
    expect(page.locator(".fd-code-empty")).to_contain_text("has no code or spec", timeout=8000)
    expect(page.locator(".fd-code-panel-editor")).to_be_hidden()
    expect(page.locator(".fd-code-table")).to_contain_text("2016", timeout=8000)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    expect(code.locator(".ace_content")).to_contain_text("p_cap > 2", timeout=8000)
    assert editor._component_instances[query].expr == "p_year > 2014"


def test_the_inspector_offers_the_spec_and_keeps_the_settings(page):
    editor, query = make_editor()
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    expect(page.locator(".fd-edit-spec")).to_be_visible(timeout=8000)
    expect(page.locator(".fd-code-snippet")).to_have_count(0)
    expect(page.get_by_text("Limit", exact=True).first).to_be_visible()
    page.locator(".fd-edit-spec").click()
    expect(page.locator(".fd-code-panel")).to_be_visible(timeout=8000)
    header = page.locator(".fd-code-panel-header")
    expect(header.locator("input")).to_have_count(1)  # Run on change only


def test_outputs_json_cannot_show_are_previewed(page):
    from tests.test_code_panel import make_opaque_editor

    editor, node = make_opaque_editor()
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    expect(page.locator(".fd-code-table")).to_be_visible(timeout=15000)
    outputs = page.locator(".fd-code-outputs")
    outputs.get_by_role("button", name="Pipeline").click()
    preview = page.locator(".fd-code-preview")
    expect(preview).to_contain_text("2 rows", timeout=15000)
    outputs.get_by_role("button", name="Result").click()
    expect(preview).to_contain_text("'pipeline': Opaque", timeout=15000)
    expect(preview).not_to_contain_text("Opaque()")
    outputs.get_by_role("button", name="Rows").click()
    expect(page.locator(".fd-code-table")).to_be_visible(timeout=15000)
    assert not [e for e in errors if "JSON" in e]

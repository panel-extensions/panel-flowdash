const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
const MOD = IS_MAC ? "⌘" : "Ctrl"
const GROUPS = ["Dashboards", "Components", "On this dashboard", "Actions"]
// Results per group: a short overview while the query is empty, more once it narrows them.
const LIMIT_EMPTY = 5
const LIMIT_QUERY = 20

const FIELD_CSS = `
:host { display: flex; align-items: center; }
.fd-cmdk-field {
  display: flex; align-items: center; gap: 10px; box-sizing: border-box;
  width: min(480px, 36vw); height: 40px; padding: 0 8px 0 14px;
  border: 1px solid transparent; border-radius: 20px; cursor: text;
  background: var(--mui-palette-action-hover); color: var(--mui-palette-text-secondary);
  font: inherit; font-size: 0.9375rem; text-align: left;
}
.fd-cmdk-field:hover { border-color: var(--mui-palette-divider); }
.fd-cmdk-field:focus-visible { outline: 2px solid var(--mui-palette-primary-main); outline-offset: 1px; }
.fd-cmdk-label { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.fd-cmdk-icon {
  font-family: "Material Icons Outlined", "Material Icons"; font-size: 20px; line-height: 1;
  font-feature-settings: "liga"; -webkit-font-smoothing: antialiased; font-style: normal;
}
kbd {
  font: inherit; font-size: 0.75rem; line-height: 1; padding: 3px 6px; border-radius: 4px;
  border: 1px solid var(--mui-palette-divider); background: var(--mui-palette-background-paper);
  color: var(--mui-palette-text-secondary); white-space: nowrap;
}
@media (max-width: 899.95px) { .fd-cmdk-field { width: 240px; } }
@media (max-width: 599.95px) {
  .fd-cmdk-field { width: 40px; padding: 0; justify-content: center; background: none; }
  .fd-cmdk-label, .fd-cmdk-field kbd { display: none; }
}
@media (max-width: 1679.95px) {
  .fd-cmdk-field.compact { width: 40px; padding: 0; justify-content: center; background: none; }
  .fd-cmdk-field.compact:hover { background: var(--mui-palette-action-hover); }
  .fd-cmdk-field.compact .fd-cmdk-label, .fd-cmdk-field.compact kbd { display: none; }
}
`

const DIALOG_CSS = `
:host { all: initial; font-family: var(--mui-font-family, Roboto, "Helvetica Neue", Arial, sans-serif); }
.backdrop {
  position: fixed; inset: 0; z-index: 1400; display: flex; justify-content: center;
  align-items: flex-start; padding: 72px 16px 16px; box-sizing: border-box;
  background: rgba(0, 0, 0, 0.4);
}
.dialog {
  display: flex; flex-direction: column; width: min(880px, 100%); max-height: min(640px, 100%);
  border-radius: 16px; overflow: hidden; box-sizing: border-box;
  background: var(--mui-palette-background-paper); color: var(--mui-palette-text-primary);
  /* Material lightens raised surfaces in the dark scheme; the variable is unset in light. */
  background-image: var(--mui-overlays-16, none);
  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.3); border: 1px solid var(--mui-palette-divider);
}
.search { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--mui-palette-divider); }
.search input {
  flex: 1; min-width: 0; border: 0; outline: 0; background: none; padding: 4px 0;
  color: inherit; font: inherit; font-size: 1.125rem;
}
.search input::placeholder { color: var(--mui-palette-text-secondary); opacity: 1; }
.body { display: flex; flex: 1; min-height: 0; }
.results { flex: 1; min-width: 0; overflow-y: auto; padding: 8px; }
.side {
  width: 300px; flex-shrink: 0; overflow-y: auto; padding: 16px;
  border-left: 1px solid var(--mui-palette-divider); background: var(--mui-palette-action-hover);
}
.dialog:focus { outline: none; }
.dialog.help { width: min(520px, 100%); }
.help .side { width: auto; flex: 1; border-left: 0; background: none; }
.help .results { display: none; }
.group { font-size: 0.8125rem; font-weight: 500; color: var(--mui-palette-text-secondary); padding: 12px 12px 6px; }
.group:first-child { padding-top: 4px; }
.item {
  display: flex; align-items: center; gap: 14px; padding: 8px 12px; border-radius: 8px;
  cursor: pointer; min-height: 40px; box-sizing: border-box;
}
.item[aria-selected="true"] { background: rgba(var(--mui-palette-primary-mainChannel) / 0.14); }
.item .fd-cmdk-icon { color: var(--mui-palette-text-secondary); }
.text { flex: 1; min-width: 0; }
.title, .subtitle { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.title { font-size: 0.9375rem; }
.subtitle { font-size: 0.8125rem; color: var(--mui-palette-text-secondary); margin-top: 2px; }
mark { background: none; color: inherit; font-weight: 700; }
.hint { font-size: 0.875rem; color: var(--mui-palette-text-secondary); }
.keys { display: flex; gap: 4px; flex-shrink: 0; }
.empty { padding: 24px 12px; color: var(--mui-palette-text-secondary); font-size: 0.9375rem; }
.side h3 { margin: 0 0 8px; font-size: 0.875rem; font-weight: 500; }
.side h3:not(:first-child) { margin-top: 20px; }
.shortcut { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 0.875rem; padding: 5px 0; }
.footer {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px 16px; padding: 10px 16px;
  border-top: 1px solid var(--mui-palette-divider); font-size: 0.8125rem; color: var(--mui-palette-text-secondary);
}
.footer span { display: inline-flex; align-items: center; gap: 4px; }
.fd-cmdk-icon {
  font-family: "Material Icons Outlined", "Material Icons"; font-size: 20px; line-height: 1;
  font-feature-settings: "liga"; -webkit-font-smoothing: antialiased; font-style: normal;
}
kbd {
  font: inherit; font-size: 0.75rem; line-height: 1; padding: 3px 6px; border-radius: 4px; min-width: 8px;
  text-align: center; border: 1px solid var(--mui-palette-divider);
  background: var(--mui-palette-background-paper); color: var(--mui-palette-text-secondary);
}
@media (max-width: 899.95px) { .side { display: none; } .help .side { display: block; } }
@media (max-width: 599.95px) { .backdrop { padding: 16px 8px; } }
`

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag)
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") {
      node.className = value
    } else {
      node.setAttribute(key, value)
    }
  }
  node.append(...children.filter((c) => c != null))
  return node
}

function icon(name) {
  return el("span", {class: "fd-cmdk-icon", "aria-hidden": "true"}, name || "")
}

function keyLabel(key) {
  return key === "Mod" ? MOD : key
}

function keys(list) {
  return el("span", {class: "keys"}, ...list.map((k) => el("kbd", {}, keyLabel(k))))
}

// Roles whose widgets take letter keys themselves, e.g. type-ahead in a select or menu.
const KEY_ROLES = new Set(["combobox", "listbox", "option", "menu", "menuitem", "textbox", "spinbutton", "slider"])

function typing(target) {
  const tag = target?.tagName
  return (
    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable ||
    KEY_ROLES.has(target?.getAttribute?.("role"))
  )
}

function inDialog(path) {
  return path.some((node) => node instanceof Element && (
    node.getAttribute("role") === "dialog" || node.getAttribute("aria-modal") === "true"
  ))
}

// The element in full screen, which may sit in shadow roots, or rather its
// shadow root, where an appended element is rendered.
function fullscreenRoot() {
  let node = document.fullscreenElement
  while (node?.shadowRoot?.fullscreenElement) {
    node = node.shadowRoot.fullscreenElement
  }
  return node ? (node.shadowRoot ?? node) : null
}

function editing() {
  return new URLSearchParams(window.location.search).get("edit") === "true"
}

// Matches every whitespace-separated token as a substring of the title,
// subtitle or keywords, or else as a subsequence of the title, and returns
// a score plus the matched spans in the title and subtitle.
export function matchItem(item, query) {
  const tokens = query.toLowerCase().split(/\s+/).filter(Boolean)
  const title = (item.title || "").toLowerCase()
  const subtitle = (item.subtitle || "").toLowerCase()
  const keywords = (item.keywords || "").toLowerCase()
  const spans = {title: [], subtitle: []}
  let score = 0
  for (const token of tokens) {
    let index = title.indexOf(token)
    if (index >= 0) {
      const wordStart = index === 0 || /\W/.test(title[index - 1])
      score += 10 + (wordStart ? 6 : 0) + (index === 0 ? 4 : 0)
      spans.title.push([index, index + token.length])
      continue
    }
    index = subtitle.indexOf(token)
    if (index >= 0) {
      score += 4
      spans.subtitle.push([index, index + token.length])
      continue
    }
    if (keywords.includes(token)) {
      score += 3
      continue
    }
    const positions = []
    let from = 0
    for (const char of token) {
      const found = title.indexOf(char, from)
      if (found < 0) {
        return null
      }
      positions.push(found)
      from = found + 1
    }
    // Letters spread across the title, like "cap" in "Location Map", are noise.
    const gaps = positions[positions.length - 1] - positions[0] + 1 - token.length
    if (gaps > 2) {
      return null
    }
    score += 5 - gaps
    for (const p of positions) {
      spans.title.push([p, p + 1])
    }
  }
  return {score, spans}
}

function mergeSpans(spans) {
  const sorted = [...spans].sort((a, b) => a[0] - b[0])
  const merged = []
  for (const span of sorted) {
    const last = merged[merged.length - 1]
    if (last && span[0] <= last[1]) {
      last[1] = Math.max(last[1], span[1])
    } else {
      merged.push([...span])
    }
  }
  return merged
}

function highlighted(text, spans) {
  const node = document.createDocumentFragment()
  let at = 0
  for (const [start, end] of mergeSpans(spans)) {
    node.append(text.slice(at, start), el("mark", {}, text.slice(start, end)))
    at = end
  }
  node.append(text.slice(at))
  return node
}

export function filterItems(items, query) {
  const limit = query.trim() ? LIMIT_QUERY : LIMIT_EMPTY
  const groups = []
  for (const group of GROUPS) {
    const matches = []
    items.forEach((item, order) => {
      if ((item.group || "Actions") !== group) {
        return
      }
      const match = matchItem(item, query)
      if (match) {
        matches.push({item, order, ...match})
      }
    })
    matches.sort((a, b) => b.score - a.score || a.order - b.order)
    if (matches.length) {
      groups.push({group, matches: matches.slice(0, limit)})
    }
  }
  return groups
}

export function render({model, el: root}) {
  const registry = (window.__flowdashCommandPalettes ??= new Map())
  registry.get(model.id)?.()

  const style = document.createElement("style")
  style.textContent = FIELD_CSS
  const field = el(
    "button",
    // A short name, so the field does not match locators for the words in its placeholder.
    {class: "fd-cmdk-field", type: "button", "aria-label": "Search", "aria-keyshortcuts": "Control+K Meta+K",
     title: `${model.placeholder} (${MOD}+K)`},
    icon("search"),
    el("span", {class: "fd-cmdk-label", "aria-hidden": "true"}, model.placeholder),
    el("kbd", {"aria-hidden": "true"}, `${MOD} K`),
  )
  root.append(style, field)
  const syncCompact = () => field.classList.toggle("compact", model.compact)
  syncCompact()
  model.on("compact", syncCompact)

  // The dialog lives outside the app bar, so neither its stacking nor its
  // inverted palette applies; CSS variables still come from the page.
  const host = document.createElement("div")
  host.className = "flowdash-command-palette"
  const shadow = host.attachShadow({mode: "open"})
  const dialogStyle = document.createElement("style")
  dialogStyle.textContent = DIALOG_CSS
  shadow.append(dialogStyle)

  let state = null
  let returnFocus = null

  function shortcutColumn() {
    const side = el("div", {class: "side", "aria-label": "Keyboard shortcuts"})
    let group = null
    for (const shortcut of model.shortcuts) {
      if (shortcut.group !== group) {
        group = shortcut.group
        side.append(el("h3", {}, group))
      }
      side.append(el("div", {class: "shortcut"}, el("span", {}, shortcut.label), keys(shortcut.keys)))
    }
    return side
  }

  function renderResults() {
    const {results, input} = state
    const groups = filterItems(model.items, input.value)
    state.flat = []
    results.replaceChildren()
    for (const {group, matches} of groups) {
      const groupId = `fd-cmdk-group-${group.replace(/\W+/g, "-")}`
      const list = el("div", {role: "group", "aria-labelledby": groupId})
      for (const {item, spans} of matches) {
        const index = state.flat.length
        state.flat.push(item)
        const text = el(
          "div", {class: "text"},
          el("div", {class: "title"}, highlighted(item.title || "", spans.title)),
          item.subtitle ? el("div", {class: "subtitle"}, highlighted(item.subtitle, spans.subtitle)) : null,
        )
        const row = el(
          "div", {class: "item", role: "option", id: `fd-cmdk-item-${index}`, "aria-selected": "false"},
          icon(item.icon), text,
          item.keys?.length ? keys(item.keys) : item.hint ? el("span", {class: "hint"}, item.hint) : null,
        )
        row.addEventListener("mousemove", () => select(index, false))
        row.addEventListener("click", () => run(index))
        list.append(row)
      }
      results.append(el("div", {class: "group", id: groupId}, group), list)
    }
    if (!state.flat.length) {
      results.append(el("div", {class: "empty"}, model.items.length ? "No results" : "Loading…"))
    }
    select(0, true)
  }

  function select(index, scroll) {
    const {results, input} = state
    const count = state.flat.length
    if (!count) {
      state.active = -1
      input.removeAttribute("aria-activedescendant")
      return
    }
    state.active = (index + count) % count
    for (const row of results.querySelectorAll(".item")) {
      row.setAttribute("aria-selected", String(row.id === `fd-cmdk-item-${state.active}`))
    }
    input.setAttribute("aria-activedescendant", `fd-cmdk-item-${state.active}`)
    if (scroll) {
      results.querySelector(`#fd-cmdk-item-${state.active}`)?.scrollIntoView({block: "nearest"})
    }
  }

  function run(index) {
    const item = state?.flat[index]
    if (!item) {
      return
    }
    close()
    if (item.id === "action:fullscreen") {
      // Full screen is only granted during the user's gesture, so it is
      // requested here rather than after a round trip to the server.
      document.dispatchEvent(new CustomEvent("flowdash:fullscreen", {cancelable: true}))
      return
    }
    model.send_msg({type: "run", id: item.id})
  }

  function open(help = false) {
    if (state) {
      if (help !== state.help) {
        close()
      } else {
        state.input.focus()
        return
      }
    }
    returnFocus = document.activeElement
    const input = el("input", {
      type: "text", placeholder: model.placeholder, "aria-label": model.placeholder,
      role: "combobox", "aria-expanded": "true", "aria-controls": "fd-cmdk-results", autocomplete: "off",
      spellcheck: "false",
    })
    const results = el("div", {class: "results", id: "fd-cmdk-results", role: "listbox"})
    const footer = el(
      "div", {class: "footer"},
      el("span", {}, el("kbd", {}, "Up"), el("kbd", {}, "Down"), " to move"),
      el("span", {}, el("kbd", {}, "Enter"), " to open"),
      el("span", {}, el("kbd", {}, "Esc"), " to close"),
      el("span", {}, el("kbd", {}, "?"), " for all shortcuts"),
    )
    const dialog = el(
      "div", {class: help ? "dialog help" : "dialog", role: "dialog", "aria-modal": "true",
              "aria-label": help ? "Keyboard shortcuts" : "Command palette"},
      el("div", {class: "search"}, icon(help ? "keyboard" : "search"),
         help ? el("div", {style: "flex: 1; font-size: 1.125rem"}, "Keyboard shortcuts") : input,
         el("kbd", {}, "Esc")),
      el("div", {class: "body"}, results, shortcutColumn()),
      help ? null : footer,
    )
    const backdrop = el("div", {class: "backdrop"}, dialog)
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) {
        close()
      }
    })
    dialog.addEventListener("keydown", onDialogKey)
    input.addEventListener("input", () => renderResults())
    state = {help, input, results, backdrop, flat: [], active: -1}
    shadow.append(backdrop)
    // In full screen the rest of the page is hidden and inert, so the dialog
    // goes inside the full screen element.
    const parent = fullscreenRoot() ?? document.body
    if (host.parentNode !== parent) {
      parent.append(host)
    }
    if (help) {
      dialog.tabIndex = -1
      dialog.focus()
    } else {
      renderResults()
      input.focus()
      model.send_msg({type: "open"})
    }
  }

  function close() {
    if (!state) {
      return
    }
    state.backdrop.remove()
    state = null
    if (returnFocus?.isConnected && returnFocus.focus) {
      returnFocus.focus()
    }
    returnFocus = null
  }

  function onDialogKey(event) {
    if (event.key === "Escape") {
      event.preventDefault()
      event.stopPropagation()
      close()
    } else if (state.help) {
      return
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      select(state.active + (event.key === "ArrowDown" ? 1 : -1), true)
    } else if (event.key === "Enter") {
      event.preventDefault()
      run(state.active)
    } else if (event.key === "?" && !state.input.value) {
      event.preventDefault()
      open(true)
    } else {
      return
    }
    // Handled here; the document's shortcuts must not see the key too.
    event.stopPropagation()
  }

  function onDocumentKey(event) {
    const mod = event.ctrlKey || event.metaKey
    const key = event.key.toLowerCase()
    if (mod && !event.altKey && key === "k") {
      event.preventDefault()
      state && !state.help ? close() : open()
      return
    }
    const path = event.composedPath()
    if (state || event.defaultPrevented || event.isComposing || typing(path[0]) || inDialog(path)) {
      return
    }
    if (mod && !event.altKey && !event.shiftKey && key === "s") {
      // Only in edit mode: elsewhere the browser's own Save stays available.
      if (editing()) {
        event.preventDefault()
        model.send_msg({type: "shortcut", name: "save"})
      }
      return
    }
    if (mod || event.altKey) {
      return
    }
    if (event.key === "?") {
      event.preventDefault()
      open(true)
    } else if (event.shiftKey) {
      return
    } else if (key === "r") {
      model.send_msg({type: "shortcut", name: "refresh"})
    } else if (key === "e") {
      model.send_msg({type: "shortcut", name: "edit"})
    } else if (key === "f" && !editing()) {
      document.dispatchEvent(new CustomEvent("flowdash:fullscreen", {cancelable: true}))
    } else if (key === "s" && editing()) {
      model.send_msg({type: "shortcut", name: "split"})
    } else if (event.key === "Escape" && editing()) {
      model.send_msg({type: "shortcut", name: "view"})
    }
  }

  field.addEventListener("click", () => open())
  document.addEventListener("keydown", onDocumentKey)
  model.on("items", () => {
    if (state && !state.help) {
      renderResults()
    }
  })

  const cleanup = () => {
    document.removeEventListener("keydown", onDocumentKey)
    host.remove()
    state = null
    registry.delete(model.id)
  }
  registry.set(model.id, cleanup)
  model.on("remove", cleanup)
}

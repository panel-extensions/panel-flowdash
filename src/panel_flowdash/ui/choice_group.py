"""A single-choice list of options with a title and a description each, as an ARIA radio group."""

from __future__ import annotations

import param
from panel.custom import JSComponent


class ChoiceGroup(JSComponent):
    """Pick one of ``items``, each ``{"label": ..., "description": ..., "icon": ...}``.

    It is a ``radiogroup`` labelled ``label`` whose options are ``radio``
    elements with ``aria-checked``, reached with one Tab stop: the arrow keys
    move between the options and choose, Home and End go to the first and the
    last, and Enter or Space chooses the focused option.
    """

    items = param.List(
        default=[], doc="The options: dicts with a label, a description and an icon."
    )

    index = param.Integer(default=0, bounds=(0, None), doc="The index of the chosen option.")

    label = param.String(default="", doc="The accessible name of the group.")

    _esm = """
function render({ model, el }) {
  const group = document.createElement("div")
  group.className = "group"
  group.setAttribute("role", "radiogroup")
  el.appendChild(group)

  function options() { return Array.from(group.querySelectorAll("[role=radio]")) }

  function choose(i, focus) {
    const all = options()
    if (!all.length) { return }
    i = Math.max(0, Math.min(all.length - 1, i))
    if (model.index !== i) { model.index = i }
    sync()
    if (focus) { all[i].focus() }
  }

  function sync() {
    group.setAttribute("aria-label", model.label || "")
    options().forEach((option, i) => {
      const checked = i === model.index
      option.setAttribute("aria-checked", checked ? "true" : "false")
      option.tabIndex = checked ? 0 : -1
      option.classList.toggle("checked", checked)
    })
  }

  function build() {
    group.replaceChildren()
    ;(model.items || []).forEach((item, i) => {
      const option = document.createElement("div")
      option.setAttribute("role", "radio")
      option.className = "option"
      const icon = document.createElement("span")
      icon.className = "mi"
      icon.setAttribute("aria-hidden", "true")
      icon.textContent = item.icon || "dashboard"
      const text = document.createElement("span")
      text.className = "text"
      const title = document.createElement("span")
      title.className = "title"
      title.textContent = item.label || ""
      const description = document.createElement("span")
      description.className = "description"
      description.textContent = item.description || ""
      text.append(title, description)
      option.append(icon, text)
      option.addEventListener("click", () => choose(i, true))
      option.addEventListener("keydown", (event) => {
        const last = options().length - 1
        const moves = {ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: last}
        if (event.key in moves) {
          event.preventDefault()
          let next = moves[event.key]
          if (next > last) { next = 0 }
          if (next < 0) { next = last }
          choose(next, true)
        } else if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          choose(i, true)
        }
      })
      group.appendChild(option)
    })
    sync()
  }

  build()
  model.on("items", build)
  model.on(["index", "label"], sync)
}
export default { render }
"""

    _stylesheets = [  # noqa: RUF012
        """
:host { display: block; width: 100%; }
.mi {
  font-family: 'Material Icons'; font-weight: normal; font-style: normal; font-size: 22px;
  line-height: 1; letter-spacing: normal; text-transform: none; white-space: nowrap;
  -webkit-font-smoothing: antialiased; font-feature-settings: 'liga';
}
.group { display: flex; flex-direction: column; gap: 2px; }
.option {
  display: flex; gap: 14px; align-items: flex-start; padding: 10px 12px; cursor: pointer;
  border-radius: 6px; border-left: 3px solid transparent;
  color: var(--mui-palette-text-primary);
}
.option:hover { background: var(--mui-palette-action-hover); }
.option.checked {
  background: var(--mui-palette-action-selected, rgba(0, 114, 181, 0.12));
  border-left-color: var(--mui-palette-primary-main);
}
.option:focus { outline: none; }
.option:focus-visible {
  outline: 2px solid var(--mui-palette-primary-main);
  outline-offset: -2px;
}
.option .mi { margin-top: 1px; color: var(--mui-palette-text-secondary); }
.option.checked .mi { color: var(--mui-palette-primary-main); }
.text { display: flex; flex-direction: column; min-width: 0; }
.title { font-size: 15px; line-height: 22px; font-weight: 500; }
.description { font-size: 13px; line-height: 18px; color: var(--mui-palette-text-secondary); }
"""
    ]

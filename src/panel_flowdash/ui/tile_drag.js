const TILE_KEY_PREFIX = "flowdash-tile-key-"
// Pixels the pointer moves before a press on a handle counts as a drag.
const THRESHOLD = 4
// The drop targets shown for an empty area, at the edge of the grid.
const EMPTY_TOP = 44
const EMPTY_SIDE = 88
// Thinner while a component beside the grid is dragged, since the grid is a
// target then too and a drop near a tile's edge should land on the grid.
const EMPTY_TOP_SLIM = 20
const EMPTY_SIDE_SLIM = 24

function collect(root, test, found = []) {
  for (const el of root.querySelectorAll("*")) {
    if (test(el)) { found.push(el) }
    if (el.shadowRoot) { collect(el.shadowRoot, test, found) }
  }
  return found
}

function hasClass(cls) { return el => el.classList?.contains(cls) }

function shown(el) {
  const rect = el.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

// Pixels around a bar the pointer may be and still drop in it, since a bar can be slim.
const SLACK = 8

function within(rect, x, y, slack = 0) {
  return x >= rect.left - slack && x <= rect.right + slack && y >= rect.top - slack && y <= rect.bottom + slack
}

function keyOf(el, prefix) {
  const cls = [...el.classList].find(c => c.startsWith(prefix))
  return cls ? cls.slice(prefix.length) : null
}

// The elements around *el* that scroll, across shadow roots, nearest first.
function scrollers(el) {
  const found = []
  for (let node = el; node; node = node.parentNode ?? node.host) {
    if (node.nodeType === Node.ELEMENT_NODE && /auto|scroll/.test(getComputedStyle(node).overflowY)) {
      found.push(node)
    }
  }
  return found
}

// The part of *rect* that is on screen, inside the elements that scroll it.
function visibleRect(rect, clips) {
  let {left, right, top, bottom} = rect
  top = Math.max(top, 0)
  bottom = Math.min(bottom, window.innerHeight)
  for (const clip of clips) {
    const r = clip.getBoundingClientRect()
    top = Math.max(top, r.top)
    bottom = Math.min(bottom, r.bottom)
  }
  return {left, right, top, bottom, width: right - left, height: bottom - top}
}

function box(rect, styles) {
  const el = document.createElement("div")
  Object.assign(el.style, {
    position: "fixed", boxSizing: "border-box", left: `${rect.left}px`, top: `${rect.top}px`,
    width: `${rect.right - rect.left}px`, height: `${rect.bottom - rect.top}px`, ...styles,
  })
  return el
}

export function render({model}) {
  let drag = null
  let layer = null

  const primary = "var(--mui-palette-primary-main, #1976d2)"
  const muted = "var(--mui-palette-text-disabled, #9e9e9e)"

  // The page's targets are found once per drag, since walking every shadow
  // root on each pointer move would be slow on a large dashboard.
  function findTargets() {
    const areaClasses = Object.fromEntries(model.areas.map(a => [`fd-area-${a}-${model.uid}`, a]))
    const found = {grid: null, areas: Object.fromEntries(model.areas.map(a => [a, []]))}
    collect(document, el => {
      if (!el.classList?.length) { return false }
      if (el.classList.contains(model.grid) && shown(el)) { found.grid ??= el }
      for (const cls of el.classList) {
        if (cls in areaClasses) { found.areas[areaClasses[cls]].push(el) }
      }
      return false
    })
    found.clips = found.grid ? scrollers(found.grid.parentNode ?? found.grid.host) : []
    found.controls = new Map()
    for (const els of Object.values(found.areas)) {
      for (const el of els) {
        found.controls.set(el, collect(el.shadowRoot ?? el, hasClass(`fd-control-${model.uid}`))
          .map(c => ({el: c, id: model.controls[keyOf(c, "fd-key-")]?.id}))
          .filter(c => c.id && c.id !== drag.id))
      }
    }
    found.tiles = []
    if (found.grid) {
      for (const item of collect(found.grid.shadowRoot ?? found.grid, hasClass("muuri-grid-item"))) {
        const tile = item.querySelector(`[class*="${TILE_KEY_PREFIX}"]`)
        const id = tile ? model.tiles[keyOf(tile, TILE_KEY_PREFIX)]?.id : null
        if (id) { found.tiles.push({el: item, id}) }
      }
    }
    return found
  }

  function zones() {
    const zones = []
    const found = drag.found
    const gridEl = found.grid
    const gridRect = gridEl ? visibleRect(gridEl.getBoundingClientRect(), found.clips) : null
    const slim = drag.kind === "control"
    for (const area of model.areas) {
      const el = found.areas[area].find(shown)
      if (el) {
        zones.push({area, el, rect: el.getBoundingClientRect(), empty: false})
      } else if (gridRect) {
        const rect = area === "top"
          ? {...gridRect, bottom: gridRect.top + (slim ? EMPTY_TOP_SLIM : EMPTY_TOP)}
          : {...gridRect, right: gridRect.left + (slim ? EMPTY_SIDE_SLIM : EMPTY_SIDE)}
        zones.push({area, el: null, rect, empty: true})
      }
    }
    if (drag.kind === "control" && gridEl) {
      zones.push({area: "grid", el: gridEl, rect: gridRect, empty: false})
    }
    return zones
  }

  function controlsIn(zone) {
    return (drag.found.controls.get(zone.el) ?? []).filter(c => shown(c.el))
  }

  function tilesIn() {
    const tiles = drag.found.tiles.filter(t => shown(t.el))
    const rect = el => el.getBoundingClientRect()
    return tiles.sort((a, b) => (rect(a.el).top - rect(b.el).top) || (rect(a.el).left - rect(b.el).left))
  }

  // The component the drop goes in front of, and a marker showing where.
  function insertion(zone, x, y) {
    if (zone.empty) { return {before: null, marker: null} }
    const vertical = zone.area === "sidebar"
    const members = zone.area === "grid" ? tilesIn() : controlsIn(zone)
    let last = null
    for (const member of members) {
      const r = member.el.getBoundingClientRect()
      const ahead = vertical
        ? y < r.top + r.height / 2
        : y < r.top || (y <= r.bottom && x < r.left + r.width / 2)
      if (ahead) {
        const marker = vertical
          ? {left: r.left, right: r.right, top: r.top - 2, bottom: r.top + 1}
          : {left: r.left - 2, right: r.left + 1, top: r.top, bottom: r.bottom}
        return {before: member.id, marker}
      }
      last = r
    }
    if (!last) { return {before: null, marker: null} }
    const marker = vertical
      ? {left: last.left, right: last.right, top: last.bottom - 1, bottom: last.bottom + 2}
      : {left: last.right - 1, right: last.right + 2, top: last.top, bottom: last.bottom}
    return {before: null, marker}
  }

  function hit(x, y) {
    const all = zones()
    // An empty bar's strip lies over the grid, so it takes no slack from the tiles.
    const zone = all.find(z => z.area !== "grid" && within(z.rect, x, y, z.empty ? 0 : SLACK)) ??
      all.find(z => z.area === "grid" && within(z.rect, x, y))
    return {all, zone: zone ?? null}
  }

  function paint(x, y) {
    if (!layer) {
      layer = document.createElement("div")
      layer.className = "fd-tile-drag-layer"
      Object.assign(layer.style, {position: "fixed", inset: "0", pointerEvents: "none", zIndex: "2147483000"})
      document.body.appendChild(layer)
    }
    const {all, zone} = hit(x, y)
    const parts = []
    for (const z of all) {
      const refused = z.area !== "grid" && !drag.fits
      const over = z === zone
      const color = refused ? muted : primary
      const el = box(z.rect, {
        border: `2px ${over && !refused ? "solid" : "dashed"} ${color}`,
        borderRadius: "8px",
        background: `color-mix(in srgb, ${color} ${over ? 16 : 6}%, transparent)`,
        display: "flex", alignItems: "center", justifyContent: "center",
        font: "500 0.8125rem/1.2 var(--mui-typography-fontFamily, Roboto, sans-serif)",
        color, textAlign: "center", padding: "4px",
      })
      el.dataset.area = z.area
      el.dataset.over = over ? "true" : "false"
      el.dataset.refused = refused ? "true" : "false"
      if (z.empty && drag.kind !== "control") { el.textContent = model.labels[z.area] ?? z.area }
      parts.push(el)
    }
    let target = null
    if (zone) {
      const refused = zone.area !== "grid" && !drag.fits
      target = {area: zone.area, refused, before: null}
      if (refused) {
        const hint = document.createElement("div")
        hint.className = "fd-tile-drag-hint"
        hint.textContent = drag.refusal || model.refusal
        Object.assign(hint.style, {
          position: "fixed", left: `${x + 14}px`, top: `${y + 14}px`, padding: "4px 10px",
          borderRadius: "4px", whiteSpace: "nowrap",
          background: "var(--mui-palette-grey-800, #424242)", color: "#fff",
          font: "500 0.8125rem/1.4 var(--mui-typography-fontFamily, Roboto, sans-serif)",
        })
        parts.push(hint)
      } else {
        const {before, marker} = insertion(zone, x, y)
        target.before = before
        if (marker) {
          const line = box(marker, {background: primary, borderRadius: "2px"})
          line.className = "fd-tile-drag-marker"
          parts.push(line)
        }
      }
    }
    if (drag.ghost) {
      Object.assign(drag.ghost.style, {left: `${x + 12}px`, top: `${y + 8}px`})
      parts.push(drag.ghost)
    }
    layer.replaceChildren(...parts)
    drag.target = target
  }

  function finish() {
    layer?.remove()
    layer = null
    document.documentElement.style.cursor = drag?.cursor ?? ""
    drag = null
  }

  function start(e) {
    if (drag.kind === "control") {
      const ghost = document.createElement("div")
      ghost.className = "fd-tile-drag-ghost"
      ghost.textContent = drag.title || "Component"
      Object.assign(ghost.style, {
        position: "fixed", padding: "6px 12px", borderRadius: "6px",
        background: "var(--mui-palette-background-paper, #fff)",
        color: "var(--mui-palette-text-primary, #222)",
        boxShadow: "0 4px 16px rgba(0, 0, 0, 0.2)",
        font: "500 0.875rem/1.4 var(--mui-typography-fontFamily, Roboto, sans-serif)",
        whiteSpace: "nowrap",
      })
      drag.ghost = ghost
      drag.cursor = document.documentElement.style.cursor
      document.documentElement.style.cursor = "grabbing"
    }
    drag.started = true
    drag.found = findTargets()
    paint(e.clientX, e.clientY)
  }

  function onDown(e) {
    if (!model.enabled || e.button !== 0 || drag) { return }
    const path = e.composedPath()
    const handle = path.find(n => n.classList?.contains("fd-control-handle"))
    if (handle) {
      const info = model.controls[handle.dataset.fdKey]
      if (!info) { return }
      e.preventDefault()
      drag = {kind: "control", ...info, x0: e.clientX, y0: e.clientY, started: false}
      return
    }
    const tileHandle = path.find(n => n.classList?.contains("muuri-handle") && n.classList.contains("drag"))
    const item = tileHandle?.closest(".muuri-grid-item")
    const tile = item?.querySelector(`[class*="${TILE_KEY_PREFIX}"]`)
    const info = tile ? model.tiles[keyOf(tile, TILE_KEY_PREFIX)] : null
    if (!info || !model.areas.length) { return }
    drag = {kind: "tile", ...info, item, x0: e.clientX, y0: e.clientY, started: false}
  }

  function onMove(e) {
    if (!drag) { return }
    if (!drag.started) {
      if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < THRESHOLD) { return }
      start(e)
      return
    }
    if (drag.kind === "control") { e.preventDefault() }
    paint(e.clientX, e.clientY)
  }

  function onUp(e) {
    if (!drag) { return }
    const current = drag
    if (current.started) { paint(e.clientX, e.clientY) }
    const target = current.started ? current.target : null
    finish()
    if (!target || (current.kind === "tile" && target.area === "grid")) { return }
    const action = target.refused ? "refuse" : "move"
    const send = () => model.send_msg({action, id: current.id, area: target.area, before: target.before})
    if (current.kind !== "tile") {
      send()
      return
    }
    // Ahead of the layout the grid sends when the drag ends, so the editor
    // records that layout as part of the move rather than as an edit of its own.
    model.send_msg({action: "release", id: current.id, area: target.area})
    // After the grid has sent the layout the drag ended with, so the move applies
    // to it: Muuri ends a drag a frame or more after the pointer is released.
    const until = performance.now() + 2000
    const settled = () => {
      if (current.item.classList.contains("muuri-item-dragging") && performance.now() < until) {
        requestAnimationFrame(settled)
      } else {
        setTimeout(send, 50)
      }
    }
    requestAnimationFrame(settled)
  }

  function onKey(e) {
    if (drag && e.key === "Escape") { finish() }
  }

  document.addEventListener("pointerdown", onDown, true)
  document.addEventListener("pointermove", onMove, true)
  document.addEventListener("pointerup", onUp, true)
  document.addEventListener("pointercancel", finish, true)
  document.addEventListener("keydown", onKey, true)
  model.on("remove", () => {
    finish()
    document.removeEventListener("pointerdown", onDown, true)
    document.removeEventListener("pointermove", onMove, true)
    document.removeEventListener("pointerup", onUp, true)
    document.removeEventListener("pointercancel", finish, true)
    document.removeEventListener("keydown", onKey, true)
  })
  const el = document.createElement("span")
  el.style.display = "none"
  return el
}

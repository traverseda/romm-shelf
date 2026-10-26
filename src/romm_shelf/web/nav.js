// Controller navigation for romm-shelf (design.md § UI).
//
// The keyboard is the base layer: arrows move focus, Enter activates, Escape
// goes back, Q/E (or PageUp/PageDown) switch tabs, R refreshes. The gamepad
// poll below only translates buttons into those same actions, so a Steam Input
// keyboard template drives the UI just as well when the Gamepad API is absent.
//
// Focus moves within a group ([data-nav-group]) by geometry, and between groups
// only along declared neighbours (data-nav-up / -down / -left / -right).
// While an element with [data-nav-modal] exists, focus stays inside it.

const DIRS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };

function items(group) {
  return [...group.querySelectorAll("[data-nav]")].filter((el) => el.offsetParent !== null);
}

function scope() {
  const modals = document.querySelectorAll("[data-nav-modal]");
  return modals.length ? modals[modals.length - 1] : document;
}

function center(el) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

function nearest(from, candidates, dir) {
  const a = center(from);
  let best = null, bestScore = Infinity;
  for (const el of candidates) {
    if (el === from) continue;
    const b = center(el);
    const dx = b.x - a.x, dy = b.y - a.y;
    const along = { up: -dy, down: dy, left: -dx, right: dx }[dir];
    const across = dir === "up" || dir === "down" ? Math.abs(dx) : Math.abs(dy);
    if (along <= 1) continue;
    const score = along + across * 2;
    if (score < bestScore) { best = el; bestScore = score; }
  }
  return best;
}

function focusFirst(root = scope()) {
  const el = root.querySelector("[data-nav-default]") || root.querySelector("[data-nav]");
  if (el) { el.focus(); el.scrollIntoView({ block: "nearest" }); }
}

export function move(dir) {
  const root = scope();
  let cur = document.activeElement;
  if (!cur || !root.contains(cur) || !cur.hasAttribute("data-nav")) return focusFirst(root);
  const group = cur.closest("[data-nav-group]");
  let next = group && nearest(cur, items(group), dir);
  if (!next && group) {
    const name = group.getAttribute(`data-nav-${dir}`);
    const target = name && root.querySelector(`[data-nav-group="${name}"]`);
    if (target) next = nearest(cur, items(target), dir) || items(target)[0];
  }
  if (next) { next.focus(); next.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}

function emit(name) { window.dispatchEvent(new CustomEvent(name)); }

window.addEventListener("keydown", (e) => {
  if (DIRS[e.key]) { e.preventDefault(); move(DIRS[e.key]); }
  else if (e.key === "Escape" || e.key === "Backspace") { e.preventDefault(); emit("nav-back"); }
  else if (e.key === "q" || e.key === "PageUp") emit("nav-prev-tab");
  else if (e.key === "e" || e.key === "PageDown") emit("nav-next-tab");
  else if (e.key === "r") emit("nav-refresh");
  else if (e.key === "Enter" && document.activeElement?.hasAttribute("data-nav")) {
    e.preventDefault(); document.activeElement.click();
  }
});

// Gamepad: standard mapping. A=0 B=1 X=2 Y=3 LB=4 RB=5, d-pad 12-15, left stick axes 0/1.
const BUTTONS = {
  0: () => document.activeElement?.click(),
  1: () => emit("nav-back"),
  3: () => emit("nav-refresh"),
  4: () => emit("nav-prev-tab"),
  5: () => emit("nav-next-tab"),
  12: () => move("up"), 13: () => move("down"), 14: () => move("left"), 15: () => move("right"),
};
const REPEATS = new Set([12, 13, 14, 15]);
const FIRST_DELAY = 350, REPEAT_EVERY = 90, DEADZONE = 0.5;
const held = new Map();

function pressed(pad, i) {
  if (pad.buttons[i]?.pressed) return true;
  const [x, y] = pad.axes;
  return { 12: y < -DEADZONE, 13: y > DEADZONE, 14: x < -DEADZONE, 15: x > DEADZONE }[i] ?? false;
}

function poll(now) {
  for (const pad of navigator.getGamepads?.() ?? []) {
    if (!pad) continue;
    for (const i of Object.keys(BUTTONS).map(Number)) {
      const key = `${pad.index}:${i}`;
      if (!pressed(pad, i)) { held.delete(key); continue; }
      const next = held.get(key);
      if (next === undefined) { BUTTONS[i](); held.set(key, now + FIRST_DELAY); }
      else if (REPEATS.has(i) && now >= next) { BUTTONS[i](); held.set(key, now + REPEAT_EVERY); }
    }
  }
  requestAnimationFrame(poll);
}
requestAnimationFrame(poll);
window.addEventListener("gamepadconnected", (e) => emit("nav-gamepad", e));

export { focusFirst };

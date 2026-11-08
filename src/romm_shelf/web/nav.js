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
const SCROLL_STEP = 80;  // px per press; a held d-pad repeats every 90 ms

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
  // A pane marked data-nav-scroll (a manual) takes Up/Down to scroll; Up leaves it only from the top.
  if (cur.hasAttribute("data-nav-scroll") && (dir === "down" || (dir === "up" && cur.scrollTop > 0))) {
    cur.scrollBy({ top: dir === "down" ? SCROLL_STEP : -SCROLL_STEP });
    return;
  }
  const group = cur.closest("[data-nav-group]");
  let next = group && nearest(cur, items(group), dir);
  // A neighbour can be a list ("letters,grid"): the first one on screen with something to focus.
  if (!next && group) {
    for (const name of (group.getAttribute(`data-nav-${dir}`) || "").split(",").filter(Boolean)) {
      const target = root.querySelector(`[data-nav-group="${name}"]`);
      const there = target ? items(target) : [];
      if (there.length) { next = nearest(cur, there, dir) || there[0]; break; }
    }
  }
  if (next) { next.focus(); next.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}

function emit(name) { window.dispatchEvent(new CustomEvent(name)); }

const typing = () => document.activeElement?.matches("input, textarea");

window.addEventListener("keydown", (e) => {
  // In a text box, keys type. Up/Down still leave it, and so do Left/Right once the caret is at
  // that end of the text; Enter and Escape hand focus to the results.
  if (typing()) {
    const box = document.activeElement;
    const atEdge = box.selectionStart === box.selectionEnd
      && (e.key === "ArrowLeft" ? box.selectionStart === 0 : box.selectionEnd === box.value.length);
    if (e.key === "ArrowUp" || e.key === "ArrowDown" || ((e.key === "ArrowLeft" || e.key === "ArrowRight") && atEdge)) {
      e.preventDefault(); move(DIRS[e.key]);
    }
    else if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); emit("nav-search-done"); }
    return;
  }
  if (DIRS[e.key]) { e.preventDefault(); move(DIRS[e.key]); }
  else if (e.key === "Escape" || e.key === "Backspace") { e.preventDefault(); emit("nav-back"); }
  else if (e.key === "q" || e.key === "PageUp") emit("nav-prev-tab");
  else if (e.key === "e" || e.key === "PageDown") emit("nav-next-tab");
  else if (e.key === "[") emit("nav-prev-page");
  else if (e.key === "]") emit("nav-next-page");
  else if (e.key === "/") { e.preventDefault(); emit("nav-search"); }
  else if (e.key === "r") emit("nav-refresh");
  else if (e.key === "Enter" && document.activeElement?.hasAttribute("data-nav")) {
    e.preventDefault(); document.activeElement.click();
  }
});

// Gamepad: standard mapping. A=0 B=1 X=2 Y=3 LB=4 RB=5 LT=6 RT=7, d-pad 12-15, left stick axes 0/1.
// In the search box, A and B finish the search (the on-screen keyboard is Steam's: Steam+X).
const BUTTONS = {
  0: () => typing() ? emit("nav-search-done") : document.activeElement?.click(),
  1: () => emit(typing() ? "nav-search-done" : "nav-back"),
  2: () => emit("nav-search"),
  3: () => emit("nav-refresh"),
  4: () => emit("nav-prev-tab"),
  5: () => emit("nav-next-tab"),
  6: () => emit("nav-prev-page"),
  7: () => emit("nav-next-page"),
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

// On a Steam Deck the browser sees one physical controller twice: Steam Input's
// virtual pad and the built-in controller under it. Buttons are merged across
// pads, so one press is one action. Pads with the standard layout are preferred,
// so a raw device with other button numbers can't add stray presses.
function pads() {
  const all = [...(navigator.getGamepads?.() ?? [])].filter(Boolean);
  const standard = all.filter((p) => p.mapping === "standard");
  return standard.length ? standard : all;
}

// The Gamepad API keeps reporting the controller while something else has focus. In Game
// Mode that's Steam's own menu: the d-pad and A presses that pick "Exit game" used to move
// focus here and press whatever they landed on. So nothing is done while the page isn't
// focused, and a button still held when focus comes back waits to be released.
function poll(now) {
  const connected = pads();
  if (!document.hasFocus()) {
    for (const i of Object.keys(BUTTONS).map(Number))
      if (connected.some((pad) => pressed(pad, i))) held.set(i, Infinity);
    requestAnimationFrame(poll);
    return;
  }
  for (const i of Object.keys(BUTTONS).map(Number)) {
    if (!connected.some((pad) => pressed(pad, i))) { held.delete(i); continue; }
    const next = held.get(i);
    if (next === undefined) { BUTTONS[i](); held.set(i, now + FIRST_DELAY); }
    else if (REPEATS.has(i) && now >= next) { BUTTONS[i](); held.set(i, now + REPEAT_EVERY); }
  }
  requestAnimationFrame(poll);
}
requestAnimationFrame(poll);
window.addEventListener("gamepadconnected", (e) => emit("nav-gamepad", e));

export { focusFirst };

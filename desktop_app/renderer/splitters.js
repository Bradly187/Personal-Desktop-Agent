"use strict";
// Draggable + keyboard-resizable splitters writing CSS variables on :root.
// Sizes persist in localStorage. Arrow keys move 24px per press (RA-friendly:
// no precision drag required); double-click resets to the default.

const Splitters = (() => {
  const KEY_STEP = 24;

  function init(splitterId, cssVar, opts) {
    const { min, max, defaultPx, invert = false, axis = "x" } = opts;
    const el = document.getElementById(splitterId);
    const root = document.documentElement;
    const storeKey = `split:${cssVar}`;

    const clamp = (v) => Math.min(max, Math.max(min, v));
    const current = () =>
      parseInt(getComputedStyle(root).getPropertyValue(cssVar), 10) || defaultPx;
    const apply = (px) => {
      root.style.setProperty(cssVar, `${clamp(px)}px`);
      localStorage.setItem(storeKey, String(clamp(px)));
    };

    const saved = parseInt(localStorage.getItem(storeKey), 10);
    if (!Number.isNaN(saved)) apply(saved);

    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      const startPos = axis === "x" ? e.clientX : e.clientY;
      const startSize = current();
      // Iframes swallow pointermove while hovered — pointer capture on the
      // splitter keeps the drag alive across them.
      const move = (ev) => {
        const pos = axis === "x" ? ev.clientX : ev.clientY;
        const delta = (pos - startPos) * (invert ? -1 : 1);
        apply(startSize + delta);
      };
      const up = (ev) => {
        el.releasePointerCapture(ev.pointerId);
        el.classList.remove("dragging");
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });

    el.addEventListener("dblclick", () => apply(defaultPx));

    el.addEventListener("keydown", (e) => {
      const grow = axis === "x" ? "ArrowRight" : "ArrowDown";
      const shrink = axis === "x" ? "ArrowLeft" : "ArrowUp";
      if (e.key !== grow && e.key !== shrink) return;
      e.preventDefault();
      const dir = (e.key === grow ? 1 : -1) * (invert ? -1 : 1);
      apply(current() + dir * KEY_STEP);
    });
  }

  function initAll() {
    init("split-left", "--left", { min: 160, max: 600, defaultPx: 260, axis: "x" });
    init("split-right", "--right", { min: 300, max: 800, defaultPx: 400, axis: "x", invert: true });
    init("split-bottom", "--bottom", { min: 120, max: 600, defaultPx: 240, axis: "y", invert: true });
  }

  return { initAll };
})();

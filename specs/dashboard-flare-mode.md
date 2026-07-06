# Spec: Dashboard Flare-Mode Toggle

> Closes IG-14 requirement for a reduced-motion / high-contrast / large-target mode tailored for rheumatoid arthritis flare days.

---

## 1. Background — the "Why"

The Desktop Agent is heavily used by an operator with rheumatoid arthritis (RA). While the gesture and voice sensors are adaptive to RA flare days, the observability dashboard at `:8770` is not. On a high-pain day, reading 12.5px dense logs or trying to click 28px tool targets with a trackball or compromised hand mobility is actively painful.

We already have a system-level measure of the operator's physical state: the `pain_day_score` (0.0 to 1.0) emitted by the `BehavioralTwinState` and available on the `/api/metrics` gauge `pain_day_score`.

**Status:** Draft / Active
**Owner:** Antigravity

---

## 2. Requirements

### Requirement 1: Automatic Toggle
1. The dashboard JS (`web_client_chat/dashboard.js`) SHALL observe the `pain_day_score` from the periodic `/api/metrics` poll.
2. WHEN `pain_day_score >= 0.6`, the dashboard SHALL automatically append the `.flare-mode` class to the `<body>` element.
3. WHEN `pain_day_score < 0.6` or the gauge is absent, the dashboard SHALL remove the `.flare-mode` class (falling back to OS-level preferences).
4. The threshold logic SHALL NOT be duplicated across multiple JS files.

### Requirement 2: Visual Adjustments (`.flare-mode`)
1. Base font size (`--base-font` via `rem`) SHALL increase by at least 20%.
2. All interactive targets (`.tool`, `.approval button`, `.trace-item`) SHALL enforce a `min-height` and `min-width` of 44px, meeting WCAG 2.5.5 Target Size.
3. Contrast for secondary text (`--text-dim`) SHALL increase to meet WCAG 2.2 AA contrast ratios against the background.
4. Spacing (padding/gap) around lists and feed items SHALL increase to prevent mis-clicks.

### Requirement 3: Accessibility Foundation
1. The CSS SHALL use `@media (prefers-reduced-motion)` to disable animations (e.g., the streaming cursor blink).
2. The CSS SHALL use `rem` for all text and target sizing instead of hardcoded `px`, ensuring OS/Browser magnification scales correctly.

---

## 3. Technical Design

- **CSS Variables:** Introduce `--base-font` on `:root`, initialized to `15px`. In `.flare-mode`, set to `18px`.
- **rem conversions:** Update `12px` to `0.8rem`, `15px` to `1rem`, `22px` to `1.46rem`, etc.
- **Target sizing:** Add `min-height: 44px;` and `min-width: 44px;` for `.flare-mode .tool`, `.flare-mode .approval button`, and `.flare-mode .collapse-btn`.
- **JS Hook:** In `refreshMetricsSoon()` or `refreshMetrics()` inside `dashboard.js`, check `g.pain_day_score` and toggle `document.body.classList.toggle('flare-mode', score >= 0.6)`.

No backend Python changes are required, as `pain_day_score` is already emitted by the `/api/metrics` endpoint.

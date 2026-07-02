# Tasks: Desktop App Shell (Electron)

> Gate 2 approved with the spec (Brad, 2026-07-02, in-session plan approval).
> Requirement numbers reference `requirements.md` §3.

- [x] 1. Repo prep: `desktop_app/node_modules/` in `.gitignore`; `--chat-no-browser` flag in `main.py` — satisfies R2.6
- [x] 2. Scaffold `desktop_app/` npm project; `npm install`; node-pty rebuilt against Electron ABI (postinstall `scripts/rebuild-pty.js`)
- [x] 3. Main process: window + `main/auth.js` token header injection (+ fs.watch reload) — satisfies R1.2, R1.3, R1.4
- [x] 4. Main process: `main/backend.js` lifecycle state machine (attach/spawn/poll/taskkill-tree, owned-only kill) — satisfies R2.1–R2.5
- [x] 5. Main process: `main/pty.js` session registry + `main/fs-ipc.js` (drives, listDir, read w/ size+binary guards, write, stat) + `preload.js` contextBridge — satisfies R3.3, R4.1, R4.4
- [x] 6. Renderer: grid layout + splitters (pointer capture, keyboard resize, persistence) + tab manager + chat/dashboard iframes (health-gated src, hide-not-unmount) — satisfies R1.1, R1.5, R1.6, R5.1–R5.3, R5.5
- [x] 7. Renderer: file tree (drives, lazy expand, error rows) — satisfies R3.1, R3.2, R3.5
- [x] 8. Renderer: Monaco editor (AMD local load, worker proxy, models/tab, dirty + Ctrl+S + mtime conflict, dirty-close prompt) — satisfies R3.2–R3.4, R3.6
- [x] 9. Renderer: terminal (xterm+fit, resize propagation, exit/respawn) + backend panel (pill, log tail, Stop disabled when attached) — satisfies R4.1–R4.3, R2.4, R2.5
- [x] 10. Shortcuts incl. Menu accelerators — satisfies R5.4
- [x] 11. `tests/test_chat_no_browser_flag.py` — satisfies R2.6 verification
- [x] 12. Live verification (attached mode verified via SHELL_SMOKE self-test: zero 401s, 2 WS upgrades, editor+pty+tree live; owned mode post-merge when backend is down) + update `CLAUDE.md` Run Commands + `docs/file-map.md`

## Verification checklist

1. Backend already running → shell shows **attached**; chat iframe sends/receives; dashboard populates; zero 401s in devtools Network; quit leaves `/health` at 200.
2. Backend down → shell spawns, `starting → owned`, log tail streams, no browser tab (`--chat-no-browser`); quit kills the python pair. *(post-merge — do not kill the live agent to test this)*
3. Token file removed → banner, graceful 401s; restored → recovery on reload.
4. Terminal: prompt at repo root; window resize clean; `exit` → banner → Enter respawns.
5. Editor: open/edit/Ctrl+S round-trip; >5 MB warn; binary refusal; dirty-close prompt; mtime conflict prompt.
6. Tree: all drives listed; access-denied dir → inline error row.
7. Splitters: coarse drag, keyboard resize, double-click reset, sizes survive restart; `Ctrl+1/2` works with iframe focus.
8. `git status` free of `node_modules`/`dist` noise.

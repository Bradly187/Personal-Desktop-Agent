"""--chat-no-browser flag (spec: desktop-app-shell R2.6).

The Electron shell (desktop_app/) spawns `main.py --chat --chat-no-browser`
and embeds the chat UI itself; without the flag every owned spawn would pop a
browser tab via _open_chat_shell.
"""

import sys

import main


def test_r2_6_chat_no_browser_parses(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--chat", "--chat-no-browser"])
    args = main._parse_args()
    assert args.chat_no_browser is True


def test_r2_6_chat_no_browser_defaults_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--chat"])
    args = main._parse_args()
    assert args.chat_no_browser is False

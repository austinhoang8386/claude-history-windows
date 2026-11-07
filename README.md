# Claude History Transfer for Windows — v0.0.1

[Tiếng Việt](README_VI.md) · [Download (Releases)](https://github.com/austinhoang8386/claude-history-windows/releases)

![Claude History Transfer — English UI](docs/images/screenshot-en.png)

**Languages: English (default) and Vietnamese (Tiếng Việt).** This community-built Windows 10/11 desktop app helps copy or move **local Claude Desktop Code session cards** between two Claude account folders on the **same computer**. It is not affiliated with Anthropic or the upstream `claude-transplant` project.

## Quick start

1. Install **Python 3.10 or later** from https://www.python.org/downloads/windows/ and tick **Add Python to PATH**. The app uses wxPython (native Windows controls, readable by NVDA, JAWS and Narrator); `Start-Windows.bat` installs it with pip if missing.
2. Extract the ZIP and double-click **`Start-Windows.bat`**.
3. In Claude Desktop, sign in to the **source** and **destination** accounts at least once each. Open the Code tab to create session folders, if necessary.
4. **Completely quit Claude Desktop from the system tray and close all Claude Code terminals.**
5. In the app, select the correct `claude-code-sessions` root, then select **FROM** (source) and **TO** (destination). Account IDs are abbreviated, so verify account folders before transferring.
6. Select one or two sessions and click **Copy selected (safer)**. Restart Claude Desktop, switch to the destination account and confirm that the sessions show up and can be opened.
7. Continue with Copy or Move only after the initial check. To revert the latest operation, quit Claude and use **Undo most recent transfer**.

### Language settings

- The first launch displays **English by default**.
- Use the **Language** dropdown in the top-right corner to switch between **English** and **Tiếng Việt**. Interface labels, session validation statuses, warnings and confirmation dialogs are translated instantly.
- Your choice is saved in `%LOCALAPPDATA%\ClaudeHistoryWindows\settings.json`, so reopening the app restores your chosen language. Delete this settings file to reset to English (do not delete the `jobs` directory).
- The alternate-language guide is `README_VI.md`.

## Build a standalone Windows executable

Double-click **`Build-EXE-Windows.bat`** on a Windows machine. The script installs PyInstaller (internet required) and packages an executable at `dist\ClaudeHistoryTransfer.exe`.

A prebuilt `ClaudeHistoryTransfer.exe` is attached to each [GitHub release](https://github.com/austinhoang8386/claude-history-windows/releases). The source ZIP contains source files and build scripts only; Python is necessary when running the source through `Start-Windows.bat`.

## Supported data locations

- `%LOCALAPPDATA%\Claude-Data\claude_*\claude-code-sessions` (newer MSIX layout)
- `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code-sessions` (older MSIX layout)
- `%APPDATA%\Claude\claude-code-sessions` (Win32 layout)
- A manually selected folder **named** `claude-code-sessions`

Transcripts are **not copied, moved or edited**: Claude Code transcript content typically lives at `%USERPROFILE%\.claude\projects\...\<cliSessionId>.jsonl`. Back up your full `%USERPROFILE%\.claude\projects` directory separately before making changes.

## What the app does

- Reads only local `local_<uuid>.json` session cards and checks their JSON structure, session IDs, local transcript references and scheduled-task references. Cards that fail validation are not selectable.
- **Copy** adds identical session cards under a second existing account folder and does not alter the source cards.
- **Move** copies cards, verifies SHA-256 checksums, then removes source cards after saving verified backups.
- Conflicting destination files, duplicate CLI IDs and unresolved fork dependencies cause the operation to stop without overwriting those cards.
- Receipts, backups and Undo state are stored in `%LOCALAPPDATA%\ClaudeHistoryWindows\jobs`. **Keep this directory to retain Undo capability.**
- Undo restores the source in Move mode, and only deletes destination cards when they still match the transferred copy. It refuses to erase edited sessions.
- Does not require WSL or Claude API keys and does not upload session data to any server.

## Known limitations

- **Only local Claude Desktop Code session card metadata is transferred.** This does *not* migrate claude.ai web conversations, transcript files, Remote Control/cloud sessions, server-side attachments, authentication, or complex scheduled task metadata.
- This is not a feature-complete Windows port of `claude-transplant`; several advanced upstream behaviors are deliberately outside scope.
- Works between account folders on the same Windows installation. Cross-computer migrations need separate transcript transfer and compatible project paths.
- Claude Desktop may change its storage format. This version has been tested against **synthetic fixture files**, not yet against your particular Claude Desktop installation. Start with a Copy of one or two sessions.
- Avoid opening and modifying the same copied session under multiple accounts simultaneously.

## Troubleshooting

- **No accounts found:** sign in to both accounts, open the Code tab and quit Claude Desktop; use **Rescan** or manually pick a root.
- **Missing transcript:** check `%USERPROFILE%\.claude\projects`. A card alone does not hold the conversation's actual content.
- **Copied sessions not shown:** check destination account and organization IDs and the chosen session root; restart Claude Desktop.
- **Undo refused:** session cards may have been edited after the move. Review the operation's `receipt.json` and backups; avoid deleting files manually.
- **Python not found:** install Python with PATH enabled, then retry the startup script.

Architecture references: https://github.com/vitaliyhayda/claude-transplant and https://github.com/sahol3/claude-code-session-restorer

## License

[MIT](LICENSE)

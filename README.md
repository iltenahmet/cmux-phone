# cmux-phone

Use your Mac's cmux panes from your phone. It's a web page served over Tailscale with named buttons, so you don't need key combos.

It's for checking on and replying to Claude Code agents running in cmux panes. It is [tmux-phone](https://github.com/The-Sentience-Company/tmux-phone) with cmux in place of tmux: the same page, the same buttons, the same look.

## How cmux maps to the page

| On the page | In cmux |
|---|---|
| session | a cmux window, numbered as cmux lists them |
| window | a workspace |
| pane | a terminal tab (cmux calls it a surface) |

Browser tabs are not listed.

## What it does

- **Pane list:** every terminal in the cmux window, drawn as a tree under its workspace (`├─`, `└─`). Each shows its title, its last line, and a status for Claude Code, Codex and Cursor panes at the right of its title (`needs you` / `working` / `idle`, plain text). Refreshes every 3 s.
- **Open a pane:** tap it. The screen is a real terminal on the page (xterm.js). The server reads the pane's screen from cmux about 12 times a second and sends it when it changes; what you type goes straight to the pane. Type into it as on the Mac. It reconnects by itself if the connection drops.
- **The Mac stays put:** opening a pane on the phone does not switch the workspace or the focus on the Mac. A workspace or pane made on either shows on both.
- **Size:** while a pane is open on the phone, it is sized to the phone, on the Mac too, so text wraps at the phone's width. It goes back to its own size when you go back to the list, or about 15 s after the phone drops off.
- **Strip:** the strip at the very bottom of the screen is the iPhone home-indicator area. Under a dim line, below the key bar and below the list, it names the open pane's workspace as `2: agent`, or in the list the cmux window as `session 1`.
- **Buttons:** Close pane in the pane view.
- **Files:** `[files]` in the pane view opens the pane's folder (`..`, folders, then files, hidden ones too), or the file open in the pane's nvim. A file opens read-only with highlighting and line numbers, so it scrolls and selects like a web page. `[edit]` brings up the keyboard, `[save]` writes it, unless the file changed on disk since it was opened. An nvim on the Mac with that file open reloads it. Files over 2 MB, binary and non-UTF-8 files show one line instead. Pinch sets the font size, the same one the terminal uses. CodeMirror 6 is bundled from `vendor-src/` (`npm ci && npm run build`).
- **Edit:** `[edit]` on the list puts each row's buttons at its right, cutting the name to make room: a session `[x]`, a workspace `[rename] [+ pane] [x]`, a pane `[rename] [x]` on its title line. `[+ session]` follows the last session, `[+ window]` the last workspace. `[x]` asks first. Tapping a row opens nothing. Rename is inline: Enter or tapping away saves, Escape cancels. `[+ pane]` splits beside a workspace's one pane, else under its last pane, and opens the new pane. `[+ window]` makes a workspace with a shell in your home folder. The list does not redraw on its 3 s refresh while in edit mode, so a tap is never lost. Claude Code sets its own pane title, so it may rename a renamed Claude pane back.
- **Zoom:** pinch the terminal to change its font size (8 to 20). The pane re-fits to the new size. The size is kept for next time.
- **Scroll:** swipe on the terminal. It works like the mouse wheel on the Mac: an app that reads the mouse gets the wheel (Claude Code scrolls its conversation), and in a shell the phone scrolls back through the pane's history. The text follows the finger; an app that reads the mouse moves in steps of three lines, the smallest step cmux sends. Scrolling a shell's history on the phone does not scroll the pane on the Mac. While scrolled back, `[↓ bottom]` shows at the bottom right of the terminal; tap it to go back to the bottom. Typing, a key bar key, or a paste goes back to the bottom too.
- **Photo:** tap it to open the photo picker. The picked image is pasted into the pane's input. Claude Code shows it as `[Image #N]`.
- **Paste:** `[paste]` pastes the iPhone clipboard into the pane, without Enter; iOS shows its own Paste button to tap first. An image (e.g. a screenshot after "Copy and Delete") goes in like a photo. Text goes in through cmux's paste path, the one Cmd+V uses, so multi-line text arrives as one bracketed paste and is not submitted line by line. Long-press on the terminal is taken by scrolling, so this is the only way to paste text.
- **Key bar:** one scrolling row in three groups: `[mic] [photo] [paste] [enter] │ [ctrl] [alt] │ [esc] [↑] [↓] [←] [→] [0] [1] [2] [3] [tab] [shift-tab] [ctrl-c] [model]`. Mic and photo are icons; the rest are words. model is alt+p. Bouncing dots mean a recording, photo or paste is being sent. Tapping the bar keeps the keyboard open.
- **Ctrl and Alt:** tap one, then type a key on the phone's keyboard: Ctrl then c is Ctrl-C. It turns off after one key; tap it again to turn it off sooner.
- **Updates:** the page reloads itself when `index.html` or `server.py` changes on the Mac, and reopens the pane you were on. It waits while you record, send a photo or paste, or rename. The server hashes both files into a version; the page checks it on every 3 s poll, in the list and in a pane.
- **Awake:** plugged in, the Mac stays awake while cmux-phone runs (`caffeinate -s`); the display can still sleep. On battery or with the lid closed it still sleeps.
- **Keyboard:** the page is pinned to the area above the iOS keyboard, so the header, the terminal (refit, so the pane reflows) and the key bar all stay visible.
- **Look:** a terminal look throughout: monospace, square, `[bracketed]` header buttons.
- **Theme:** `[theme]` on the list opens the themes, each row drawn in its own colors. A tap sets it at once, for the terminal's 16 colors and the page around it. Monokai is the default. Also gruvbox-dark-hard, gruvbox-dark, the four Cursor dark and light themes, dracula, catppuccin-mocha, tokyo-night, nord, one-dark, solarized-dark, and xterm (the old black look). Kept in this browser, like the font size. Apps that draw in their own 24-bit colors keep them.
- **Dictation:** tap mic, talk, tap it again (it shows the time while recording). Whisper large-v3-turbo transcribes it on the Mac in about a second and pastes the text into the pane. Nothing is submitted until you press Enter. The audio never leaves the Mac.

## Sessions

A session on the page is a cmux window. The list shows the workspaces of one cmux window, the phone's target. With more than one cmux window, a row at the top reads `session: [1] [2]`, the target in reverse video. Tap another to switch. The target is kept across page reloads and server restarts (`~/.local/state/cmux-phone/target`).

- **Default:** the first cmux window.
- **Target gone:** if the target window is closed, the phone moves to the first one left.
- **Edit mode:** sessions show one per row, the phone's in reverse video, each with `[x]`. `[+ session]` opens a new cmux window on the Mac and switches the phone to it. `[x]` asks first and says how many workspaces close with it. cmux windows have no names, so there is no rename.

## What differs from tmux-phone

- **No hidden phone session.** tmux-phone attached its own tmux client. cmux has no second client, so the page draws the pane from the screen cmux reports. An escape sequence the app sent never reaches the phone as such; the phone gets the result.
- **No zoom.** A pane is sized to the phone instead of zoomed inside its window.
- **Sessions can't be renamed.**
- **Scrolling a shell is the phone's own**, as above; tmux-phone scrolled tmux's copy mode, which the Mac saw too.

## Install

Needs macOS, Homebrew, cmux, and the Tailscale app signed in on both the Mac and the phone.

In cmux, set Settings → Automation → socket access to **Automation mode**. A launchd job is not started from inside cmux, and cmux only answers such a program in that mode. It lets any program running as your macOS user control cmux.

```
brew install whisper-cpp ffmpeg
./install.sh you@example.com     # your Tailscale login
```

The installer:
- gets the speech model (574 MB) into `~/.local/state/cmux-phone/models`: a hard link to tmux-phone's copy if you have it, else a download
- installs the launchd service `com.cmux-phone`, which starts at login and restarts if it crashes
- publishes it with `tailscale serve`, on the tailnet's port 443, or on the port in `CMUX_PHONE_HTTPS_PORT` to run beside something already on 443

The first time, Tailscale may print a link to turn on Serve for your tailnet. Open it, enable Serve, and the installer continues.

On the phone, open the URL it prints. Then tap Share → Add to Home Screen.

Re-run `./install.sh` after changing the code, or restart the service:

```
launchctl kickstart -k gui/$(id -u)/com.cmux-phone
```

Logs: `~/.local/state/cmux-phone/server.log`.

## Tests

- `./smoke_test.py <url>` runs against the live install, through the tailnet URL; `install.sh` ends with it. It only reads: the pane list matches cmux's, the terminal stream shows a pane's screen, dictation returns the spoken words, other websites get 403.
- `cd tests && npm ci && npx playwright install webkit && node browser_test.mjs` drives the page as an iPhone in WebKit against a copy of the server started with `--dev`. It runs in your real cmux, only inside a workspace it makes and closes itself.
- `python3 tests/agent_screens_test.py` checks the status parser against screens captured from Codex and Cursor.

`python3 server.py --dev` serves on localhost with no Tailscale login check and no dictation, for work on the page.

## Security

Anyone who gets in has a full shell on the Mac, so:

- **Tailnet only:** `tailscale serve` answers only devices on your tailnet. Never use `tailscale funnel`.
- **One login:** the server accepts only the Tailscale login given to `install.sh`. It checks the `Tailscale-User-Login` header that `tailscale serve` adds.
- **Same origin only:** POSTs must come from the app's own origin, so another web page open on the phone can't type into your terminal.
- **Local only:** the server listens on `127.0.0.1:8768`, whisper-server on `127.0.0.1:8769`.

## Assumptions

- cmux is running, with its socket at `~/.local/state/cmux/cmux.sock` (or `CMUX_SOCKET_PATH`).
- A pane counts as a coding agent if a process in its foreground is named `claude` (Claude Code), `codex` (OpenAI Codex CLI) or `agent` / `cursor-agent` (Cursor CLI). The status labels read the agent's screen:
  - **Claude Code:** `needs you` when the `❯` input box is gone, `working` while the spinner line (`✻ Thinking…`) shows.
  - **Codex:** `needs you` when the hint under its `›` line says `to confirm`, `to submit answer` or `enter continue`; `working` while `Working (5s • esc to interrupt)` shows.
  - **Cursor:** `needs you` when the `→` input box between its `▄` and `▀` bars is gone; `working` while that line ends in `ctrl+c to stop`.

  The last line is the start of the agent's latest reply, or for Cursor the last line above its input box. A UI change in any of them can break this.
- The server uses cmux's socket methods (`system.tree`, `terminal.replay`, `terminal.input`, `terminal.paste`, `terminal.size_policy.set` and a few more). Their parameters are not documented, so a cmux update can break them; the browser test shows what broke.

## Uninstall

```
launchctl bootout gui/$(id -u)/com.cmux-phone
rm ~/Library/LaunchAgents/com.cmux-phone.plist
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=443 off
rm -rf ~/.local/state/cmux-phone
```

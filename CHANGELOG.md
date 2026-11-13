# Changelog

## Unreleased

- The log is always written to %LOCALAPPDATA%\StickAI\stickai.log (it wasn't when started from StickAI.exe).
- **Arrivals:** a summoned character drops from the top, walks or runs in from a side, or jumps up out of the taskbar.
- **Chat:**
  - select and copy messages (right-click menu);
  - addressee chips (one character, several, or "Everyone");
  - a stop button that cancels the answer being thought up.
- **Right-click a stickman:** a message box just for him.
- **Notifications** in a chosen corner while the chat is closed, with an optional sound.
- Fixed: typing in the chat box could freeze the app for seconds ("Not responding"). The auto-growing box and its
  scrollbar fed back into each other (scrollbar -> narrower -> re-wrap -> taller -> scrollbar...). Now there is no
  scrollbar and the box is resized once per change.
- **Work shows in the animation:** while a step runs they hold a prop:
  - a laptop for the terminal, VS Code and the code helper;
  - a magnifier for searching;
  - a box for file operations;
  - an envelope for mail;
  - a map for Maps;
  - a remote for YouTube and media.
- **Landing animation:** a dip, a deep crouch or a three-point landing depending on the fall, with dust puffs.
- **Fishing rod:**
  - In peace, the stickmen reel themselves up onto window edges above, or hang from the top of the screen.
  - In fights, they hook the opponent and yank him in, and run away up a window.
  - The rod, the line (slack or taut) and the hook are drawn.
- **Fights** («деритесь», "fight!", «Red, побей Blue»). The stickmen charge, punch, kick, do flying kicks, block,
  get knocked back and run away, with hit sparks and kick swooshes. «хватит драться» or "stop" ends it, and the
  winner celebrates. New poses: fighting stance, punch, kick, block, stagger, flying kick, knocked back.
- The music show starts only when your message asks a character to play music (checked in code). It no longer
  starts on its own when Green opens a YouTube video or does anything else.
- **Browser skill:**
  - YouTube: search, play the first video, your feeds;
  - Gmail: inbox, unread, search, and compose a **draft** (you press Send);
  - Google Maps: places and directions by car, on foot, by transit or by bike;
  - tabs: list, switch, close;
  - links: click one on the page;
  - page text: read it when asked, with an opt-in setting.

  Common phrases work as quick commands, with no AI request.
- Step fields shared by several skills (query, to, from…) now describe every use, not just the first skill's.
- **Creating files and folders** anywhere in your folders (`create_files`): empty files or files with text,
  never over an existing name, always with Allow; "undo" moves only untouched new files to the Recycle Bin.
- "Open folder CV6" finds a folder by name and opens it in one step. Found and opened folders tell the model
  what's inside, so follow-ups like "3 more files like this one" work.
- **Open in VS Code:** the stickmen open folders (projects) and single files in VS Code by name or path
  ("открой проект StickAI в VS Code" works locally, with no AI request).
- **Renaming:** one file or folder by an approximate name (`rename_path`), or many at once with a pattern
  (`manage_files` op `rename`: `{n}`, `{name}`, `{date}`). Extensions are kept, names are never replaced,
  it always asks, and "undo" puts the old names back.
- **StickAI.exe** (launcher/build.py): starts StickAI without a console window. With no console, the log is written to
  %LOCALAPPDATA%\StickAI\stickai.log. A second start only says StickAI is already running.
- Alt+` brings back a chat window that was minimized with its title-bar button.
- A character playing the music show no longer also presses play/pause, which paused your own player.
- No placeholder text in the chat box or the VS Code question box.
- **Code helper in VS Code:** a StickAI extension (vscode/, packed without Node.js) with four commands:
  * **Explain** (Ctrl+Alt+E);
  * **ask questions** in the panel (Ctrl+Alt+Q);
  * **add comments** (Ctrl+Alt+C), shown as a diff to accept. Only comment changes are allowed; anything else gets a warning;
  * **What's this error?** from the light bulb.

  The chat understands "explain / comment / what's this error" while VS Code is in use.
  The link is local only (127.0.0.1 + token).
- **Files skill:** move / copy / delete (Recycle Bin) / list files by type, name and date
  ("move all today's PDFs from Downloads to Documents"), with a file list in the Allow dialog and undo.
- Removed the Spotify-specific workarounds (spotify: links and Spotify prompt hints); Spotify gets its own skill later.
- **Skills** (stickai/skills/): System (media keys, close / force-close / hide / show apps by name),
  Explorer (find files and folders, open known folders), Terminal (own window, folders, Git with Allow for changes).
- **Quick commands** in English, Russian and Slovak run locally with no AI request.
- **Action mode:** requests to do things use Flash first at a low temperature; overloaded models are skipped at once.
- Store apps (Calculator, Settings...) are recognised; force-closing one never kills the shared frame host.
- **One automatic look after acting:** numbered buttons (`B3: …`) of the window in front, matches first;
  `click_button` by number, with the remembered position as a fallback for flaky Chromium apps.
- Plans are all-or-nothing, with precise reasons sent back when a step is unusable; fewer wasted rounds.
- Media rule: Space only pauses or resumes; to play something specific they click its own Play button.
- **Open any installed app** by name from the Start menu (desktop and Store apps), or bring it back if it's
  already open. Follow-up rounds include the window's button labels.
- Stricter honesty: a reply that promises an action must contain its steps.
- **Plans and follow-up rounds:** several steps with one approval; failed steps (or `continue`) go back to
  them with the results (and a screenshot if screen viewing is on) so they can fix or finish the job.
- **Flexible steps:** `open_url` (web search, new window), `press_keys`, `type_text` into any window with Enter,
  `click_at` (what they see), `wait`.
- Window list names the app (`Chrome: "…"`), duplicate fields removed; schema field descriptions for weaker models.
- They no longer claim to have done things they didn't do.
- **New tools** (see README → More tools): click a button by name, scroll (and read) a page,
  arrange windows, open apps from your allow-list, spell-check another app's text field on this PC,
  clipboard helper, timers and reminders.
- The chat window steps aside when it covers what a task needs to click (also fixes typing tasks).
- **Spell-check in the chat box** (Windows spell checker; suggestions, Add to dictionary, Ignore).
- **Music on speakers while you wear headphones:** choose the music show's output device, or let it
  switch to speakers automatically when headphones are the default.
- **A Gemini key per character** (Settings → Characters → Own Gemini key); requests for a character use their key first.
- **Backup Gemini API keys** (`backup_api_keys` in secrets.toml), used only when a key runs out or fails.
- Code grouped by purpose: `tools/` (things they do for you) and `activities/` (shows).

## 1.0.0 (2026-11-11)

First public release.

- **On the desktop:** procedural stickmen that walk, run, jump, sit and climb windows, ride windows you drag,
  hop onto search bars and web-page elements, and can be grabbed and thrown with the mouse.
- **Talking (Gemini):** four characters with their own personalities, chat window with pictures,
  banter between characters, reactions, long-term memory that survives restarts. English, Russian, Slovak.
- **Opt-in abilities, each behind Allow / Deny:** close / minimize / open windows, type into fields,
  play click-target games, create and edit text files in their own sandboxed folder, look at your screen.
- **Fun:** music show with your own song, couch time while you watch videos.
- **Settings window** for everything, including the new *Say hello when they arrive* option
  (off: called characters drop in silently).
- One dark look everywhere: styled confirm / permission dialogs, dark title bars and tray menu.

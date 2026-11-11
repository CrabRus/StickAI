# Changelog

## Unreleased

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

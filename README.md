# StickAI

Little stickmen that live on your Windows desktop. They walk along the taskbar, climb and sit on your windows, hop onto search bars and web pages, and can be grabbed and thrown with the mouse. Give them a free Gemini API key and they talk: each has a personality, they chat with you and with each other, and they remember you between sessions.

**Version 1.0** · Windows 10/11 · Python 3.12+ · see [CHANGELOG.md](CHANGELOG.md)

## Install and run

```
git clone <this repository>
cd StickAI
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start.bat
```

(`start.bat` just runs `.venv\Scripts\python main.py`.) On the first run StickAI creates your own `characters.toml` from [stickai/defaults/characters.default.toml](stickai/defaults/characters.default.toml). It's git-ignored, like `secrets.toml` and the `memory/` folder, so your settings, key and chats never end up in the repository.

Options:
- `--debug`: show window outlines and the HUD at start. They're off by default because they cost frame time; the tray menu toggles them.
- `--spawn yellow blue`: start with these characters on screen.
- `--characters path.toml`: use a different character file.
- `--trace`: log state.
- `--exit-after N --snapshot out.png`: for testing.

Use the **tray icon** to talk, edit characters, toggle debug, pause, respawn and quit. The chat window also has a **⏻** button that quits (after asking).

**Alt+Shift+`** hides everything at once: the stickmen, their bubbles, the chat and the settings window. While hidden, they freeze in place and don't chat among themselves. Press it again to bring everything back exactly as it was. Alt+` (chat) and the tray icon also bring them back. You can change both hotkeys in Settings → General. Ctrl+C in the console also quits.

## Talking to them (Gemini)

1. Get a free API key at <https://aistudio.google.com/apikey>.
2. Give it to StickAI, either:
   - as an environment variable (then open a new terminal):
     `setx GEMINI_API_KEY "your-key"`
   - or in a `secrets.toml` next to `characters.toml` (git-ignored; copy [secrets.example.toml](secrets.example.toml)):
     `gemini_api_key = "your-key"`
   - **A key per character (optional):** Settings → Characters → **Own Gemini key**, or a `[character_keys]` table in `secrets.toml` (`yellow = "..."`). One request voices everybody present, so each request uses the key of the character it's *for*: the first one you named in your message, the one you threw, or, for banter and messages to nobody in particular, the present characters take turns. If that key fails or runs out, the shared key and backups are used. Keys are stored only in `secrets.toml`, never in `characters.toml`.
   - **Backup keys (optional):** `backup_api_keys = ["key2"]` in `secrets.toml`. They're tried in order only when the key before is out of quota, rejected, or can't use any of the models; a key that ran out rests for 10 minutes. This doesn't make answers faster. It's useful for a paid key behind your free one. Keys from the same Google Cloud project share one quota, and Google's terms don't allow extra projects just to get around limits.
3. Run the app and press **Alt+`** (or click the tray icon) to open the **chat window**, then type **hi yellow**. Yellow drops in and answers, both in the chat window and in a yellow speech bubble above his head. Click a bubble to dismiss it.

The chat window:
- **Messages:** Enter sends, Shift+Enter starts a new line. The window's X just hides it; Alt+` brings it back.
- **Colours:** each character's messages are in their colour. "Red → Blue" means Red is talking to Blue.
- **Spelling:** misspelled words get a red wavy underline. Right-click one for suggestions, *Add to dictionary* or *Ignore*. It uses the Windows spell checker, so it works for every language installed in Windows (English, Slovak, Russian, Ukrainian…) and even in mixed messages. Names of the characters are never flagged. Settings → General → *Check spelling in the chat box*.
- **Pictures:** use the 📎 button, paste with **Ctrl+V**, or drag an image file onto the window. They see it (Gemini is multimodal) and remember what it showed.
- **History:** the whole history is there, including earlier sessions, with a date line between sessions.

Things to try:
- **hey guys** summons everyone.
- **bye red** makes Red say goodbye and leap off the screen.
- Grab someone with the mouse and fling them: they'll have something to say about it.
- Leave two or more on the desktop and stay quiet: every few minutes they chat with each other, walk over to each other, and continue running jokes.

### Memory (survives restarts)

Everything lives in the `memory/` folder next to `characters.toml` (git-ignored):

| file | what it is |
|---|---|
| `chat_log.jsonl` | Every message, picture and notable event. The latest lines are sent with each request, so a restart picks up where you left off. |
| `memory.json` | Long-term memory. Every `summarize_every` new lines, Gemini folds the chat into facts: shared facts about you and the group, plus a notebook per character. These facts go into every prompt. It's readable JSON, so edit it if they remember something wrong. |
| `images/` | The pictures you sent. |

They also know the current date and time, and how long you were away.

To start over, quit the app and delete `memory/`.

### How it works

1. **Summoning** happens locally: any character name or alias you type, or a group word like "guys", drops that character in. No AI is needed for this.
2. **One request voices everyone present.** The structured JSON reply is `{"replies": [{"speaker", "to", "text", "action"}], "image_note"}`. Because `to` can name another character, they talk to each other, not just to you. Request kinds:
   - **chat:** you said something, possibly with a picture.
   - **banter:** you've been quiet and two or more of them are around.
   - **react:** you threw someone.
   - **summary:** memory upkeep.

   Requests run one at a time. Your messages always win.
3. **Context sent:**
   - every character sheet;
   - long-term memory;
   - the time;
   - who is present and what each is doing (on the taskbar, climbing a window, being held by your cursor...);
   - recent events;
   - the last few chat lines.

   Window titles are sent only if you set `share_window_titles = true`.
4. **Actions:**
   - `wave`
   - `jump`: crouch, leap, tuck, land.
   - `run`
   - `sit`: on the floor, or on a window's edge with legs dangling.
   - `approach`: walk over to whoever they're talking to, or run if they're far away.
   - `leave`: jump off the top of the screen.

   Speakers also turn to face the character they're addressing.
5. **Models** are tried in order:
   - a brief overload (HTTP 503) is retried twice;
   - rate limits, timeouts or an unknown model move on to the next model.

   Free-tier quotas are small (reportedly about 20 requests/day for the newest Flash, more for Flash-Lite), so the default list starts with Flash-Lite. Banter and summaries also use requests; tune `banter_every` and `summarize_every` if you run out. Check your real limits in AI Studio.

### Settings window (⚙ in the chat window, or tray → Settings)

- **General:**
  - **Language:** English, Русский or Slovenčina. This is the app's language, and they speak it too.
  - **Above their heads:** full speech bubbles, just a **!** or **?** (read the text in the chat), or nothing at all.
  - **Say hello when they arrive:** on by default. Off: a character you call drops in silently. A message that only calls them ("yellow", "hi guys", "red, come here") gets no reply and uses no Gemini request; if you also ask something, they answer it without greeting.
  - **Other options:** banter and its interval, throw comments, chat window always on top, sharing window titles, the hotkey, and the Gemini models.
- **Characters:**
  - **Properties:** name, aliases, colour, size, walking speed, **energy** (low: sits around; high: runs and jumps), climb chance, restlessness.
  - **Text:** personality and voice in roomy, readable editors.
  - **Memory:** what each character remembers about you.
  - **Call / send home** button.
  - Colour and size change on the desktop as soon as you save.
- **Memory:** what they all remember, how much is stored, open the folder, and **Forget everything**.
- **Reset to defaults:**
  - The buttons are **Reset these settings** (General tab), **Reset this character** and **Reset all characters**.
  - They restore the shipped originals from `stickai/defaults/characters.default.toml`.
  - A reset only fills in the form. **Save** applies it; **Revert** undoes it.
  - Characters you added yourself have no default, so their reset button is disabled.
- The settings window is a **modal child of the chat window**: it always sits on top of the chat. While it's open, the chat and the stickmen on the desktop are blocked until you close it. The chat hotkey and the tray icon just bring Settings to the front.

**Save** writes `characters.toml`, which you can still edit by hand. It's re-read when saved, with comments regenerated, and gets these keys:
- **Settings:** language, bubbles, greet_on_arrival, hotkey, models, temperature, start-up cast, group words, history length, privacy switch, shared world lore, banter, throw reactions, memory summarizing, chat window always-on-top.
- **Per character:** display_name, aliases, color, size, walk_speed, energy, climb_chance, restlessness, personality, voice.

Add a `[characters.purple]` table to create a new stickman.

## Stack

Python 3.12+ · PySide6 (Qt 6) for windows and QPainter rendering · ctypes for Win32/DWM · comtypes (UI Automation) · numpy (aim games).

## Layout

Grouped by what each part does:

```
main.py                    entry point
start.bat                  runs main.py with the project's .venv
stickai/
  app.py                   wiring: QApplication, frame + polling timers, tray, CLI
  config.py                size, colours, poll rate
  i18n.py                  English / Russian / Slovak strings
  world.py                 simulation container, fixed-substep physics
  overlay.py               per-monitor transparent click-through window
  render.py                QPainter drawing: stickman, debug outlines, HUD

  anim/                    HOW THEY MOVE
    mathutil.py            Vec, damping, 2-bone IK, Bezier helpers
    stickman.py            state machine + procedural pose + skeleton

  desktop/                 WHAT THEY SENSE (read-only)
    win32.py               ctypes: EnumWindows, DWM bounds, monitors, styles
    scene.py               windows -> walkable platforms and climbable walls
    widgets.py             UI Automation: search bars, web-page elements to stand on
    capture.py             fast GDI screen capture into numpy

  brain/                   HOW THEY THINK AND REMEMBER
    characters.py          loads / validates / saves characters.toml
    gemini.py              REST client: models + backup keys, retries, worker thread
    director.py            summoning, chat / banter / react / summary requests -> bubbles, actions, tasks
    memory.py              chat_log.jsonl + memory.json + images/

  tools/                   WHAT THEY DO FOR YOU (each behind Allow / Deny)
    tasks.py               TaskRunner: proposes, asks, then animates and runs a task
    control.py             windows (close / minimize / restore), cursor glide, typing
    files.py               sandboxed folder: create / write / open text files, backups
    aim.py                 local target detector for click / aim games

  activities/              SHOWS THEY PUT ON
    music.py               music boxes + your song, output device choice
    couch.py               couch time while you watch videos

  ui/                      WINDOWS YOU SEE
    chatwindow.py          chat: history, colours, pictures, ⚙ / ⏻ buttons
    spellcheck.py          Windows spell checker in the chat box
    settings.py            settings: general / characters / memory
    bubble.py              coloured speech bubble that follows a head
    dialogs.py             dark confirm / permission dialogs
    theme.py               shared colours, dark title bars, dark menus
    hotkey.py              RegisterHotKey + Qt native event filter

  defaults/
    characters.default.toml  the shipped characters and settings
characters.toml            yours (created on first run, hot-reloaded, git-ignored)
memory/                    created at runtime: what they remember (git-ignored)
```

## How the stickmen know where windows are

Everything is in **physical pixels of the virtual desktop**. Qt 6 makes the process Per-Monitor-DPI-Aware v2, so Win32 never returns DPI-virtualized coordinates; `app.py` warns if that's not the case.

1. **Enumerate** (`desktop/win32.py`, every 50 ms): `EnumWindows` yields top-level windows in **Z-order, front to back**. A window is skipped when it's:
   - hidden or minimized;
   - *cloaked* (`DWMWA_CLOAKED`): suspended UWP apps and windows on other virtual desktops;
   - a tool window or click-through overlay;
   - untitled, or part of the shell (desktop, taskbar, Start);
   - one of our own windows.
2. **Real bounds**: `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` gives the edges you actually see. `GetWindowRect` would add Windows' ~7 px invisible resize border (×DPI), and stickmen would float beside windows.
3. **Platforms** (`desktop/scene.py`): each window's top edge becomes a segment `y, x0..x1`. Then:
   - The parts covered by windows *earlier* in the Z-order are subtracted, so a hidden edge isn't walkable.
   - What's left is clipped to the monitors' work areas, skipping edges with no headroom (for example, a maximized window's top).
   - Each monitor's work-area bottom (the top of the taskbar) is added as a floor.
4. **Physics** uses these segments:
   - Falling: the stickman lands on the highest segment crossed during that substep.
   - Walking: past `x0`/`x1` he steps off and falls. If the screen ends, he turns around.
5. **Riding windows**: the window he stands on is re-read *every frame* (one cheap DWM call). He moves by the same delta as the window, so dragging a window carries him smoothly. After every poll, he checks that his segment still exists under his feet. If the window closed, was minimized, or got covered, he falls.

## Rendering and DPI

**Performance notes**, measured with 4 stickmen on a 4K screen:
- Each stickman is one `QPainterPath`, so one `drawPath` per frame. The invisible grab halo is only drawn while the cursor is near.
- Pose and IK run once per frame. Physics still runs in ≤4 ms substeps.
- The window under a stickman is re-read once per frame, not per substep.
- Chat messages are painted directly instead of using a stylesheet per QLabel.

Result: frame work dropped from about 3.9 ms to 2.25 ms (median), UI timer jitter at the 95th percentile from 10.6 ms to 2.4 ms, and chat resizing from 68 ms to 32 ms.

- **One overlay per monitor**, each sized to that monitor's work area. A single window spanning mixed-DPI monitors can't be pixel-exact on both. Leaving the taskbar uncovered also stops Windows treating the overlay as a full-screen app.
- **Painting**: each overlay reads its real physical rect back with `GetWindowRect`. It then paints the world with `scale(1/ratio)`, `translate(-origin)`, so the drawing code only ever sees desktop pixels.
- **Click-through**: `WS_EX_LAYERED | WS_EX_TRANSPARENT`, via Qt's `WindowTransparentForInput`, plus `WS_EX_NOACTIVATE`.
- **Dirty rectangles**: only the stickman's old and new bounding box is repainted each frame. A layered window re-uploads whatever is dirty, so this is what keeps CPU usage low on 4K screens.

## Climbing

`scene.py` also turns each window's visible **left and right edges** into `Wall` segments, with the same Z-order occlusion as platforms. A wall is *climbable* when its visible part reaches the window's top corner and that top edge is walkable.

While walking, a stickman that meets a climbable wall (facing it, within 0.3 h, bottom within reach) climbs it 85% of the time:

1. **CLIMB_PREP**: an anticipation crouch with the arms swung back.
2. **CLIMB_JUMP**: a hop onto the wall. It's a higher jump if the window floats above the floor.
3. **CLIMBING**: hand over hand. Planted hands and feet stay fixed on the wall, using the same trick that keeps feet from sliding while walking.
4. **MANTLE**: hands plant on the top edge, the body pushes up, the lead foot steps over, and he walks off along the top.

The wall's window is tracked live, so climbing a window while you drag it works. Closing or minimizing it drops him.

## Widgets inside windows and web pages

[desktop/widgets.py](stickai/desktop/widgets.py) scans for them on a background thread with **Windows UI Automation**, the API screen readers use.
- **Why UI Automation:** Chrome, Edge, Explorer, WinUI and Qt apps draw their own controls, but they all report them through it.
- **Web pages:** for a browser this *is* the page's HTML structure (its accessibility tree). That's the practical way to read a page's tags from outside the browser; no extension is needed.

Every 1.5 s it looks at the six topmost visible windows:

| kind | where | from |
|---|---|---|
| field | all apps | text fields, search and address bars, drop-downs |
| heading | browsers, Electron apps | `<h1>`–`<h3>` |
| image | browsers | `<img>` |
| button | browsers | `<button>` |

- **What's read:** position, kind and the accessible name (e.g. "Search"), never typed text or page text.
- **How positions are stored:** relative to their window, so they stay right while the window is dragged.

Each top edge becomes a walkable platform belonging to its window. Parts hidden behind other windows are cut out.
- **Hop:** stickmen hop onto them in a computed arc that lands exactly on target, even if the window moves mid-jump.
- **Other ways on:** they also land on them when dropped or thrown, walk along them, sit with legs dangling, and fall off the ends.
- **Toggles:** Settings → General → *Walk and sit on search bars inside windows* and *…and on web pages*.
- **Debug colours:** fields yellow, headings pink, pictures purple, buttons orange.

## Choosing their brain (Gemini models)

Settings → General → **Their brain**:

| preset | models, tried in order | cost |
|---|---|---|
| Free & fast (default) | `gemini-3.5-flash-lite` → `gemini-3.8-flash` | free tier |
| Free & smarter | `gemini-3.8-flash` → `gemini-3.5-flash-lite` | free tier |
| **Pro** | `gemini-3.1-pro-preview` → Flash → Flash-Lite | **needs billing** on your API key |

- **Pro has no free tier** (Google's pricing page lists it as *Not available*; as of Nov 2026 it's about $2 / $12 per 1M input / output tokens). On a free key it answers "quota exceeded".
- **Fallback:** StickAI then falls back to Flash, shows a one-time notice, and skips Pro for a while instead of retrying it on every message.
- **Older Pro:** `gemini-2.5-pro` is closed to new users and is being retired on Dec 1, 2026.
- **Pro only for your chats:** Pro is used only for replies to *your* messages. Banter, reactions and memory summaries always use the Flash models.
- **Free daily limits:** Google no longer publishes them per model; see *View your active rate limits* in [AI Studio](https://aistudio.google.com/).

## Seeing your screen (opt-in)

Settings → General → **Seeing your screen**:
- **Off** (default).
- **When you ask:**
  - Ask something like "what's on my screen?". If they decide they need to look, they answer "let me see!", a screenshot of the screen your cursor is on is taken, and they answer again about what they see.
  - You can also press **👁** in the chat to attach a screenshot to your message yourself.
- **Also on their own:** about half of their spontaneous chats include a peek at your screen, and they comment on what you're doing.

Screenshots go to Google Gemini and are **not** saved on disk. Every look is logged in the chat as "👀 … looked at your screen".

## Doing things for you (opt-in, asks every time)

Settings → General → **Let them close / open windows and type for you**. It's off by default. When on:
- **Window list:** they get the list of your open and minimized windows (titles, sent to Google) and the text fields found in them.
- **What they can do:** if you *ask in a chat message*, the character who answers can take one task:
  - close or minimize a window: they leap onto it, your cursor glides to its ✕ / _ button, they stomp, and it closes or minimizes. Closing is the same as pressing ✕, so apps still ask to save.
  - open (restore) a window;
  - type text into a field: they hop onto the field, the cursor glides in, clicks, and the text is typed while they hammer on it.
- **You always decide:** an **Allow / Deny** dialog shows exactly what will happen; Allow is the highlighted default (Enter); Esc denies.
- **Without asking (optional):** turn on the sub-option *…open / close / show / hide windows without asking* and window tasks run straight away. Typing text still asks every time.
- **Limits:** tasks never come from their own chatter or reactions, only from replies to your message. The model is told never to act on text it reads on screen or on web pages. Typed text is one line (no Enter key) and at most 300 characters.
- **You can take over:** move the mouse while the cursor is gliding and the task stops ("you moved the mouse"). Typing also stops if you switch to another window.
- **Logging:** results are logged in the chat (✅ / 🚫 / ⚠️).

## Playing click / aim games

With **Let them close / open windows…** on, ask e.g. *"Red, play the aim game, click the targets until I say stop"*.

1. **Gemini picks the game:** it chooses the game window. If it can see your screen, it also gives the target colour and where one target is; otherwise the colour is guessed locally (the most common vivid colour in the window).
2. **You allow it once.** Then the chat window steps aside, the stickman leaps onto the game window and points, and a **local detector** ([tools/aim.py](stickai/tools/aim.py), numpy, on a background thread with GDI capture, so the UI never waits for a screenshot) does the work:
   - it grabs the window's pixels, finds blobs of the target colour (about 12 ms) and clicks the nearest one with a quick cursor glide;
   - this repeats about 3–5 times a second, with no Gemini request per click.
3. **Safety:**
   - it ignores our own stickmen, bubbles and windows (so a red stickman is never a "target") and the window's title bar;
   - right before every click it checks that the window under that exact point *is* the game window, otherwise it skips the click.
4. **Stopping:** say **stop** (also *хватит / стоп / dosť / prestaň*) in the chat (Alt+` brings it back), **move the mouse**, close the game, or wait 5 minutes. The score is logged: "🎯 Red hit 21 targets".

## Music show

Set a song in Settings → General → **Music for the show** (mp3, wav, ogg, flac, m4a) and its **Tempo (BPM)**. **Play music on** picks the output: Windows' default, a specific device, or **Speakers, even when I wear headphones**. With that one, if the default output is headphones or a headset (by its Windows name, e.g. "Headphones (… Buds)"), the song goes to your speakers instead, and it switches again if you plug or unplug headphones mid-show. Then ask e.g. *"Green, play some music"*.
1. **Setting up:** Green sets out a row of four wooden music boxes, our own drawing, popping in one by one.
2. **Playing:** your song starts, and on every beat Green strikes the next box. It flashes, a coloured ♪ floats up, and he bounces on every fourth beat.
3. **Ending:** it ends with the song or when you say **stop**, and the boxes pack away.

It's code in [activities/music.py](stickai/activities/music.py), using QtMultimedia for playback. Any character can do it, but Green loves it.

## Couch time

When you watch something, everybody on the desktop gathers on a sofa, drawn from behind ([activities/couch.py](stickai/activities/couch.py)).
- **"Watching":** the active window is fullscreen, or its title looks like a video site or player (YouTube, Netflix, Twitch, VLC, mpv, Кинопоиск…), for 4 s in a row.
- **From the chat:** "let's watch something together" starts it too, and it stays until you say **stop**. "Remove the couch" puts it away; it stays away until your next video. When you watch, they just watch with you and never offer to open or play things.
- **The show:** a striped sofa fades in at the bottom-right of that screen. Everyone hops onto the seat, an app-made static platform, and sits. The sofa is drawn in front of them, so only heads and shoulders peek over its back.
- **Ending:** 6 s after you stop watching, they get up and the sofa fades away.
- **Local only:** detection runs on your PC and nothing is sent anywhere.
- **Toggle:** Settings → General → *Sit on a couch together when I watch videos*.

## Their own folder (opt-in)

Settings → General → **Let them create folders and text files in their own folder** (default `Documents\StickAI`; change it with *Browse…*). Ask e.g. *"Blue, write a poem about windows into poems/window.md"*.
- **Tasks:**
  - `create_folder`
  - `write_file`: creates the file, or edits it by rewriting the whole text.
  - `open_path`: opens a file in its default app, or opens the folder.
- **Sandboxed:** only inside that folder. Paths are resolved, and anything that would land outside it (`..`, other drives, absolute paths) is refused.
- **Text files only:** `.txt .md .csv .json .xml .html .css .log .ini .yaml .toml …`. No programs or scripts that could run when double-clicked. Max 100 KB.
- **Every overwrite is backed up first** into `.stickai-backups\` inside the folder.
- **What they see:** the file list and the text of the few most recently changed files are sent with each chat request, so they can edit them.
- **Asking:** each action asks Allow / Deny, unless you tick *…create / edit files there without asking*.

## Mouse: drag and throw

The overlays stay click-through, except while the cursor is over a stickman:
- Each frame, the cursor position (`GetCursorPos`) is hit-tested against his curves.
- On a hit, `WS_EX_TRANSPARENT` is removed from that monitor's overlay only.
- Windows hit-tests layered windows **per pixel**, so empty areas still click through. Each stickman is also drawn with an invisible (alpha 1/255) halo, which makes him easy to grab.

While dragged, the body hangs from the head like a pendulum (Verlet point mass on a rigid rod), and the limbs trail behind the motion. Letting go throws him with the cursor's velocity. He lands on whatever window or floor is below.

## Animation

Procedural and sprite-free. Limbs and spine are drawn as **quadratic Bézier curves** shaped by the IK joints, not straight bones. Pelvis, chest and head follow at different speeds, so the spine bends and the head trails (overlapping action).
- `pos` is the contact point.
- Each state produces a *target pose*: hip offset, torso lean, foot targets and hand targets. The states are FALLING, LANDING, IDLE, WALKING (including running), TURNING, SITTING, JUMP_PREP, the climb states, and DRAGGED.
- The current pose is exponentially damped toward the target. That damping is the automatic blending between states.
- Two-bone IK then places knees and elbows.

Details:
- **Gait**: stride length is derived as `speed / (2 · step rate)`, so the planted foot never slides, even while accelerating.
- **Running**: blends in with speed.
  - The duty factor (the share of the cycle a foot is planted) drops from 0.5 to 0.33, which adds a flight phase where both feet are off the ground.
  - Cadence and stride grow, and the swing foot kicks up behind.
  - The hip bounces, low at mid-stance and high in flight.
  - The torso leans forward, and the arms pump with elbows bent.
- **Jump**: an anticipation crouch with arms swung back, then a stretch on take-off, arms up and knees tucked on the way up, legs reaching down on the way down, and a squash on landing.
- **Sit**:
  - On the floor: knees up, leaning back on the hands.
  - On a window's top edge or a search bar: they sit right where they are, legs hanging down over the front and swinging out of step.
  - In both cases the pose settles slowly and gets up the same way.
- **Spontaneous behaviour**: `energy` and `restlessness` set how often they pause, sit, sprint or hop.
- **Turning**: knee and elbow bend direction follows a smoothed facing value, so the limbs swing through instead of snapping.
- **Landing squash**: an under-damped spring gives one soft bounce.
- **Physics**: runs in ≤4 ms substeps, so behavior is independent of the frame rate.

## Roadmap

- **V1.1**
  - Climbing down.
  - All four stickmen interacting (collisions, grabbing each other).
  - Tumbling/rolling after hard throws.
- **V1.2**: DirectComposition swap chain instead of a layered window, for vsync-locked frames with no per-frame upload.
- **V2.1**: richer actions from the AI: go to a window, follow someone, climb, sit, race. The AI chooses *intents*; the animation layer executes them at 60 FPS.
- **V2.2**: more reactions, e.g. when a window they're standing on closes.
- **V2.3**: more tasks (scroll a page, click a named button), still each behind Allow / Deny.

## Privacy

Everything runs on your PC. Only what's needed for a reply is sent to Google Gemini with your own API key: the chat, character sheets and memory, and, only if you turn them on, window titles, screenshots and files from their folder. Nothing is sent anywhere else.

## License

[MIT](LICENSE)

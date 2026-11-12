"""Browser (Chrome and other Chromium browsers) + Google services.

* YouTube: search, play the first result, home / subscriptions / history / watch later;
* Gmail: inbox, unread, search, and a new message as a DRAFT (the user presses Send);
* Google Maps: find a place, directions from A to B (by car / on foot / transit / bike);
* tabs of the front browser window: list, switch to, close (by part of the title);
* read the open page's text (opt-in setting, sent to Google like the chat) and click a link on it.

Services open as plain web addresses in the default browser: no clicking around their UI, so
they work every time. Tabs, page text and links use Windows UI Automation.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from ctypes import wintypes

from ..i18n import tr
from ..tools import control
from .base import Action, Param, Skill, Unusable, background

BROWSERS = {"Chrome", "Edge", "Brave", "Opera", "Vivaldi", "Yandex"}
MAX_PAGE_CHARS = 12000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
YT_PAGES = {"home": "https://www.youtube.com/", "subscriptions": "https://www.youtube.com/feed/subscriptions",
            "history": "https://www.youtube.com/feed/history", "watch_later": "https://www.youtube.com/playlist?list=WL"}
MAIL = "https://mail.google.com/mail/u/0/"
TRAVEL = ("driving", "walking", "transit", "bicycling")

_P = r"(?:,?\s*(?:пожалуйста|please|prosím|плиз))?[.!?]*"
_YT = r"(?:ютуб[еау]?|youtube|ютюб[еау]?|you ?tube|юту?бчик[еау]?)"
QUICK_YT_PLAY = re.compile(rf"^(?:включи|поставь|запусти|play|pusti)\s+(?:(?:на|в|on|na)\s+{_YT}\s+(.+?)|(.+?)\s+(?:на|в|on|na)\s+{_YT}){_P}$", re.I)
QUICK_YT_FIND = re.compile(rf"^(?:найди|поищи|search|find|nájdi|hľadaj)\s+(?:(?:на|в|on|na)\s+{_YT}\s+(.+?)|(.+?)\s+(?:на|в|on|na)\s+{_YT}){_P}$|"
                           rf"^{_YT}\s+(.+?){_P}$", re.I)
QUICK_YT_OPEN = re.compile(rf"^(?:открой|зайди на|open|otvor)\s+{_YT}{_P}$", re.I)
_MAIL = r"(?:почт[уыае]|gmail|джимейл|жмейл|мейл|e-?mail|mail|poštu|gmail)"
QUICK_MAIL_OPEN = re.compile(rf"^(?:открой|зайди в|open|otvor)\s+(?:мою\s+|my\s+)?{_MAIL}{_P}$", re.I)
QUICK_MAIL_UNREAD = re.compile(rf"^(?:проверь|check|skontroluj)\s+(?:мою\s+|my\s+)?{_MAIL}{_P}$|"
                               rf"^(?:есть ли|есть|any)\s+(?:новые\s+)?(?:письма|new (?:mail|emails))\??$", re.I)
QUICK_MAIL_FIND = re.compile(rf"^(?:найди|поищи|find|search)\s+(?:в\s+{_MAIL}|письм[оа])\s+(.+?){_P}$|"
                             rf"^(?:find|search)\s+(?:my\s+)?(?:mail|emails?|gmail)\s+for\s+(.+?){_P}$", re.I)
_MAP = r"(?:карт[еау]х?|google maps|гугл карт\w*|maps?|mape)"
QUICK_MAP_FIND = re.compile(rf"^(?:покажи|найди|show|find|ukáž|nájdi)\s+(?:(?:на|в|on|v)\s+{_MAP}\s+(.+?)|(.+?)\s+(?:на|в|on|v)\s+{_MAP}){_P}$|"
                            rf"^(?:где находится|where is)\s+(.+?)\s+(?:на|в|on)\s+{_MAP}{_P}$", re.I)
QUICK_ROUTE = re.compile(rf"^(?:(?:построй\s+|проложи\s+|покажи\s+)?маршрут|как (?:добраться|доехать|дойти)|directions|route|"
                         rf"how (?:do i )?get|cesta|trasa)\s+(?:(?:от|из|from|z)\s+(.+?)\s+)?(?:до|в|к|на|to|do|na)\s+(.+?){_P}$", re.I)
MODE_WORDS = [(re.compile(r"\s*,?\s*(?:пешком|on foot|walking|peši)\s*$", re.I), "walking"),
              (re.compile(r"\s*,?\s*(?:на (?:общественном )?транспорте|на автобусе|на метро|на трамвае|на поезде|"
                          r"by (?:bus|transit|metro|train)|mhd|autobusom|vlakom)\s*$", re.I), "transit"),
              (re.compile(r"\s*,?\s*(?:на машине|на автомобиле|на авто|by car|driving|autom)\s*$", re.I), "driving"),
              (re.compile(r"\s*,?\s*(?:на велосипеде|на велике|by bike|bicycling|bicyklom)\s*$", re.I), "bicycling")]
_TAB = r"(?:вкладк[уиа]|таб|tab|kartu)"
QUICK_TAB_CLOSE = re.compile(rf"^(?:закрой|close|zatvor)\s+{_TAB}\s+(?:с\s+|with\s+)?(.+?){_P}$", re.I)
QUICK_TAB_SWITCH = re.compile(rf"^(?:переключись|перейди|вернись|switch|go back|go|prepni)\s+(?:на|во?|to|na)\s+{_TAB}\s+(?:с\s+)?(.+?){_P}$", re.I)
QUICK_TAB_LIST = re.compile(rf"^(?:какие|какие у меня|what|which|aké)\s+{_TAB.replace('[уиа]', 'и')}?\w*\s*(?:открыты|open|otvorené)?\??$", re.I)
QUICK_READ = re.compile(r"^(?:прочитай|прочти|перескажи|о ч[её]м|кратко перескажи|summari[sz]e|read|what'?s on|zhrň|prečítaj)\s+"
                        r"(?:мне\s+)?(?:эт\w+\s+|this\s+|the\s+|túto\s+)?(?:страниц\w*|стать\w*|page|article|stránk\w*|článok)\??[.!]*$", re.I)


# ---------------------------------------------------------------- URLs
def youtube_first_video(query: str) -> tuple[str, str] | None:
    """(video id, title) of the first search result, read from the results page (no API key)."""
    req = urllib.request.Request("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query),
                                 headers={"User-Agent": UA, "Accept-Language": "en;q=0.8", "Cookie": "SOCS=CAI"})
    try:
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"(.{0,3000}?)"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"',
                  html, re.S)
    if not m:
        return None
    try:
        title = json.loads('"' + m.group(3) + '"')
    except ValueError:
        title = m.group(3)
    return m.group(1), title


def maps_url(query: str = "", origin: str = "", dest: str = "", mode: str = "") -> str:
    if dest:
        p = {"api": "1", "destination": dest}
        if origin:
            p["origin"] = origin
        if mode in TRAVEL:
            p["travelmode"] = mode
        return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(p)
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": "1", "query": query})


def gmail_url(action: str, query: str = "", to: str = "", subject: str = "", body: str = "") -> str:
    if action == "compose":
        p = {"view": "cm", "fs": "1"}
        for k, v in (("to", to), ("su", subject), ("body", body)):
            if v:
                p[k] = v
        return "https://mail.google.com/mail/?" + urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
    if action == "search" and query:
        return MAIL + "#search/" + urllib.parse.quote(query, safe="")
    if action == "unread":
        return MAIL + "#search/" + urllib.parse.quote("is:unread in:inbox", safe="")
    return MAIL + "#inbox"


def open_url(url: str) -> None:
    os.startfile(url)  # default browser: a new tab in its open window


# ---------------------------------------------------------------- the browser window (UI Automation)
def browser_windows() -> list[int]:
    """Visible Chromium browser windows, front-most first (z-order), then minimized ones."""
    u = ctypes.windll.user32
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if u.IsWindowVisible(h) and not u.GetWindow(h, 4) and control.app_name(h) in BROWSERS:  # GW_OWNER
            out.append(h)
        return True
    u.EnumWindows(cb, 0)
    return sorted(out, key=lambda h: bool(u.IsIconic(h)))


def _uia():
    from ..tools import uia
    return uia._auto()


# Chrome adds hover-card notes to tab names: " – Memory usage – 38.9 MB", " – закріплено" (pinned)...
TAB_NOTE = re.compile(r"(?:\s+[–-]\s+[^–]{2,40}?\s+[–-]\s+[\d.,]+\s*\S{1,3})$|"
                      r"\s+[–-]\s+(?:закріплено|закреплено|pinned|pripnuté|звук\w*|audio playing|playing audio)$", re.I)


def tab_title(name: str) -> str:
    name = " ".join(name.split())
    for _ in range(3):
        cut = TAB_NOTE.sub("", name)
        if cut == name:
            break
        name = cut
    return name


def tabs(hwnd: int) -> list[dict]:
    """[{name, selected, element}] of the window's tabs."""
    UIA, auto = _uia()
    arr = auto.ElementFromHandle(hwnd).FindAll(
        UIA.TreeScope_Descendants, auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                                UIA.UIA_TabItemControlTypeId))
    out = []
    for i in range(arr.Length):
        e = arr.GetElement(i)
        try:
            sel = bool(e.GetCurrentPattern(UIA.UIA_SelectionItemPatternId)
                       .QueryInterface(UIA.IUIAutomationSelectionItemPattern).CurrentIsSelected)
        except Exception:  # noqa: BLE001
            sel = False
        out.append({"name": tab_title(e.CurrentName or ""), "selected": sel, "element": e})
    return out


def _match_tabs(all_tabs: list[dict], match: str) -> list[dict]:
    want = " ".join(match.lower().split())
    if not want:
        return []
    exact = [t for t in all_tabs if t["name"].lower() == want]
    return exact or [t for t in all_tabs if want in t["name"].lower()]


def select_tab(tab: dict) -> bool:
    UIA, _auto = _uia()
    e = tab["element"]
    try:
        e.GetCurrentPattern(UIA.UIA_SelectionItemPatternId).QueryInterface(
            UIA.IUIAutomationSelectionItemPattern).Select()
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        e.GetCurrentPattern(UIA.UIA_LegacyIAccessiblePatternId).QueryInterface(
            UIA.IUIAutomationLegacyIAccessiblePattern).DoDefaultAction()
        return True
    except Exception:  # noqa: BLE001
        return False


def close_tab(tab: dict) -> bool:
    """Press the tab's own ✕ button (works without focusing the window)."""
    UIA, auto = _uia()
    btns = tab["element"].FindAll(UIA.TreeScope_Descendants, auto.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId))
    for i in range(btns.Length):
        try:
            btns.GetElement(i).GetCurrentPattern(UIA.UIA_InvokePatternId).QueryInterface(
                UIA.IUIAutomationInvokePattern).Invoke()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _page_document(hwnd: int):
    """The visible web page (the selected tab's document) of a browser window."""
    UIA, auto = _uia()
    arr = auto.ElementFromHandle(hwnd).FindAll(UIA.TreeScope_Descendants, auto.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_DocumentControlTypeId))
    best, area = None, -1
    for i in range(arr.Length):
        d = arr.GetElement(i)
        try:
            if d.CurrentIsOffscreen:
                continue
            d.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(UIA.IUIAutomationTextPattern)
            r = d.CurrentBoundingRectangle
            a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        except Exception:  # noqa: BLE001
            continue
        if a > area:
            best, area = d, a
    return best


def page_address(hwnd: int) -> str:
    UIA, auto = _uia()
    arr = auto.ElementFromHandle(hwnd).FindAll(UIA.TreeScope_Descendants, auto.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId))
    for i in range(min(arr.Length, 3)):
        try:
            v = arr.GetElement(i).GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(
                UIA.IUIAutomationValuePattern).CurrentValue
            if v:
                return str(v)
        except Exception:  # noqa: BLE001
            continue
    return ""


def clean_text(text: str) -> str:
    text = text.replace("￼", "").replace("\xa0", " ").replace("\r", "")
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank == 1 and out:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def read_page(hwnd: int) -> tuple[str, str, str]:
    """(title, address, text) of the page shown in the window."""
    UIA, _auto = _uia()
    doc = _page_document(hwnd)
    if doc is None:
        return "", "", ""
    tp = doc.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(UIA.IUIAutomationTextPattern)
    text = clean_text(tp.DocumentRange.GetText(MAX_PAGE_CHARS * 3))
    title = next((t["name"] for t in tabs(hwnd) if t["selected"]), doc.CurrentName or "")
    return title, page_address(hwnd), text


def find_links(hwnd: int, text: str) -> list:
    """Links / buttons on the visible page named like `text`, best first."""
    from ..tools.uia import _norm
    UIA, auto = _uia()
    doc = _page_document(hwnd)
    if doc is None:
        return []
    pc, ct = auto.CreatePropertyCondition, UIA.UIA_ControlTypePropertyId
    cond = auto.CreateOrCondition(pc(ct, UIA.UIA_HyperlinkControlTypeId), pc(ct, UIA.UIA_ButtonControlTypeId))
    arr = doc.FindAll(UIA.TreeScope_Descendants, cond)
    want = _norm(text)
    found = []
    for i in range(min(arr.Length, 2000)):
        e = arr.GetElement(i)
        try:
            got = _norm(e.CurrentName or "")
        except Exception:  # noqa: BLE001
            continue
        if not got:
            continue
        rank = 0 if got == want else 1 if got.startswith(want) else 2 if want in got else None
        if rank is not None:
            found.append((rank, e.CurrentIsOffscreen, len(got), i, e))
    found.sort(key=lambda f: f[:4])
    return [f[4] for f in found]


def invoke(e) -> bool:
    UIA, _auto = _uia()
    for pid, iface, call in ((UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern, "Invoke"),
                             (UIA.UIA_LegacyIAccessiblePatternId, UIA.IUIAutomationLegacyIAccessiblePattern,
                              "DoDefaultAction")):
        try:
            getattr(e.GetCurrentPattern(pid).QueryInterface(iface), call)()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _quick_join(fn, timeout: float = 2.5):
    """Run a short UI Automation query off the Qt thread and wait for it (for Allow texts)."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("value")


# ---------------------------------------------------------------- the skill
class BrowserSkill(Skill):
    name = "browser"
    KINDS = ("youtube", "gmail", "maps", "browser_tabs", "read_page", "click_link")

    def actions(self, settings):
        acts = [
            Action("youtube", "YouTube: search videos, or play = true to start the first found video at once; "
                              "or open a page (home / subscriptions / history / watch_later)",
                   {"query": Param("STRING", "youtube / maps / gmail search: what to look for"),
                    "play": Param("BOOLEAN", "youtube: play the first result (\"включи\", \"play\")"),
                    "page": Param("STRING", "youtube without a query", tuple(YT_PAGES))},
                   ('"включи на ютубе lofi" -> {"kind":"youtube","query":"lofi","play":true}',)),
            Action("gmail", "Gmail in the browser: inbox, unread, search mail, or compose a new message as a DRAFT "
                            "(the user checks it and presses Send themselves)",
                   {"mail": Param("STRING", "gmail: what to do", ("inbox", "unread", "search", "compose")),
                    "to": Param("STRING", "gmail compose: recipient e-mail address(es)"),
                    "subject": Param("STRING", "gmail compose: subject"),
                    "body": Param("STRING", "gmail compose: the message text")},
                   ('"напиши письмо anna@mail.com что я опоздаю" -> {"kind":"gmail","mail":"compose",'
                    '"to":"anna@mail.com","subject":"Опоздаю","body":"Привет! Я немного опоздаю."}',)),
            Action("maps", "Google Maps: find a place (query), or directions to a place (to; from = start, empty = "
                           "where the user is)",
                   {"from": Param("STRING", "maps: start of the route (empty = current location)"),
                    "to": Param("STRING", "maps: route destination"),
                    "travel": Param("STRING", "maps: how to travel", TRAVEL)},
                   ('"как дойти до вокзала пешком" -> {"kind":"maps","to":"вокзал","travel":"walking"}',)),
            Action("browser_tabs", "tabs of the browser window: list them, switch to one, or close one "
                                   "(by part of its title)",
                   {"tabs": Param("STRING", "browser_tabs: what to do", ("list", "switch", "close")),
                    "match": Param("STRING", "browser_tabs: part of the tab's title, e.g. 'YouTube'")}),
            Action("click_link", "click a link or button ON the web page open in the browser, by its text",
                   {"link": Param("STRING", "click_link: the link's text as shown on the page")}),
        ]
        if settings.read_pages:
            acts.append(Action("read_page", "read the text of the web page open in the browser (only when the user "
                                            "asks to read / summarize / answer from it); then answer from it", {}))
        return acts

    def rules(self, settings):
        return ("For Google services prefer youtube / gmail / maps over open_url (they open the right page "
                "directly). Never send mail: compose only makes a draft. For browser_tabs list and read_page add "
                "\"continue\": true and then tell the user what you found.")

    # ------------------------------------------------------------------ quick
    def quick(self, text, app):
        m = QUICK_YT_PLAY.match(text)
        if m:
            return [{"kind": "youtube", "query": (m.group(1) or m.group(2)).strip(" \"'«»"), "play": True}]
        m = QUICK_YT_FIND.match(text)
        if m:
            return [{"kind": "youtube", "query": (m.group(1) or m.group(2) or m.group(3)).strip(" \"'«»")}]
        if QUICK_YT_OPEN.match(text):
            return [{"kind": "youtube", "page": "home"}]
        if QUICK_MAIL_OPEN.match(text):
            return [{"kind": "gmail", "mail": "inbox"}]
        if QUICK_MAIL_UNREAD.match(text):
            return [{"kind": "gmail", "mail": "unread"}]
        m = QUICK_MAIL_FIND.match(text)
        if m:
            return [{"kind": "gmail", "mail": "search", "query": (m.group(1) or m.group(2)).strip(" \"'«»")}]
        m = QUICK_ROUTE.match(text)
        if m:
            dest, mode = m.group(2), ""
            for rx, md in MODE_WORDS:
                if rx.search(dest):
                    dest, mode = rx.sub("", dest), md
                    break
            step = {"kind": "maps", "to": dest.strip(" \"'«»"), "from": (m.group(1) or "").strip(" \"'«»")}
            if mode:
                step["travel"] = mode
            return [step]
        m = QUICK_MAP_FIND.match(text)
        if m:
            return [{"kind": "maps", "query": (m.group(1) or m.group(2) or m.group(3)).strip(" \"'«»")}]
        for rx, op in ((QUICK_TAB_CLOSE, "close"), (QUICK_TAB_SWITCH, "switch")):
            m = rx.match(text)
            wins = browser_windows() if m else []
            if m and wins:
                match = m.group(1).strip(" \"'«»")
                names = _quick_join(lambda: [t["name"] for t in tabs(wins[0])]) or []
                if sum(match.lower() in n.lower() for n in names) == 1:  # exactly one tab: else the AI decides
                    return [{"kind": "browser_tabs", "tabs": op, "match": match}]
                return None
        if QUICK_TAB_LIST.match(text) and browser_windows():
            return [{"kind": "browser_tabs", "tabs": "list"}]
        if QUICK_READ.match(text) and app.roster.settings.read_pages and browser_windows():
            return [{"kind": "read_page"}]
        return None

    # ---------------------------------------------------------------- resolve
    def resolve(self, runner, step, targets):
        kind = step["kind"]
        s = runner.app.roster.settings
        base = {"kind": kind, "skill": self.name}
        if kind == "youtube":
            query = " ".join(str(step.get("query") or step.get("text") or "").split()).strip(" \"'«»")
            page = str(step.get("page") or "")
            if not query:
                url = YT_PAGES.get(page, YT_PAGES["home"])
                return {**base, "url": url, "play": False, "trusted": True, "what": tr("br.yt_open")}
            play = bool(step.get("play"))
            return {**base, "query": query, "play": play, "trusted": True,
                    "url": "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query),
                    "what": tr("br.yt_play" if play else "br.yt_find", query=query)}
        if kind == "gmail":
            action = str(step.get("mail") or ("search" if step.get("query") else "inbox"))
            if action not in ("inbox", "unread", "search", "compose"):
                raise Unusable("mail must be inbox, unread, search or compose")
            query = " ".join(str(step.get("query") or "").split())
            if action == "search" and not query:
                raise Unusable("gmail search needs query")
            to, subject, body = (str(step.get(k) or "").strip() for k in ("to", "subject", "body"))
            if action == "compose" and to and not all(re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", a.strip())
                                                      for a in re.split(r"[,;]", to)):
                raise Unusable(f"'to' must be e-mail address(es), got {to!r}; ask the user for the address")
            what = {"inbox": tr("br.mail_inbox"), "unread": tr("br.mail_unread"),
                    "search": tr("br.mail_search", query=query),
                    "compose": tr("br.mail_compose", to=to or "…", subject=subject or "…")}[action]
            return {**base, "url": gmail_url(action, query, to, subject, body), "trusted": True, "what": what}
        if kind == "maps":
            dest = " ".join(str(step.get("to") or "").split())
            origin = " ".join(str(step.get("from") or "").split())
            query = " ".join(str(step.get("query") or step.get("text") or "").split())
            mode = str(step.get("travel") or "")
            if not dest and not query:
                raise Unusable("maps needs query (a place) or to (a destination)")
            what = tr("br.route", to=dest, frm=origin or tr("br.here")) if dest else tr("br.map_find", query=query)
            return {**base, "url": maps_url(query, origin, dest, mode), "trusted": True, "what": what}
        wins = browser_windows()
        if not wins:
            raise Unusable("no browser window is open (open one with open_url)")
        hwnd = wins[0]
        if kind == "read_page":
            if not s.read_pages:
                raise Unusable("reading pages is turned off in Settings")
            return {**base, "hwnd": hwnd, "trusted": True, "what": tr("br.read")}
        if kind == "click_link":
            link = " ".join(str(step.get("link") or step.get("label") or step.get("text") or "").split())
            if not link:
                raise Unusable("missing link (its text)")
            return {**base, "hwnd": hwnd, "link": link, "trusted": False, "what": tr("br.click", link=link)}
        op = str(step.get("tabs") or "list")
        if op not in ("list", "switch", "close"):
            raise Unusable("tabs must be list, switch or close")
        if op == "list":
            return {**base, "hwnd": hwnd, "op": op, "trusted": True, "what": tr("br.tabs_list")}
        match = " ".join(str(step.get("match") or step.get("text") or "").split()).strip(" \"'«»")
        if not match:
            raise Unusable("missing match (part of the tab's title)")
        names = _quick_join(lambda: [t["name"] for t in tabs(hwnd)]) or []
        hits = [n for n in names if match.lower() in n.lower()]
        if not hits:
            raise Unusable(f"no tab with {match!r} in its title; open tabs: " + "; ".join(n[:50] for n in names[:30]))
        if op == "close" and len(hits) > 1 and not any(n.lower() == match.lower() for n in hits):
            raise Unusable(f"{len(hits)} tabs match {match!r}: " + "; ".join(n[:50] for n in hits[:8]) +
                           " - use a more exact part of the title")
        return {**base, "hwnd": hwnd, "op": op, "match": match, "trusted": op == "switch" or s.trust_windows,
                "what": tr("br.tab_" + op, tab=hits[0][:60])}

    # -------------------------------------------------------------------- run
    def run(self, runner, s, spec):
        from ..tools.tasks import TaskFailed
        kind = spec["kind"]
        if kind == "youtube" and spec.get("play"):
            box = background(lambda: youtube_first_video(spec["query"]))
            yield ("until", lambda: box.get("done", False), 10.0)
            found = box.get("value")
            if found:
                vid, title = found
                open_url(f"https://www.youtube.com/watch?v={vid}")
                spec["done"] = tr("br.yt_playing", title=title)
                spec["report"] = f"playing on YouTube: {title}"
            else:
                open_url(spec["url"])
                spec["report"] = "couldn't pick a video; opened the search results"
            s.jump()
            return
        if "url" in spec:
            s.jump()
            yield ("wait", 0.3)
            try:
                open_url(spec["url"])
            except OSError as e:
                raise TaskFailed(str(e))
            yield ("wait", 1.0)
            return
        hwnd = spec["hwnd"]
        if not control.exists(hwnd):
            raise TaskFailed(tr("br.no_browser"))
        if kind == "read_page":
            box = background(lambda: read_page(hwnd))
            yield ("until", lambda: box.get("done", False), 12.0)
            title, address, text = box.get("value") or ("", "", "")
            if not text:
                raise TaskFailed(tr("br.read_failed"))
            cut = len(text) > MAX_PAGE_CHARS
            spec["report"] = (f"page \"{title}\" ({address}):\n" + text[:MAX_PAGE_CHARS] +
                              ("\n[... the rest of the page is cut]" if cut else ""))
            spec["followup"] = True
            spec["done"] = tr("br.read_done", title=title[:60] or address[:60])
            return
        if kind == "click_link":
            def click():
                links = find_links(hwnd, spec["link"])
                return bool(links) and invoke(links[0])
            box = background(click)
            yield ("until", lambda: box.get("done", False), 10.0)
            if not box.get("value"):
                raise TaskFailed(tr("br.no_link", link=spec["link"]))
            spec["followup"] = True
            spec["report"] = f"clicked {spec['link']!r} on the page"
            s.jump()
            return
        op = spec["op"]

        def work():
            all_tabs = tabs(hwnd)
            if op == "list":
                return [("* " if t["selected"] else "") + t["name"] for t in all_tabs]
            hits = _match_tabs(all_tabs, spec["match"])
            if not hits:
                return None
            if op == "switch":
                return select_tab(hits[0])
            if close_tab(hits[0]):
                return True
            return "select" if select_tab(hits[0]) else False  # ✕ hidden on a narrow tab: Ctrl+W below
        box = background(work)
        yield ("until", lambda: box.get("done", False), 10.0)
        value = box.get("value")
        if op == "list":
            names = value or []
            spec["report"] = f"{len(names)} tab(s) (* = the open one):\n" + "\n".join(n[:80] for n in names[:40])
            spec["followup"] = True
            spec["done"] = tr("br.tabs_listed", n=len(names))
            return
        if value == "select":  # close the now-selected tab with Ctrl+W (needs the window in front)
            control.restore_window(hwnd)
            yield ("wait", 0.4)
            if control.foreground_root() != hwnd:
                raise TaskFailed(tr("task.why_focus"))
            control.press_combo(["ctrl", "w"])
            yield ("wait", 0.3)
            value = True
        if not value:
            raise TaskFailed(tr("br.tab_failed", tab=spec["match"]))
        if op == "switch":
            control.restore_window(hwnd)
        s.jump()

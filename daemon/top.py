#!/usr/bin/env python3
"""hopper-top: the overview (daemon/overview.py) live in the terminal, like top; read-only.

    ssh -t hopper@<build server> hopper/daemon/top.py

SSH is the access control and the transport: whoever may log in as hopper may look.
It refreshes every REFRESH seconds. Keys:

  overview   up/down, PgUp/PgDn, Home/End  select a run (Laminar's)
             Enter    that run's log (from Laminar's web server, as it grows)
             r        refresh now
             q        quit
  log        up/down, PgUp/PgDn/space, Home/g, left/right   scroll
             End/G, f follow the end as it grows (on by default for a running run;
                      f turns it off again, so does scrolling up)
             / n N    search (case-insensitive), next, previous
             q, Esc   back

Nothing here changes anything.
"""

import curses
import locale
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import overview

REFRESH = 2.0
LOG_REFRESH = 1.0

GREEN, RED, YELLOW, CYAN = 1, 2, 3, 4
RESULT_COLORS = {"success": GREEN, "failed": RED, "aborted": RED, "running": YELLOW}


def main():
    locale.setlocale(locale.LC_ALL, "")
    if "utf" not in (locale.nl_langinfo(locale.CODESET) or "").lower():
        try:  # ssh often brings no locale; logs are UTF-8
            locale.setlocale(locale.LC_ALL, "C.UTF-8")
        except locale.Error:
            pass
    try:
        curses.setupterm()
    except curses.error:  # the client's terminal isn't known here (kitty, ...)
        os.environ["TERM"] = "xterm-256color"
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(Top().run)


class Screen:
    def __init__(self, window):
        self.window = window

    def size(self):
        return self.window.getmaxyx()

    def line(self, y, pieces, fill=0):
        """Draw (text, attribute) pieces from the left of row y, clipped to the width."""
        height, width = self.size()
        if not 0 <= y < height:
            return
        x = 0
        if fill:
            self.put(y, 0, " " * width, fill)
        for text, attr in pieces:
            if x >= width:
                break
            self.put(y, x, text[:width - x], attr | fill)
            x += len(text)

    def put(self, y, x, text, attr):
        try:
            self.window.addstr(y, x, text, attr)
        except curses.error:  # the bottom right corner can't be written without scrolling
            pass


def color(number, bold=False):
    return curses.color_pair(number) | (curses.A_BOLD if bold else 0)


def age(text):
    """Seconds since a time as common.timestamp writes them."""
    return time.time() - common.parse_time(text).timestamp() if text else None


def clamp(value, low, high):
    """value, kept within low..high; low if high < low (an empty list)."""
    return max(low, min(value, high))


def size(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return "%.1f%s" % (n, unit) if unit in ("G", "T") else "%d%s" % (n, unit)
        n /= 1024


class Top:
    def run(self, window):
        self.screen = Screen(window)
        curses.use_default_colors()
        for number, foreground in ((GREEN, curses.COLOR_GREEN), (RED, curses.COLOR_RED),
                                   (YELLOW, curses.COLOR_YELLOW), (CYAN, curses.COLOR_CYAN)):
            curses.init_pair(number, foreground, -1)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        window.timeout(250)
        self.selected = 0  # the selected row in the list of runs, 0 is the newest
        self.offset = 0  # the first row shown
        data, loaded = None, 0
        while True:
            if data is None or time.time() - loaded >= REFRESH:
                data, loaded = overview.gather(), time.time()
            runs = data["laminar"]["runs"] if data["laminar"] else []
            self.selected = clamp(self.selected, 0, len(runs) - 1)
            self.draw(data, runs)
            key = window.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key == ord("r"):
                data = None
            elif key in (curses.KEY_ENTER, 10, 13) and runs:
                run = runs[self.selected]
                LogView(self.screen, run["log"], run["run"], follow=run["result"] == "running").run()
                data = None
            else:
                self.move(key, len(runs))

    def move(self, key, count):
        """Move the selection for a navigation key; other keys do nothing."""
        if key in (curses.KEY_UP, ord("k")):
            self.selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected += 1
        elif key == curses.KEY_PPAGE:
            self.selected -= self.page
        elif key == curses.KEY_NPAGE:
            self.selected += self.page
        elif key == curses.KEY_HOME:
            self.selected = 0
        elif key == curses.KEY_END:
            self.selected = count - 1
        self.selected = clamp(self.selected, 0, count - 1)

    def draw(self, data, runs):
        window, screen = self.screen.window, self.screen
        window.erase()
        height, width = screen.size()
        disk = data["disk"]
        build = data["release_build"]
        screen.line(0, [(" hopper-top  %s  %s" % (data["host"], data["time"][11:19]), curses.A_BOLD),
                        ("   code %s" % data["code"] if data["code"] else "", 0),
                        ("   free %s of %s" % (size(disk["free"]), size(disk["total"])), 0),
                        ("   release build running (pid %d)" % build if build else "", 0)], fill=curses.A_REVERSE)
        y = 2
        for setup in data["setups"]:
            y = self.draw_setup(y, setup, data)
        y += 1
        if data["laminar"]:
            y = self.draw_laminar(y, data["laminar"])
            y += 1
        screen.line(y, [("runs", curses.A_BOLD), ("  (Laminar's; Enter: log)", curses.A_DIM)])
        y += 1
        self.page = max(1, height - y - 1)
        # scroll just enough to keep the selected row on screen
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + self.page:
            self.offset = self.selected - self.page + 1
        for i, run in enumerate(runs[self.offset:self.offset + self.page]):
            if self.offset + i == self.selected:
                mark = curses.A_REVERSE
            else:
                mark = 0
            screen.line(y + i, [(" ", 0)] + self.result_pieces(run, run["run"]) + [("  %s" % (run.get("reason") or ""), curses.A_DIM)],
                        fill=mark)
        screen.line(height - 1, [(" up/down select  Enter log  r refresh  q quit", curses.A_DIM)])
        window.refresh()

    def draw_setup(self, y, setup, data):
        """A setup: what it has live"""
        pieces = [("%-11s" % setup["name"], curses.A_BOLD)]
        if setup["error"]:
            pieces.append(("can't read its files: %s" % setup["error"], color(RED, True)))
        else:
            pieces += self.live_pieces(setup["live"])
        self.screen.line(y, pieces)
        return y + 1

    def result_pieces(self, run, what):
        """A Laminar run: what, its result, when and how long"""
        result = run["result"]
        return [("%-30s" % what, 0),
                ("%-9s" % result, color(RESULT_COLORS[result], result != "success") if result in RESULT_COLORS else curses.A_DIM),
                ("%s  %7s" % (overview.short_time(run.get("started")), overview.duration(run.get("seconds"))), 0)]

    def live_pieces(self, live):
        pieces = []
        for channel, versions in sorted(live["channels"].items()):
            ids = sorted(versions, key=lambda v: int(v) if v.isdigit() else 0, reverse=True)
            pieces.append(("%s %s  " % (channel, " ".join("%s%s" % (v, "x%d" % len(versions[v]) if len(versions[v]) > 1 else "")
                                                          for v in ids)), color(CYAN)))
        website = live["website"]
        if website:
            test, production = website.get("test"), website.get("production")
            pieces.append(("website %s  " % ("test = production" if test == production else "test %s, production %s" % (test, production)), color(CYAN)))
        published = live["published"]
        if published:
            pieces.append(("files: %s, %s" % (published.get("about") or published.get("kind"), published["snapshot"][:13]), curses.A_DIM))
        return pieces

    def draw_laminar(self, y, lam):
        """Laminar's jobs (logs: its web UI), and the trigger that starts them"""
        status = lam["status"] or {}
        pieces = [("%-11s" % "laminar", curses.A_BOLD)]
        if lam["laminard"].get("ActiveState") != "active":
            pieces.append(("laminard NOT running: service %s  " % lam["laminard"].get("ActiveState"), color(RED, True)))
        if lam["trigger"]:
            pieces.append(("trigger running", color(GREEN)))
            if status.get("looked") and status.get("pid") == lam["trigger"]:
                late = age(status["looked"])
                pieces.append(("  looked %ds ago" % late, color(RED, True) if late > 3 * status.get("interval", 60) else curses.A_DIM))
            if status.get("problem"):
                pieces.append(("  last look failed: %s" % status["problem"], color(RED, True)))
        else:
            pieces.append(("trigger NOT running: service %s" % lam["service"].get("ActiveState"), color(RED, True)))
        self.screen.line(y, pieces)
        y += 1
        for job in lam["jobs"]:
            self.screen.line(y, self.laminar_job_pieces(job))
            y += 1
        return y

    def laminar_job_pieces(self, job):
        trigger = job["trigger"] or ""
        source = trigger.split()[-1] if trigger and not trigger.startswith("cron ") else trigger[5:] or "by hand"
        pieces = [("  %-23s %-12s " % (job["name"], source), 0 if job["trigger"] else curses.A_DIM)]
        last = job["last_run"]
        if last:
            pieces += self.result_pieces(last, last["run"])
        if job["running"]:
            pieces.append(("  running %s" % " ".join(job["running"]), color(YELLOW, True)))
        elif job["running"] is None:
            pieces.append(("  (laminard can't be asked)", color(RED)))
        if job["problem"]:
            pieces.append(("  %s" % job["problem"], color(RED, True)))
        seen = job["seen"]
        if seen.get("next"):
            pieces.append(("  next %s" % seen["next"][5:16].replace("T", " "), curses.A_DIM))
        return pieces

class LogStream:
    """A Laminar run's log from its web server: the whole of it for a finished run; for a
    running one, what it has so far and then more as it comes, until the run ends. Read
    in a thread; take() hands over what came since the last take()."""

    def __init__(self, url):
        self.url, self.data, self.error, self.done = url, b"", None, False
        self.closed = False
        self.lock = threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            # no timeout: a running build can be quiet for minutes (a refused connection fails at once)
            with urllib.request.urlopen(self.url) as response:
                while not self.closed:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    with self.lock:
                        self.data += chunk
        except OSError as e:
            self.error = "can't read %s: %s" % (self.url, e)
        self.done = True

    def take(self):
        with self.lock:
            data, self.data = self.data, b""
        return data

    def close(self):
        self.closed = True  # the thread stops at the next chunk (or the end)


class LogView:
    """A Laminar run's log, read as it grows."""

    def __init__(self, screen, url, title, follow):
        self.screen, self.url, self.title, self.follow = screen, url, title, follow
        self.stream = None
        self.lines, self.partial = [], ""
        self.top, self.left = 0, 0
        self.search, self.message = None, ""

    def load(self):
        if self.stream is None:
            self.stream = LogStream(self.url)
        data = self.stream.take()
        if self.stream.error:
            self.message = self.stream.error
        text = self.partial + data.decode("utf-8", "replace")
        parts = text.split("\n")
        self.partial = parts.pop()
        # a line rewritten with \r (progress) shows what it said last
        self.lines += [p.rstrip("\r").rsplit("\r", 1)[-1].expandtabs() for p in parts]

    def run(self):
        try:
            self.show()
        finally:
            if self.stream:
                self.stream.close()

    def show(self):
        window = self.screen.window
        loaded = 0
        while True:
            if time.time() - loaded >= LOG_REFRESH:
                self.load()
                loaded = time.time()
            height, width = self.screen.size()
            page = max(1, height - 2)
            last_top = max(0, len(self.lines) - page)
            if self.follow:
                self.top = last_top
            self.top = max(0, min(self.top, last_top))
            self.draw(page)
            key = window.getch()
            if key == -1:
                continue
            if key in (ord("q"), 27):
                return
            moves = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                     curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page}
            if key in moves:
                self.top += moves[key]
            elif key in (curses.KEY_HOME, ord("g")):
                self.top, self.follow = 0, False
            elif key in (curses.KEY_END, ord("G")):
                self.top, self.follow = last_top, True
            elif key == curses.KEY_LEFT:
                self.left = max(0, self.left - 8)
            elif key == curses.KEY_RIGHT:
                self.left += 8
            elif key == ord("f"):
                self.follow = not self.follow
            elif key == ord("/"):
                self.search = self.prompt("/") or self.search
                self.find(1, start=self.top)
            elif key == ord("n"):
                self.find(1, start=self.top + 1)
            elif key == ord("N"):
                self.find(-1, start=self.top - 1)
            if key in moves and moves[key] < 0 or key in (ord("/"), ord("n"), ord("N")):
                self.follow = False

    def find(self, direction, start):
        if not self.search:
            return
        needle = self.search.lower()
        order = range(max(0, start), len(self.lines)) if direction > 0 else range(min(start, len(self.lines) - 1), -1, -1)
        for i in order:
            if needle in self.lines[i].lower():
                self.top, self.message = i, ""
                return
        self.message = "not found: %s" % self.search

    def prompt(self, label):
        window = self.screen.window
        height, width = self.screen.size()
        self.screen.line(height - 1, [(label, 0)], fill=0)
        window.move(height - 1, len(label))
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        window.timeout(-1)
        try:
            text = window.getstr(height - 1, len(label), max(1, width - len(label) - 1)).decode("utf-8", "replace")
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            window.timeout(250)
        return text.strip()

    def draw(self, page):
        window, screen = self.screen.window, self.screen
        window.erase()
        height, width = screen.size()
        screen.line(0, [(" %s  " % self.title, curses.A_BOLD), (self.url, 0),
                        ("  %d lines" % len(self.lines), 0), ("  following" if self.follow else "", curses.A_BOLD)],
                    fill=curses.A_REVERSE)
        needle = (self.search or "").lower()
        for i, text in enumerate(self.lines[self.top:self.top + page]):
            attr = 0
            if text.startswith("==="):
                attr = color(CYAN, True)
            elif "error" in text.lower() or "traceback" in text.lower() or " crashed:" in text:
                attr = color(RED)
            elif needle and needle in text.lower():
                attr = color(YELLOW, True)
            screen.line(1 + i, [(text[self.left:], attr)])
        bottom = min(len(self.lines), self.top + page)
        screen.line(height - 1, [(self.message or " %d-%d of %d   up/down PgUp/PgDn g/G left/right  f follow  / n N search  q back"
                                  % (self.top + 1 if self.lines else 0, bottom, len(self.lines)), curses.A_DIM)])
        window.refresh()


if __name__ == "__main__":
    main()

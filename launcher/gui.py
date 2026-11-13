#!/usr/bin/env python3
"""Run a Launcher build on a virtual display and drive it, to check the GUI without a desktop.

    launcher/gui.py start <launcher> <data-dir> [--size WxH]
    launcher/gui.py stop
    launcher/gui.py shot <file.png> [--crop WxH+X+Y] [--scale PERCENT]
    launcher/gui.py click X Y [BUTTON]
    launcher/gui.py key KEY...
    launcher/gui.py move TEXT X Y
    launcher/gui.py size TEXT WIDTH HEIGHT

start   starts Xvfb on the display (if it isn't running) and the Launcher binary with -d
        data-dir, its output in data-dir/gui.log; nothing of the desktop is touched
stop    stops Launchers started on the display, then its Xvfb
shot    the whole screen as PNG (ImageMagick's import), cropped and scaled if asked
click   at screen coordinates; BUTTON 1 left (default), 3 right, 4 and 5 the wheel
key     X keysym names, pressed in turn: Down, Escape, Return, BackSpace, a; joined with +,
        held together: ctrl+z, ctrl+shift+z (ctrl, shift, alt and super for their left keys)
move    the top-level window whose title contains TEXT, to X Y
size    the same window, to WIDTH x HEIGHT

There is no window manager: windows open at 0,0 without frames, and the newest takes the
keyboard. Move dialogs aside to see the window under them. The display is :77, or
HOPPER_GUI_DISPLAY.

Needs Xvfb, python-xlib and ImageMagick.
"""
import os
import signal
import subprocess
import sys
import time

DISPLAY = os.environ.get("HOPPER_GUI_DISPLAY", ":77")


def fail(message):
    sys.exit(f"gui.py: {message}")


def process_ids(name, argument=None):
    """The processes named name, those with argument among their arguments if given"""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if not args or os.path.basename(args[0].decode(errors="replace")) != name:
            continue
        if argument is None or argument.encode() in args:
            out.append(int(entry))
    return out


def launcher_ids():
    """Launchers on our display: their environment says so"""
    out = []
    for name in ("MultiMC", "MultiMC.bin"):
        for pid in process_ids(name):
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env = f.read().split(b"\0")
            except OSError:
                continue
            if f"DISPLAY={DISPLAY}".encode() in env:
                out.append(pid)
    return out


def start(args):
    if len(args) < 2:
        fail("start needs the launcher binary and a data folder")
    launcher, data = os.path.abspath(args[0]), os.path.abspath(args[1])
    size = "1600x1000"
    if "--size" in args:
        size = args[args.index("--size") + 1]
    if not os.access(launcher, os.X_OK):
        fail(f"{launcher} isn't an executable")
    os.makedirs(data, exist_ok=True)
    if not process_ids("Xvfb", DISPLAY):
        subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{size}x24"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1)
    env = dict(os.environ, DISPLAY=DISPLAY)
    # a session of its own: it outlives this script and the shell that ran it
    with open(os.path.join(data, "gui.log"), "w") as log:
        subprocess.Popen([launcher, "-d", data], env=env, stdout=log, stderr=subprocess.STDOUT,
                         cwd=os.path.dirname(launcher), start_new_session=True)
    print(f"started on {DISPLAY}, output in {os.path.join(data, 'gui.log')}")


def stop(args):
    for pid in launcher_ids():
        os.kill(pid, signal.SIGTERM)
    time.sleep(0.5)
    for pid in process_ids("Xvfb", DISPLAY):
        os.kill(pid, signal.SIGTERM)


def shot(args):
    if not args:
        fail("shot needs a file name")
    command = ["import", "-display", DISPLAY, "-window", "root"]
    if "--crop" in args:
        command += ["-crop", args[args.index("--crop") + 1], "+repage"]
    if "--scale" in args:
        command += ["-resize", args[args.index("--scale") + 1] + "%"]
    subprocess.run(command + [args[0]], check=True)


def x_commands(command, args):
    from Xlib import X, XK, display
    from Xlib.ext import xtest

    d = display.Display(DISPLAY)

    def windows(text):
        for w in d.screen().root.query_tree().children:
            name = w.get_wm_name() or ""
            if text in name and w.get_attributes().map_state:
                yield w

    if command == "click":
        x, y = int(args[0]), int(args[1])
        button = int(args[2]) if len(args) > 2 else 1
        d.screen().root.warp_pointer(x, y)
        d.sync()
        time.sleep(0.1)
        xtest.fake_input(d, X.ButtonPress, button)
        d.sync()
        time.sleep(0.05)
        xtest.fake_input(d, X.ButtonRelease, button)
    elif command == "key":
        modifiers = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "super": "Super_L"}
        for chord in args:
            codes = []
            for key in chord.split("+") if len(chord) > 1 else [chord]:
                keysym = XK.string_to_keysym(modifiers.get(key.lower(), key))
                if not keysym:
                    fail(f"no key named {key}")
                codes.append(d.keysym_to_keycode(keysym))
            # held in order, let go the other way round
            for code in codes:
                xtest.fake_input(d, X.KeyPress, code)
            for code in reversed(codes):
                xtest.fake_input(d, X.KeyRelease, code)
            d.sync()
            time.sleep(0.05)
    elif command == "move":
        for w in windows(args[0]):
            w.configure(x=int(args[1]), y=int(args[2]))
    elif command == "size":
        for w in windows(args[0]):
            w.configure(width=int(args[1]), height=int(args[2]))
    d.sync()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, args = sys.argv[1], sys.argv[2:]
    if command == "start":
        start(args)
    elif command == "stop":
        stop(args)
    elif command == "shot":
        shot(args)
    elif command in ("click", "key", "move", "size"):
        x_commands(command, args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

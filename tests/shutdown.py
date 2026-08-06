"""Does the APPLICATION shut down cleanly? A segfault during teardown would
run after closeEvent, but workspace.save happens there - so a crash on exit
risks a half-written desk file. Repeated, because the failure is intermittent.
"""
import os, sys, subprocess, tempfile, textwrap
HERE = r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix"
CHILD = textwrap.dedent(r'''
    import os, sys, time, logging
    os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
    sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
    logging.basicConfig(level=logging.CRITICAL)
    from PyQt6.QtWidgets import QApplication
    from omnitrix.engine import Instruments, SyntheticFeed
    from omnitrix.ui.main_window import OmnitrixWindow
    from omnitrix.ui import workspace
    # never touch the real desk file
    workspace.save = lambda *a, **k: None
    workspace.restore = lambda *a, **k: None
    app = QApplication([])
    feed = SyntheticFeed(symbols=["QQQ","SPY"], start_price=400.0, tick=0.01,
                         trades_per_sec=60, prefill_minutes=2, seed=5)
    win = OmnitrixWindow(feed, Instruments(default_tick=0.01))
    win.resize(1200, 800); win.show(); win.start_feed()
    t0=time.time()
    while time.time()-t0 < 2.0: app.processEvents(); time.sleep(0.01)
    win.layout_combo.setCurrentText("4 charts")
    for _ in range(20): app.processEvents(); win._tick(); time.sleep(0.005)
    win._open_tape(); win._open_bookmap(); win._open_profile()
    for _ in range(20): app.processEvents(); time.sleep(0.005)
    feed.stop()
    for w in list(app.topLevelWidgets()):
        w.close()
    app.processEvents()
    win.close()
    app.processEvents()
    del win
    app.processEvents()
    print("CLEAN")
''')
with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                 encoding="utf-8") as f:
    f.write(CHILD); path = f.name
bad = 0
for i in range(6):
    r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                       cwd=HERE, timeout=180)
    ok = r.returncode == 0 and "CLEAN" in r.stdout
    if not ok:
        bad += 1
        print(f"  run {i}: exit={r.returncode}  {r.stderr.strip()[-160:]}")
print(f"\nclean shutdowns: {6-bad}/6")
os.unlink(path)

# ---- a failing session must leave an artifact behind ----------------------
# The shipped terminal is a WINDOWED PyInstaller exe: it has no stderr, so
# logging.basicConfig with no filename sent every log line, every survived
# exception and every reason the app stopped drawing to nowhere. A session
# died and left not one byte to say why, and the diagnosis was guesswork.
#
# Run in a child process because the interesting case is the one that cannot
# be caught in-process: a hard crash, where Python raises nothing at all.
CRASH_CHILD = textwrap.dedent(r'''
    import os, sys, logging, ctypes
    os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
    sys.path.insert(0, r"C:\Users\ADMIN\Desktop\Footprint_Omnitrix")
    logging.basicConfig(level=logging.INFO)
    from omnitrix import app as A
    p = A._install_file_log()
    A._install_faulthandler(p)
    A._install_thread_excepthook()
    A._install_excepthook()
    print("LOGPATH:" + p, flush=True)
    logging.getLogger("omnitrix").info("session started")
    for h in logging.getLogger().handlers: h.flush()
    ctypes.string_at(0)                 # access violation, not an exception
''')
fd, cpath = tempfile.mkstemp(suffix=".py")
os.write(fd, CRASH_CHILD.encode()); os.close(fd)
r = subprocess.run([sys.executable, cpath], capture_output=True, text=True,
                   cwd=HERE, timeout=120)
logp = ""
for line in r.stdout.splitlines():
    if line.startswith("LOGPATH:"):
        logp = line[len("LOGPATH:"):].strip()
os.unlink(cpath)

ok_log = bool(logp) and os.path.exists(logp) \
    and "session started" in open(logp, encoding="utf-8").read()
print(f"  {'PASS' if ok_log else 'FAIL'}  the session writes a log FILE, not a "
      f"stderr the exe does not have   {logp or '(none)'}")

fatal = (logp + ".fatal") if logp else ""
ftxt = open(fatal, encoding="utf-8").read() if fatal and os.path.exists(fatal) else ""
ok_fatal = "fatal exception" in ftxt.lower() or "Traceback" in ftxt or "File \"" in ftxt
print(f"  {'PASS' if ok_fatal else 'FAIL'}  a HARD crash still leaves a stack   "
      f"{ftxt.splitlines()[0] if ftxt else '(empty)'}")
print(f"        (the process died with exit {r.returncode}, and Python raised "
      f"nothing - this is the case the excepthook cannot see)")

sys.exit(1 if (bad or not ok_log or not ok_fatal) else 0)

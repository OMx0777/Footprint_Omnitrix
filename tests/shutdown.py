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
sys.exit(1 if bad else 0)

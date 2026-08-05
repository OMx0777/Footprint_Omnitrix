"""Honest 100-client load test: server and clients in SEPARATE processes.

The first attempt ran both in one process, so the CPU figure included the
clients and the GIL held the feeder to 0.69 MB/s against a 1.59 MB/s target -
it measured the harness, not the server. Here the server is its own process and
its CPU is sampled by PID, and the clients live in their own processes so they
cannot steal the feeder's GIL slices.
"""
import os
import subprocess
import sys
import time
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_DIR = r"C:\Users\ADMIN\Desktop\Host_Omnitrix"
PORT = 9873
N_CLIENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
RUN_S = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
N_PROCS = 5                      # client processes; 20 sockets each

SERVER = r'''
import asyncio, os, sys, threading, time
HOST = r"{host}"
sys.path.insert(0, HOST); sys.path.insert(0, os.path.join(HOST, "client_app"))
os.environ["OMNITRIX_BIND"]="127.0.0.1"; os.environ["OMNITRIX_PORT"]="{port}"
os.environ["OMNITRIX_TOKEN"]=""
import logging; logging.disable(logging.INFO)
from omnitrix.engine import wire
from omnitrix.engine.takion_decode import L1, L2
import broadcaster_server as bs

SWEEPS, LEVELS, L1PS = 173, 256, 1200

def feeder():
    seq = {{wire.CH_L1: 0, wire.CH_L2: 0}}
    l2 = b"\x02" + bytes(L2.size); l1 = b"\x01" + bytes(L1.size)
    per = 1.0 / SWEEPS; nxt = time.perf_counter(); sent_n = [0]
    n1 = max(1, L1PS // SWEEPS)
    sent = 0
    while True:
        out = bytearray()
        for payload, count in wire.split_payload([l2] * LEVELS):
            seq[wire.CH_L2] += 1
            out.extend(wire.encode(wire.CH_L2, seq[wire.CH_L2], payload, count))
        for payload, count in wire.split_payload([l1] * n1):
            seq[wire.CH_L1] += 1
            out.extend(wire.encode(wire.CH_L1, seq[wire.CH_L1], payload, count))
        bs.broadcast(bytes(out)); sent += len(out)
        # Writing the counter every batch (173/s) was itself a bottleneck and
        # raced the reader onto an empty file. Once every 200 batches is
        # plenty, written atomically via replace so a read never sees a
        # half-written file.
        if sent_n[0] % 200 == 0:
            tmpf = r"{sent_file}" + ".tmp"
            with open(tmpf, "w") as f: f.write(str(sent))
            os.replace(tmpf, r"{sent_file}")
        sent_n[0] += 1
        nxt += per
        d = nxt - time.perf_counter()
        if d > 0: time.sleep(d)
        else: nxt = time.perf_counter()

async def main():
    bs.main_loop = asyncio.get_running_loop()
    srv = await asyncio.start_server(bs.handle_client, "127.0.0.1", {port})
    threading.Thread(target=feeder, daemon=True).start()
    print("READY", flush=True)
    while True:
        await asyncio.sleep(1)
        with open(r"{drop_file}", "w") as f:
            f.write(str(sum(c.dropped for c in bs.clients)) + " " +
                    str(len(bs.clients)) + " " +
                    str(max((c.queue.qsize() for c in bs.clients), default=0)))
asyncio.run(main())
'''

CLIENT = r'''
import socket, sys, time, os, json
HOST = r"{host}"
sys.path.insert(0, os.path.join(HOST, "client_app"))
from omnitrix.engine import wire
from omnitrix.engine.takion_decode import L1, L2
import threading
N = int(sys.argv[1]); PORT = {port}; RUN = float(sys.argv[2]); OUT = sys.argv[3]
STALL = len(sys.argv) > 4 and sys.argv[4] == "stall"
res = []
def one(i):
    r = {{"bytes":0, "lost":0, "err":None, "last":0.0}}
    res.append(r)
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=15)
        s.settimeout(3.0)
        g = wire.GapDetector(); buf = bytearray()
        end = time.perf_counter() + RUN
        if STALL:
            # Connect, read nothing. This is a laptop that slept, a debugger
            # breakpoint, a wifi drop - on 100 desks it happens every day.
            time.sleep(RUN)
            s.close(); return
        while time.perf_counter() < end:
            try: d = s.recv(1 << 16)
            except socket.timeout: continue
            if not d: break
            r["bytes"] += len(d); r["last"] = time.perf_counter()
            buf.extend(d)
            while True:
                h = wire.decode_header(buf)
                if h is None: break
                ch, cnt, sq = h
                off = wire.HEADER_SIZE; ok = True
                for _ in range(cnt):
                    if off >= len(buf): ok=False; break
                    t = buf[off]
                    n = L1.size if t==1 else L2.size if t==2 else -1
                    if n < 0 or off+1+n > len(buf): ok=False; break
                    off += 1+n
                if not ok: break
                g.observe(ch, sq); del buf[:off]
            r["lost"] = g.lost
        s.close()
    except Exception as e:
        r["err"] = repr(e)
ts = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(N)]
for t in ts: t.start()
for t in ts: t.join(RUN + 20)
json.dump(res, open(OUT, "w"))
'''

tmp = tempfile.mkdtemp()
sent_file = os.path.join(tmp, "sent.txt")
drop_file = os.path.join(tmp, "drop.txt")
open(sent_file, "w").write("0")
open(drop_file, "w").write("0 0 0")
srv_py = os.path.join(tmp, "srv.py")
cli_py = os.path.join(tmp, "cli.py")
open(srv_py, "w").write(SERVER.format(host=HOST_DIR, port=PORT,
                                      sent_file=sent_file, drop_file=drop_file))
open(cli_py, "w").write(CLIENT.format(host=HOST_DIR, port=PORT))

srv = subprocess.Popen([sys.executable, "-u", srv_py],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
line = srv.stdout.readline()
if "READY" not in line:
    print("server failed:", line, srv.stdout.read()[:600]); sys.exit(1)
print(f"server up (pid {srv.pid})")

import psutil
p = psutil.Process(srv.pid)
p.cpu_percent(None)

per = N_CLIENTS // N_PROCS
outs = [os.path.join(tmp, f"c{i}.json") for i in range(N_PROCS)]
STALL = "--stall" in sys.argv
procs = []
for i in range(N_PROCS):
    args = [sys.executable, "-u", cli_py, str(per), str(RUN_S), outs[i]]
    if STALL and i == 0:
        args.append("stall")        # 20 of the 100 desks hang
    procs.append(subprocess.Popen(args))
print(f"{N_CLIENTS} clients across {N_PROCS} processes; running {RUN_S:.0f}s ...")
time.sleep(2.0)
p.cpu_percent(None)
sent0 = int(open(sent_file).read() or 0)
t0 = time.perf_counter()
time.sleep(RUN_S - 3)
cpu = p.cpu_percent(None)
rss = p.memory_info().rss
wall = time.perf_counter() - t0
sent1 = int(open(sent_file).read() or 0)
drops, nconn, maxq = open(drop_file).read().split()
for pr in procs:
    pr.wait(timeout=120)

got = []
for o in outs:
    try:
        got.extend(json.load(open(o)))
    except Exception:
        pass
srv.kill()

fed = sent1 - sent0
rx = sum(r["bytes"] for r in got)
lost = sum(r["lost"] for r in got)
errs = [r["err"] for r in got if r["err"]]
byte_list = sorted(r["bytes"] for r in got if not r["err"])

print(f"\n--- {len(got)} clients / {N_PROCS} procs, {wall:.1f}s ---")
print(f"connected at peak      {nconn}")
print(f"feed produced          {fed/1e6:8.2f} MB  ({fed/wall/1e6:5.2f} MB/s "
      f"- target 1.59)")
print(f"egress                 {rx/1e6:8.2f} MB  ({rx/wall/1e6:5.1f} MB/s = "
      f"{rx*8/wall/1e9:.2f} Gbit/s)")
if byte_list:
    print(f"per client             min {byte_list[0]/1e6:6.2f} MB   "
          f"median {byte_list[len(byte_list)//2]/1e6:6.2f} MB   "
          f"max {byte_list[-1]/1e6:6.2f} MB")
    print(f"worst client received  {byte_list[0]/fed*100:6.2f}% of the feed"
          if fed else "worst client received  (feed counter unavailable)")
print(f"server-side drops      {drops}")
print(f"deepest client queue   {maxq} / 2000")
print(f"client-detected gaps   {lost}")
print(f"socket errors          {len(errs)} {errs[:2]}")
print(f"SERVER CPU             {cpu:5.0f}% of one core "
      f"({cpu/100:.2f} of {psutil.cpu_count()} cores)")
print(f"SERVER RSS             {rss/1e6:5.0f} MB")
if STALL:
    healthy = [b for b in byte_list if b > 1e6]
    print("")
    print(f"STALL TEST: {len(byte_list)-len(healthy)} desks stopped reading")
    if healthy:
        spread = (max(healthy)-min(healthy))/max(healthy)*100
        print(f"  healthy desks still served: {len(healthy)}")
        print(f"  spread between healthy desks: {spread:.2f}% "
              f"({'no head-of-line blocking' if spread < 5 else 'HEAD-OF-LINE BLOCKING'})")
    print(f"  server dropped {drops} batches (bounded queue doing its job)")
    print(f"  server RSS {rss/1e6:.0f} MB (unbounded queues would grow here)")

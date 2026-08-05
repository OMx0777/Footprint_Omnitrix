# TAKION-SERVER deployment

Serving ~100 desks over a 1 Gbps LAN.

## Why it is built this way

Unicasting the feed to 100 clients is **1.27 Gbit/s** (measured: 1.59 MB/s of
feed × 100). That does not fit on gigabit. Multicast sends **one** copy and lets
the switch replicate it, so server egress is 1.59 MB/s whatever the desk count.

UDP loses datagrams, and the L2 book is stateful and merged additively — a lost
datagram makes pulled liquidity read as still resting, forever. So every batch
is sequenced, every gap is detected, and every gap is repaired by replaying the
exact missing bytes over TCP from the recording. Not a snapshot: the same bytes.

## Ports

| Port | Proto | Purpose |
|---|---|---|
| 9997 | UDP multicast | live stream to group `239.7.7.7` |
| 9998 | TCP | replay: backfill on open, gap recovery |
| 9999 | TCP | legacy unicast fan-out (kept for clients not yet moved) |

## Settings

```
set OMNITRIX_MCAST_IF=192.168.x.x     <-- REQUIRED. See below.
set OMNITRIX_MCAST=239.7.7.7
set OMNITRIX_MCAST_PORT=9997
set OMNITRIX_MCAST_TTL=1
set OMNITRIX_DATA=D:\omnitrix\data
set OMNITRIX_RETAIN_DAYS=5
set OMNITRIX_REPLAY_PORT=9998
set OMNITRIX_TOKEN=<shared secret>
```

## OMNITRIX_MCAST_IF is not optional

Two interface mistakes produce a **silently dead feed** — no error, just no
data — and both were measured on Windows, not guessed:

1. **`127.0.0.1` receives nothing.** Zero of twenty datagrams. Multicast is not
   delivered over loopback on Windows. `0.0.0.0` and the real LAN address both
   work.

2. **On a multi-homed host, `0.0.0.0` can pick the wrong adapter.** A machine
   running Hyper-V, WSL or VMware has extra adapters (172.x), and the OS chooses
   by routing metric. If it picks a virtual adapter, the stream goes somewhere
   no desk can see.

The client logs a warning for both, but set the LAN address explicitly on the
server and on the desks. Find it with `ipconfig` — the one on the trading LAN.

## Switch configuration

Multicast without IGMP snooping is flooded to **every** port, which turns a
1.59 MB/s stream into 1.59 MB/s on every link and defeats the point. Confirm
with the network admin:

- **IGMP snooping enabled** on the trading VLAN
- **an IGMP querier** on that VLAN (without one, snooping ages out group
  membership and traffic either floods or stops)
- the group `239.7.7.7` is not filtered

TTL is 1, so the stream cannot leave this LAN segment. That is deliberate:
market data must not cross a router, for licensing as much as bandwidth.

## Storage

~37 GB per session at the measured rate. Five days retained by default is
~185 GB against 396 GB free. **Retention is not optional** — a recorder with no
purge fills the disk, and a full disk on the machine running Takion is a trading
outage, not a storage problem.

Layout, one directory per day:

```
data/2026-08-05/ch2.bin   framed L2 batches, exactly as sent
data/2026-08-05/ch2.idx   sampled (seq, offset) for seeking
data/2026-08-05/ch1.bin   L1
data/2026-08-05/ch1.idx
```

## Checks after starting

```
telnet <server> 9998        then type:  HELLO
  -> OK 2026-08-05 1:1:12345 2:1:98765
```
That confirms the recorder is writing and the replay server can read it.

On a desk, the terminal logs `joined 239.7.7.7:9997` and, if it had to recover,
`repaired ch2 <a>..<b>`. A desk logging `unrepaired` is one that lost data and
could not get it back — that is the line to alert on.

## What to watch

| Symptom | Meaning |
|---|---|
| client logs `multicast gap` frequently | switch is dropping — check IGMP snooping |
| client logs `unrepaired` | replay server unreachable or the range aged out |
| server logs `mcast_dropped` rising | send buffer full; the NIC or CPU is behind |
| disk filling | retention not running; check `OMNITRIX_RETAIN_DAYS` |

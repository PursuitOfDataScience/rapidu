# slurmdisk

Where your bytes and inodes are, what `du` cannot see, and how old the quota
number you are comparing against actually is.

```
$ sd .
 716.5 MiB   /home/researcher
             21,640 files  ·  94 entries  ·  0.17s

  ──────────────────────────────────────────────────────────────────────
        size                       share      files  name
   102.8 MiB  ██████████████████   14.3%      5,062  project-alpha/
    94.3 MiB  ████████████████▌░   13.2%      2,484  notebooks/
    63.3 MiB  ███████████░░░░░░░    8.8%          1  archive-a.db
    50.1 MiB  ████████▊░░░░░░░░░    7.0%      1,686  uchicago-workshops/
   392.8 MiB                       54.8%     14,092  (90 more — use -n 0 for all)
```

Sizes are cumulative, so any row agrees with `du -s` on that path, and plain
files are listed alongside directories. Bar length and bar colour encode the
same thing — this row against the largest — so they always agree; the share
column carries the absolute figure. There is no red in that ramp: the top row is
the hottest colour on *every* listing, so red there would cry wolf on every run.
Red is kept for a near-full quota, an interrupted walk, or a total that is only
a floor.

Colour is off unless stdout is a terminal, honours `NO_COLOR`, and falls back
from 256 colours to 8 and from block glyphs to ASCII when the terminal says so.

```
sd . -c        # count files only, no sizes — 8x faster (see Speed)
sd . -i        # rank by file count instead of bytes
sd . -n 0      # every entry, not just the top 10
sd . -a        # the full report: quota + /proc scan + reconciliation
sd -Q          # just the quota table, and the age of its figures
sd -D          # space held by unlinked-but-open files
sd . --json    # the complete document, for tooling
```

A long walk paints a spinner on stderr with live throughput; **Ctrl+C** prints
the subtrees that finished and says plainly that the rest is unknown, rather
than ranking a half-counted tree.

The count column is headed **files**, not "inodes". An inode is the on-disk
structure a file or directory occupies and it is the resource that runs out, but
your quota calls them files (`files (user) 21,580 / 300,000`) — so the tool uses
the quota's word and the two numbers compare without translation.

No dependencies, no root, no daemon, no config. Stdlib only, down to the
`/usr/bin/python3` that RHEL8 login nodes ship — because the moment you need
this is the moment your home directory is full and `pip` cannot write to it.

---

## What it is not

**It is not more accurate than `du`.** A correct walker agrees with
`du -s --block-size=1` byte-for-byte, and any claim to beat it is a bug. That
agreement is a test in this repo, not an aspiration:

```
tree with 208 files, 5 hard links to one inode, a 1 GiB sparse file, 12-deep nesting
  du -s --block-size=1        655,474,688 B
  slurmdisk, 1/2/4/8/16 thr   655,474,688 B    +0, and identical to each other
```

`du` is right about bytes. What it cannot tell you is *when* it is right.

## What it is

Four things `du` structurally cannot report.

### 1. Your quota number has an age, and it is usually not now

Quota readings are snapshots. On this cluster `quota` is a site wrapper that
prints a cached figure; it has been observed **28 minutes stale, and not
refreshing while a 512 MiB file was written, fsync'ed and deleted.** So every
reading is printed with the age of the *figure*, not the age of the command:

```
QUOTA
  source quota -s   figures are a snapshot 5m 27s old
  ! this number predates anything you did in the last 5m 27s.
```

Where the backend publishes no timestamp, the age prints as `UNKNOWN` — never
as "now".

### 2. A freshly written tree has not settled, and the number is provisional

GPFS does not finalise `st_blocks` immediately. Both of these were measured on
Midway3 scratch, on trees that ended at the same settled size:

| written | first reading | settled | direction |
|---|---|---|---|
| 6,000 × 8 KiB | 240.1 MiB | 375.1 MiB | **56% low** |
| 6,000 × 8 KiB | 1.2 GiB | 375.1 MiB | **69% high** |

Same workload, opposite errors, depending only on *when you looked*. `du` hands
you whichever number is current and says nothing. slurmdisk flags the tree as
provisional, and `--settle-wait 60` re-stats to measure the drift instead of
guessing at it:

```
SETTLING
  6,000 files were written in the last 2m 0s
  ! re-stat 75s later found 80.0 MiB MORE allocated: this tree is still moving.
```

With no wait, it says the figure is provisional and **does not claim it is
settled** — an immediate re-stat cannot observe an effect that takes tens of
seconds, and a null result from a blind instrument is not evidence.

### 3. Space held by files that were deleted while still open

A file unlinked while a process holds it open has no directory entry. It is
invisible to `ls`, to `du`, to `ncdu`, and to slurmdisk's own walk. The blocks
are still allocated. Verified on GPFS:

```
du -s                512 B
sd . (walk)          512 B
sd --deleted-only    512.0 MiB in 1 inode(s)
    512.0 MiB  pids=[731542]  /scratch/midway3/.../ckpt.bin
```

This is the difference between "my quota says 40 GB and I can only find 12" and
a pid you can go and kill.

### 4. Where the inodes are, which is not where the bytes are

Inode exhaustion silently blocks job submission, and users never connect the
two. One conda env is ~177,000 inodes against a 300,000 soft home quota — two
envs exhaust it. A 64-rank job writes 64 near-empty `rng_state_*.pth` files per
checkpoint, pure inode cost. So directories are ranked three ways, and the third
is the one that tells you what to pack:

```
sd .      by allocated bytes -- the classic du question
sd . -i   by file count -- what an inode quota limits
```

Packing a directory reclaims the inodes it holds, so the absolute count is the
ranking; density only tells you how cheap the tar will be. (An earlier version
ranked by files/GiB directly. A ratio is won by the smallest denominator, so it
nominated a 260 KiB `.git` directory as the top candidate ahead of one holding
ten times the inodes.)

---

## The reconciliation, and when it refuses to run

The three-way check is `walk + deleted-but-open ≈ quota`. The third term is a
snapshot of unknown age, so a naive version of this reports a phantom gap and
blames an innocent file descriptor. It is therefore allowed to *refuse*:

```
RECONCILIATION
  bytes: reconciles (difference is within 13.7 MiB)
      walked                         691.5 MiB
      = accounted for                691.5 MiB
      quota says                     683.8 MiB
      difference                      -7.7 MiB
      caveats:
        - the quota figure is a snapshot taken 327s ago and may predate recent
          writes or deletions
```

Any of these turns a difference into `INCONCLUSIVE` rather than a finding:

- the quota snapshot is stale, or published no timestamp at all
- the tree is still settling
- the walk hit unreadable directories, so its total is a floor
- the walk crossed filesystems but the quota governs one

And when a gap does survive all of that, it is reported with **candidate
explanations, none of them asserted** — deleted fds on other nodes, other users'
processes an unprivileged scan cannot inspect, snapshots, replication factor.
Walking a subtree of a larger quota'd tree is reported as a share, not a
discrepancy.

## Honest limits

- **Cross-user attribution needs root.** You can read only your own
  `/proc/*/fd`, so in a shared group quota you can prove a gap exists but cannot
  name the labmate holding it. The count of uninspectable processes is printed.
  Files *on disk* are attributed by owner, which the walk can do.
- **The deleted-fd scan is node-local.** A job holding a deleted file on a
  compute node is invisible from the login node.
- **Quota mount mapping is inferred where the backend does not publish it.**
  Ambiguous inferences are dropped, not guessed: if two filesets both resolve to
  `$HOME`, neither is used unless the hostname breaks the tie.
- **Off-site, fields go absent, not zero.** No `quota` command, no `/proc`, an
  unparseable format — each prints `n/a` with a reason, and nothing downstream
  breaks.

## Speed

**Bytes do not predict how long a walk takes; files do.** Measured on this
cluster, same filesystem, same day:

| tree | size | files | `du` | `slurmdisk` | |
|---|---|---|---|---|---|
| `.cache/huggingface` | 161.7 GiB | 3,286 | 0.44s | 0.03s | 14x |
| `.cache` | 350.0 GiB | 781,772 | 162.25s | 25.28s | **6.4x** |

Twice the bytes, **370x the wall time.** A 1 TB directory of a few large
checkpoint shards walks in milliseconds; a 1 GB directory of two million tiny
files takes minutes.

### Where the time actually goes

An earlier version of this section claimed concurrency was the lever. That was
measured against `du` and does not describe this walker. Measured against
itself, on 782k GPFS files:

```
scandir + stat (what a sizing walk does)   27.09s     28,900 files/s
scandir alone, no stat                      2.99s    261,800 files/s
this package's own bookkeeping                          +0.5%
```

**`stat` is 90% of the wall time and our Python costs half a percent**, so
tuning the inner loop is pointless. Two obvious levers were tried and both are
dead ends:

```
stat via dir_fd instead of a full path     26.89s vs 26.65s   no difference
4 or 8 processes instead of threads         41.9s vs  27.0s   35% WORSE
```

Processes being *worse* places the limit in the GPFS client per node, not per
process, and thread scaling agrees: 8 → 16 buys 5%, and 24/32/48/64 all get
slower. **~29,000 stats/s is this filesystem's metadata ceiling** and no
client-side change moves it. The pool is capped at 16 and defaults to 8, which
is also the polite setting — this walk is metadata load on a shared filesystem,
the exact sin the tool exists to diagnose. `--max-dirs-per-sec` throttles it
further on a busy day.

### The one real speedup: don't call stat

Counting files needs no `stat` at all — `d_type` from `getdents` already
separates directories from everything else. `-c` uses that:

```
1.7M-file tree     sizing walk 55s      sd . -c  6.9s      8x
782k-file tree     sizing walk 27.3s    sd . -c  3.4s      8x
```

Exact on counts, no sizes, and hard links counted once per name — all three
stated in the output rather than implied. For inode-quota pressure, which is
what this column of the tool is for, that is the whole answer.

> **Prior-art caveat, stated because it is still owed.** The `du` comparison is
> against a single-threaded 1971 program — *not* against the state of the art.
> Parallel walkers already exist (`dust`, `gdu`, `diskus`, `duc`). Given the
> ceiling measured above, any of them will land within a few percent of this
> one on the same filesystem, because none of them can make GPFS answer `stat`
> faster. A full PyPI/GitHub sweep has **not** been run. **Speed is table stakes
> here; the quota-age, settling and deleted-fd reporting are the product.**

## Install

```bash
pip install slurmdisk
```

Or just run it — it is stdlib-only:

```bash
git clone https://github.com/PursuitOfDataScience/slurmdisk
PYTHONPATH=slurmdisk/src python3 -m slurmdisk .
```

Installed, the command is `slurmdisk`, with `sd` as a short alias — the same
pairing as `slurmwatch`/`sw`.

## Options

```
sd [PATH ...] [options]

The only positional argument is a path. Modes are flags, not subcommands:
`quota`, `walk` and `deleted` are all ordinary directory names, so `sd deleted`
must mean "measure ./deleted" and nothing else.

-a, --full               quota + /proc scan + reconciliation
-c, --count              count files only, no stat -- 8x faster
-i, --inodes             rank by file count, not bytes
-n 0                     show every entry
-Q, --quota-only         quota table only; walk nothing
-D, --deleted-only       unlinked-but-open space only
-t, --threads N          walk concurrency, clamped to 16 (default 8)
-d, --depth N            directory depth to aggregate for reporting (default 2)
-n, --top N              entries per ranking (default 10)
    --settle-wait S      wait S seconds, then re-stat to measure drift
    --one-file-system    do not cross filesystem boundaries
    --max-dirs-per-sec N token-bucket rate limit on directory opens
    --no-quota           skip the quota backend
    --no-deleted         skip the /proc scan
    --json               machine-readable output
```

Exit codes: `0` clean, `1` something needs a human (incomplete walk, drifting
tree, unexplained gap), `2` error.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The suite pins the invariants that decide whether this is better or worse than
`du`: byte-exact agreement, identical results across thread counts, hard-link
dedup, sparse files not over-counted, the thread cap, and — most of the
reconciliation tests — the refusals.

## License

MIT

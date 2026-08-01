# slurmdisk

Where your bytes and inodes are, what `du` cannot see, and how old the quota
number you are comparing against actually is.

```
$ sd .
/home/researcher   700.6 MiB   21,526 inodes   0.18s

    70.6 MiB        283 inodes  notebooks
    46.3 MiB      2,581 inodes  project-alpha/midtraining
    43.7 MiB      2,328 inodes  slurmwatch/.git
    ...
```

That is the default, and it is all the default does: how big is this tree, and
what is big inside it. Everything below is behind a flag, because none of it is
needed to answer that question and all of it costs time.

```
sd . -i        # rank by inode count instead of bytes
sd . -a        # the full report: quota + /proc scan + reconciliation
sd -Q          # just the quota table, and the age of its figures
sd -D          # space held by unlinked-but-open files
sd . --json    # the complete document, for tooling
```

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
LARGEST SUBTREES   by allocated bytes -- the classic du question
MOST INODES        by inode count, with files/GiB as a column
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

`du` is single-threaded and spends ~95% of its wall time blocked on filesystem
latency, so concurrency is the whole lever — not language. Measured on 787k GPFS
inodes / 370 GB from a 6-core login node:

```
du -s --block-size=1     164.18s          <- first pass
scandir threads=1        151.75s   1.08x
scandir threads=4         45.32s   3.62x
scandir threads=8         27.71s   5.93x   <- default
scandir threads=16        26.67s   6.16x   <- knee; the cap
du -s --block-size=1     168.75s          <- AFTER all four walks
```

**The last line is the control.** A warmed metadata cache would have made that
final `du` fast. It did not — 168.75s against 164.18s, after four complete walks
of the same inodes — so the speedup is attributable to concurrency alone.

Past the knee the walk gets *slower*, so the pool is hard-capped at 16 and
defaults to 8. The fast setting and the polite setting turn out to be the same
setting, which matters because this walk is metadata load on a shared
filesystem: the exact sin the tool exists to diagnose. `--max-dirs-per-sec`
throttles it further on a busy day.

> **Prior-art caveat, stated because it is still owed.** The speed comparison
> above is against `du`, a single-threaded 1971 program — *not* against the
> state of the art. Parallel disk-usage walkers already exist (`dust`, `gdu`,
> `diskus`, `duc`) and a Rust one will very likely match or beat threaded
> Python. A full PyPI/GitHub sweep has **not** been run, so assume the parallel
> walk is a solved problem until shown otherwise. **Speed is table stakes here;
> the quota-age, settling and deleted-fd reporting are the product.**

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
-i, --inodes             rank directories by inode count, not bytes
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

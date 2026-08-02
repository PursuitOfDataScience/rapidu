<h1 align="center">rapi<code>DU</code></h1>

<p align="center">
  <strong>A rapid <code>du</code> that tells you <em>why</em> — 5.2x faster, to the same byte.</strong><br>
  <sub>Where your bytes and inodes went, what <code>du</code> cannot see, and how old the quota number you are arguing with really is.</sub>
</p>

<p align="center">
  <a href="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.6%2B-blue.svg" alt="Python 3.6+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-261230.svg" alt="Ruff"></a>
</p>

<p align="center">
  <img src="assets/demo.gif" width="900" alt="rapiDU walking a project tree and explaining that it occupies 266.8 MiB to hold 75.5 MiB of data, ranking the same tree by file count, printing a quota table with the age of its snapshot, finding 512 MiB held by a deleted-but-open file descriptor, and catching a freshly written GPFS tree that loses 224 MiB while it settles.">
</p>

```bash
rdu ~            # how big is this tree, and what is big inside it
rdu ~ -i         # rank by file count -- what an inode quota actually limits
rdu ~ -c         # count files only, no stat: 8x faster
rdu -Q           # the quota table, and the age of its figures
rdu -D           # space held by files that were deleted while still open
rdu ~ -a         # the full audit: quota + /proc scan + reconciliation
```

`rdu --help` for the rest. Stdlib only, down to the `/usr/bin/python3` that RHEL8
login nodes ship — because the moment you need this is the moment your home
directory is full and `pip` cannot write to it.

```bash
git clone https://github.com/PursuitOfDataScience/rapidu    # nothing to install
PYTHONPATH=rapidu/src python3 -m rapidu .

pip install git+https://github.com/PursuitOfDataScience/rapidu   # or packaged
```

---

## 5.2x faster than `du`, and identical to the byte

Cold, on GPFS, on trees big enough that you actually wait:

| tree | files | `du` | `rdu` | |
|---|---|---|---|---|
| a package cache | 792,225 | 168.08s | 25.40s | **6.6x** |
| a whole project directory | 1,686,589 | 298.46s | 57.43s | **5.2x** |

And it returns the *same number*. On a test tree with hard links, a sparse file
and 12-deep nesting, `du -s --block-size=1` and rapiDU at 1/2/4/8/16 threads all
return **655,474,688 B** — a test in this repo, not an aspiration. A faster
answer is only worth having if it is the same answer.

`du` is right about bytes. What it cannot tell you is **when** it is right.

## Five things `du` structurally cannot report

### 1. A freshly written tree has not settled

GPFS does not finalise `st_blocks` for tens of seconds, and it moves in *both*
directions. Measured on Midway3 scratch, 6,000 × 8 KiB files: `du -s` says
**81 MB** immediately after the write and **376 MB** once the tree settles.
Same tree, same command, **4.6x apart** — and nothing in `du`'s output says which
of the two you are holding.

```
  SETTLING
      6,000 files were written in the last 2m 0s
    ! a re-stat 60s later found 255.2 MiB LESS allocated: this tree is still moving.
```

Without `--settle-wait` it calls the figure **provisional** and does *not* claim
it is settled: an immediate re-stat cannot observe an effect that takes tens of
seconds, and a null result from a blind instrument is not evidence.

### 2. Your quota number has an age, and it is usually not now

Quota readings are snapshots. Here `quota` is a site wrapper printing a cached
figure; it has been observed **28 minutes stale, and not refreshing while a
512 MiB file was written, fsync'ed and deleted.** So the age of the *figure* is
printed beside it — and where the backend publishes no timestamp, the age prints
`UNKNOWN`, never "now".

```
QUOTA  snapshot 11m 16s old -- predates anything you just did
  labgroup    blocks   72.1 TiB / 202.3 TiB   ██▌░░░░░░░   35.6%  /project
  otherlab    blocks   11.0 TiB /  11.0 TiB   █████████▉   99.8%  /project
```

### 3. Space held by files that were deleted while still open

A file unlinked while a process holds it open has no directory entry. It is
invisible to `ls`, to `du`, to `ncdu`, and to rapidu's own walk. The blocks
are still allocated, and the quota still charges you for them:

```
$ rdu -D
UNLINKED BUT STILL OPEN
  512.0 MiB held by open file descriptors in 1 inodes
  (invisible to du, to ls, and to this tool's own walk)

      512.0 MiB  pid 802715 python
                 /scratch/midway3/$USER/ckpt-step-4000.bin
```

This is the difference between "my quota says 40 GB and I can only find 12" and
a pid you can go and kill.

### 4. Where the inodes are, which is not where the bytes are

Inode exhaustion silently blocks job submission and nobody connects the two. One
conda env is ~177,000 inodes against a 300,000 soft home quota — two envs
exhaust it. A 64-rank job writes 64 near-empty `rng_state_*.pth` files per
checkpoint, pure inode cost. `rdu -i` ranks by the count instead of the bytes,
and by the **absolute** count: an earlier version ranked by files/GiB, and a
ratio is won by the smallest denominator, so it nominated a 260 KiB `.git`
directory ahead of one holding ten times the inodes.

### 5. Why the number is that big, when the files add up to far less

Every file smaller than the filesystem's allocation unit pays for the whole
unit — 64 KiB on Midway3 scratch, 16 KiB on `/home` and `/project`. So 3,000
files of 8 KiB hold 23.6 MiB and occupy 187.6 MiB, and `du` says `188M`:

```
! 187.6 MiB allocated for 23.6 MiB of data — 8.0x. Your quota is charged the first number.
    3,000 files average 8.0 KiB against a 64.0 KiB allocation unit, so they
    occupy 164.1 MiB of padding. Packing them (tar, squashfs) returns it.
```

**The unit is measured, not assumed** — read off the allocations the files
actually landed on. `statvfs` cannot supply it: it reports the 4 MiB GPFS
*block* where files allocate in 16 KiB subblocks, a 256x error in the direction
that makes small files look free.

It runs the other way too, and that is *not* an error: below ~3.5 KiB GPFS keeps
data in the inode, so the same filesystem stores 8.7 MiB in 1.6 MiB. There the
report says bytes are nearly free and points at the inode count, which is the
quota that will actually stop you.

---

## Reading the table

```
      size  of tree              share      files  path
 661.5 GiB  █████▋░░░░░░░░░░░░   31.9%        350  checkpoints/
 593.5 GiB  █████▏░░░░░░░░░░░░   28.6%         36  model-weights/
 343.8 GiB  ██▊░░░░░░░░░░░░░░░   16.6%        968  datasets/
 470.9 GiB  ▒▒▒▒▒▒▒▒░░░░░░░░░░   22.9%      4,117  (84 more — use -n 0 for all)
```

- **The bar is share of the whole tree**, so it always agrees with the `share`
  column beside it. Scaled to the largest row instead — as most disk tools do —
  the top bar is full on every listing ever printed, which tells you nothing and
  contradicts the "31.9%" next to it. The track is that whole tree, drawn on
  every row so a short bar has an edge to be measured against.
- **The hatched row is the remainder** — everything not listed, at its true
  length. A fifth of this tree is not something to hide, but it is many
  directories and must not be drawn as though it were one.
- **Colour is rank**, cool to warm, assigned across the listing so two rows share
  a tone only when they are genuinely the same size. The column you sorted by is
  the one wearing it: under `-i`, `files` takes the tone and `size` steps back.
- **`path`, not `directory`**: plain files are ranked here too, and sizes are
  cumulative subtree totals, so any row agrees with `du -s` on that path.

## The reconciliation, and when it refuses to run

The three-way check is `walk + deleted-but-open ≈ quota`. The third term is a
snapshot of unknown age, so a naive version reports a phantom gap and blames an
innocent file descriptor. It is therefore allowed to **refuse** — any of these
turns a difference into `INCONCLUSIVE` rather than a finding:

- the quota snapshot is stale, or published no timestamp at all
- the tree is still settling
- the walk hit unreadable directories, so its total is a floor
- the walk crossed filesystems but the quota governs one
- the walk was `-c`, which never called `stat` and so has no bytes to compare

A gap that survives all of that is reported with **candidate explanations, none
of them asserted** — deleted fds on other nodes, other users' processes an
unprivileged scan cannot inspect, snapshots, replication factor. Walking a
subtree of a larger quota'd tree is reported as a share, not a discrepancy.

## Honest limits

- **Cross-user attribution needs root.** You can read only your own `/proc/*/fd`,
  so in a shared group quota you can prove a gap exists but cannot name the
  labmate holding it. The count of uninspectable processes is printed.
- **The deleted-fd scan is node-local.** A job holding a deleted file on a
  compute node is invisible from the login node.
- **Quota mount mapping is inferred** where the backend does not publish it, and
  ambiguous inferences are dropped rather than guessed.
- **Off-site, fields go absent, not zero** — each prints `n/a` with a reason.

## Speed, and its ceiling

**Bytes do not predict how long a walk takes; files do.** `stat` is ~90% of a
walk, so `rdu -c` — counts only, no `stat` — is **8x** faster again when the
question is "how many files", not "how many bytes".

**~29,000 stats/s is the filesystem's ceiling, and nothing gets past it.** More
threads do not: 8 and 16 tie, 32 is worse, 128 is much worse. Nor do more
processes — two of them move the same total as one, so the cap is the node. The
pool caps at 16 and defaults to 8, which is also the polite setting: this walk is
metadata load on a shared filesystem, the exact sin the tool exists to diagnose.

> **Prior art, stated because it is owed.** The `du` comparison is against a
> single-threaded 1971 program — *not* the state of the art. Parallel walkers
> exist (`dust`, `gdu`, `diskus`, `duc`) and, given the ceiling above, any of
> them will land within a few percent on the same filesystem. **Speed is the
> table stakes; the quota-age, settling and deleted-fd reporting are the product.**

## Behaviour worth knowing

The only positional argument is a path. Modes are flags, not subcommands:
`quota`, `walk` and `deleted` are all ordinary directory names, so `rdu deleted`
must mean "measure ./deleted" and nothing else.

Colour is off unless stdout is a terminal, honours `NO_COLOR`, and degrades from
256 colours to 8 and from block glyphs to ASCII when the terminal says so. A long
walk paints a spinner on **stderr**, so a redirected report stays clean.
**Ctrl+C** prints the subtrees that finished and says plainly that the rest is
unknown, rather than ranking a half-counted tree. Exit codes: `0` clean, `1`
something needs a human (incomplete walk, drifting tree, unexplained gap), `2`
error.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The suite pins the invariants that decide whether this is better or worse than
`du`: byte-exact agreement, identical results across thread counts, hard-link
dedup, sparse files not over-counted, the thread cap, that the package still
parses under Python 3.6 — and, most of the reconciliation tests, the refusals.
The README GIF is generated from real CLI output by
[`assets/render_demo.py`](assets/render_demo.py); nothing in it is hand-typed.

---

Before the run, [`slurmate`](https://github.com/PursuitOfDataScience/slurmate)
builds the request. During it,
[`slurmwatch`](https://github.com/PursuitOfDataScience/slurmwatch) watches.
After it, [`slurmpast`](https://github.com/PursuitOfDataScience/slurmpast) says
what happened. `rapidu` is for the storage the whole cycle runs on.

MIT

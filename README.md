<h1 align="center">rapi<code>DU</code></h1>

<p align="center">
  <strong>A 5x faster <code>du</code> that tells you why your quota is full.</strong>
</p>

<p align="center">
  <a href="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.6%2B-blue.svg" alt="Python 3.6+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
</p>

<p align="center">
  <img src="assets/demo.gif" width="900" alt="rapiDU walking a project tree and explaining that it occupies 266.8 MiB to hold 75.5 MiB of data, ranking the same tree by file count, printing a quota table with the age of its snapshot, finding 512 MiB held by a deleted-but-open file descriptor, and catching a freshly written GPFS tree that loses 224 MiB while it settles.">
</p>

```bash
pip install rapidu

rdu ~            # how big is this tree, and what is big inside it
rdu ~ -i         # rank by file count -- what an inode quota actually limits
rdu ~ -c         # count files only, no stat: 8x faster again
rdu -Q           # the quota table, and the age of its figures
rdu -D           # space held by files deleted while still open
rdu ~ -a         # the full audit: quota + /proc scan + reconciliation
```

Stdlib only, down to the `/usr/bin/python3` that RHEL8 login nodes ship — because
the moment you need this is the moment your home directory is full and `pip`
cannot write to it. `git clone` plus `PYTHONPATH=…/src python3 -m rapidu .` works
with nothing installed at all.

## It is faster, and it is the same number

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-dark.svg">
    <img src="assets/benchmark-light.svg" width="720" alt="Cold GPFS walk: du takes 168.1s against rapiDU's 25.4s on a 792,225-file package cache (6.6x), and 298.5s against 57.4s on a 1,686,589-file project directory (5.2x).">
  </picture>
</p>

A faster answer is only worth having if it is the same answer. On a tree with
hard links, a sparse file and 12-deep nesting, `du -s --block-size=1` and rapiDU
at 1/2/4/8/16 threads all return **655,474,688 B** — a test in this repo, not an
aspiration.

## What rapiDU adds to `du`

**1. It says when the number is still moving.** GPFS does not finalise
`st_blocks` for tens of seconds. The same tree reads 81 MB right after a write
and 376 MB once settled — and `du` hands you whichever you happened to ask for.

**2. It prints the age of your quota figure.** Quota readings are snapshots. This
one has been observed 28 minutes stale while a 512 MiB file was written, fsync'ed
and deleted without the number moving. Where the backend publishes no timestamp
the age prints `UNKNOWN`, never "now".

**3. It finds space with no directory entry.** A file unlinked while a process
holds it open is invisible to `ls`, to `du`, to `ncdu` and to rapiDU's own walk —
but the quota still charges you for it. `rdu -D` names the pid.

**4. It ranks by inodes, not just bytes.** One conda env is ~177,000 inodes
against a 300,000 home quota; two exhaust it. Inode exhaustion blocks job
submission and nobody connects the two.

**5. It explains why the number is so big.** Every file smaller than the
allocation unit pays for the whole unit — 64 KiB on scratch, 16 KiB on `/home`.
So 3,000 files of 8 KiB hold 23.6 MiB and occupy 187.6 MiB:

```
! 187.6 MiB allocated for 23.6 MiB of data — 8.0x. Your quota is charged the first number.
    3,000 files average 8.0 KiB against a 64.0 KiB allocation unit, so they
    occupy 164.1 MiB of padding. Packing them (tar, squashfs) returns it.
```

The unit is **measured**, not assumed: `statvfs` reports the 4 MiB GPFS *block*
where files actually allocate in 16 KiB subblocks. It runs the other way too —
below ~3.5 KiB the data lives in the inode, so the same filesystem stores 8.7 MiB
in 1.6 MiB, and there the report points at inodes instead.

## Reading the table

```
      size  of tree              share      files  path
 661.5 GiB  █████▋░░░░░░░░░░░░   31.9%        350  checkpoints/
 343.8 GiB  ██▊░░░░░░░░░░░░░░░   16.6%        968  datasets/
 470.9 GiB  ▒▒▒▒▒▒▒▒░░░░░░░░░░   22.9%      4,117  (84 more — use -n 0 for all)
```

The bar is share of the **whole tree**, so it always agrees with the number
beside it, and the track is that tree so short bars have a common edge. The
hatched row is everything not listed. Colour is rank, and the column you sorted
by is the one wearing it — under `-i`, `files` takes the tone and `size` steps
back. Sizes are cumulative, so any row agrees with `du -s` on that path.

## The ceiling, stated because it is owed

**~29,000 stats/s is the filesystem's ceiling, not the tool's.** More threads do
not help — 8 and 16 tie, 32 is worse, 128 much worse — and neither do more
processes, since two move the same total as one. The pool caps at 16 and defaults
to 8, which is also the polite setting: this walk is metadata load on a shared
filesystem, the exact sin the tool exists to diagnose.

So the comparison above is against a single-threaded 1971 program, *not* the
state of the art. `dust`, `gdu`, `diskus` and `duc` exist and will land within a
few percent of rapiDU on the same filesystem. **Speed is table stakes; items 1–5
are the product.**

## Limits

- **Cross-user attribution needs root.** You can read only your own
  `/proc/*/fd`, so in a shared group quota you can prove a gap exists but not
  name the labmate holding it. The uninspectable count is printed.
- **The deleted-fd scan is node-local.** A job holding a deleted file on a
  compute node is invisible from a login node.
- **Off-site, fields go absent, not zero** — each prints `n/a` with a reason.
- **Interrupting is safe.** Ctrl+C prints the subtrees that finished and says the
  rest is unknown, rather than ranking a half-counted tree.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The suite pins the invariants that decide whether this beats `du` or embarrasses
itself: byte-exact agreement, identical results across thread counts, hard-link
dedup, sparse files not over-counted, a Python 3.6 parse floor — and, for most of
the reconciliation tests, the refusals. The GIF and the chart are both generated
from real output by [`assets/`](assets/); nothing in either is hand-drawn.

---

Before the run, [`slurmate`](https://github.com/PursuitOfDataScience/slurmate)
builds the request. During it,
[`slurmwatch`](https://github.com/PursuitOfDataScience/slurmwatch) watches. After
it, [`slurmpast`](https://github.com/PursuitOfDataScience/slurmpast) says what
happened. rapiDU is for the storage the whole cycle runs on.

MIT

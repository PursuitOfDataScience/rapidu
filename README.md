<h1 align="center">rapiDU</h1>

<p align="center">
  <strong>A much faster <code>du</code> that tells you why your quota is full.</strong>
</p>

<p align="center">
  <a href="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.6%2B-blue.svg" alt="Python 3.6+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/demo.gif" width="900" alt="rapiDU walking a project tree and explaining that it occupies 266.8 MiB to hold 75.5 MiB of data, ranking the same tree by file count, printing a quota table with the age of its snapshot, finding 512 MiB held by a deleted-but-open file descriptor, and catching a freshly written GPFS tree that loses 224 MiB while it settles.">
</p>

## Install

```bash
pip install rapidu
```

## Use

```bash
rdu                      # this directory: how big, and what is big inside it
rdu /project/mylab       # any other path
rdu ~/scratch -n 20      # list 20 entries instead of 10

rdu -i                   # rank by file count -- what an inode quota limits
rdu -c                   # count files only, no stat: ~8x again on GPFS
rdu -Q                   # the quota table, and the age of its figures
rdu -D                   # space held by files deleted while still open
rdu -a                   # the full audit: quota + /proc scan + reconciliation
```

## Faster, and the same number

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/benchmark-dark.png">
    <img src="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/benchmark-light.png" width="720" alt="Cold GPFS walk: du takes 168.1s against rapiDU's 25.4s on a 792,225-file package cache (6.6x), and 298.5s against 57.4s on a 1,686,589-file project directory (5.2x).">
  </picture>
</p>

Same total as `du`, to the byte. That is checked on every commit.

## Reading the table

```
╭───────────────────────────────────────────────────────────────────────────────────╮
│ /project/lab/shared                                                               │
│ 1.4 TiB  ·  5,435 files  ·  87 entries  ·  4.12s                                  │
│                                                                                   │
│   ──────────────────────────────────────────────────────                          │
│         size  share                           files  entry                        │
│    661.5 GiB  █████▏░░░░░░░░░░░░   31.9%        350  checkpoints/                 │
│    343.8 GiB  ██▊░░░░░░░░░░░░░░░   16.6%        968  datasets/                    │
│    470.9 GiB  ▒▒▒▒▒▒▒▒░░░░░░░░░░   22.9%      4,117  (84 more — use -n 0 for all) │
╰───────────────────────────────────────────────────────────────────────────────────╯
```

The frame carries no title and it always closes: a line too wide for it wraps
inside, at a path separator, rather than running past the border. Its colour is a
gradient sweeping from the top-left corner, and it degrades — 24-bit if your
terminal advertises `COLORTERM=truecolor`, a 12-step ramp at 256 colours, and
two bright cyan-to-blue tones at 8. `--ascii` turns it into `+--+`; `--no-box` removes it,
which is what you want when piping into `grep` or a diff.

The path leads, because it is what the report is about; the size is the first of
the four numbers describing it. `share` labels the bar and the percentage beside
it, which are one measurement in two forms -- the picture and the number. The last
column is `entry`, not `path`, because it holds plain files as well as directories
and what is printed is a name relative to the root.

The bar is share of the whole walk, so it always agrees with the number beside
it. The hatched row is everything not listed.
The column you sorted by is the one in colour — under `-i`, `files` takes the tone
and `size` steps back. Sizes are cumulative, so any row agrees with `du -s` on
that path.

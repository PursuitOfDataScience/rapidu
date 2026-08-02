<h1 align="center">rapi<code>DU</code></h1>

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

A faster answer is only worth having if it is the same answer. On a tree with
hard links, a sparse file and 12-deep nesting, `du -s --block-size=1` and rapiDU
at 1/2/4/8/16 threads all return **655,474,688 B** — a test in this repo, not an
aspiration.

## Reading the table

```
      size  of tree              share      files  path
 661.5 GiB  █████▋░░░░░░░░░░░░   31.9%        350  checkpoints/
 343.8 GiB  ██▊░░░░░░░░░░░░░░░   16.6%        968  datasets/
 470.9 GiB  ▒▒▒▒▒▒▒▒░░░░░░░░░░   22.9%      4,117  (84 more — use -n 0 for all)
```

The bar is share of the whole tree, so it always agrees with the number beside
it. The hatched row is everything not listed. The column you sorted by is the one
in colour — under `-i`, `files` takes the tone and `size` steps back. Sizes are
cumulative, so any row agrees with `du -s` on that path.

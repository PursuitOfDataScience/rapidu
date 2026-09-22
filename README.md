<div align="center">

# 📊 rapiDU

**A much faster `du` that tells you why your quota is full.**

<a href="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml"><img src="https://github.com/PursuitOfDataScience/rapidu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://pypi.org/project/rapidu/"><img src="https://img.shields.io/pypi/v/rapidu.svg" alt="PyPI"></a>
<a href="https://pypi.org/project/rapidu/"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/badges/downloads.json" alt="PyPI downloads per month"></a>
<img src="https://img.shields.io/badge/python-3.6%2B-blue.svg" alt="Python 3.6+">
<img src="https://img.shields.io/badge/dependencies-none-brightgreen.svg" alt="No dependencies">
<img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">

<img src="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/demo.gif" width="900" alt="rapiDU sizing a project tree, ranking it by file count, printing a quota table, and finding space held by a deleted file that is still open.">

</div>

## ✨ Install

```bash
pip install rapidu
```

No dependencies and Python 3.6+, so it runs on the oldest login node you have.

## 🧰 Use

```bash
rdu                      # this directory: how big, and what is big inside it
rdu /project/mylab       # any other path
rdu ~/scratch -n 20      # list 20 entries instead of 10
rdu -i                   # rank by inode count, which is what an inode quota limits
rdu -c                   # count files only, no stat: ~8x faster again on GPFS
rdu -Q                   # your quota, and how old its figures are
rdu -D                   # space held by files deleted while still open
rdu -a                   # the full audit
```

`--ascii` and `--no-box` make the output safe to pipe into `grep` or a diff.

## Reading the table 📖

```
╭───────────────────────────────────────────────────────────────────────────────────╮
│ /project/lab/shared                                                               │
│ 1.4 TiB  ·  5,434 inodes  ·  4.12s                                                │
│                                                                                   │
│   ─────────────────────────────────────────────────────────────────────────────── │
│         size  share                          inodes  entry                        │
│    661.5 GiB  ████████░░░░░░░░░░   44.8%        350  checkpoints/                 │
│    343.8 GiB  ████▏░░░░░░░░░░░░░   23.3%        968  datasets/                    │
│    470.9 GiB  ▒▒▒▒▒▒░░░░░░░░░░░░   31.9%      4,116  (84 more — use -n 0 for all) │
╰───────────────────────────────────────────────────────────────────────────────────╯
```

Sizes are cumulative, so each row matches `du -s` on that path. The shaded last row is
everything not listed.

## ⚡ Faster, same answer

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/benchmark-dark.png">
    <img src="https://raw.githubusercontent.com/PursuitOfDataScience/rapidu/main/assets/benchmark-light.png" width="720" alt="Cold GPFS walk: du takes 168.1s against rapiDU's 25.4s on a 792,225-file package cache (6.6x), and 298.5s against 57.4s on a 1,686,589-file project directory (5.2x).">
  </picture>
</div>

Same total as `du`, to the byte, checked on every commit.
One exception: `du -s a b` dedupes inodes *across* its arguments, so a hard link is charged
to whichever path comes first. `rdu` gives each path its own full total.

## 📌 Good to know

🧮 **`inodes` counts directories too**, because your inode quota does.

💽 **On NFS, deleted-but-open files show up as `.nfsXXXX`.** They clear themselves, and
deleting them frees nothing sooner.

📦 **Allocated above apparent is not always waste.** On replicated or erasure-coded storage,
repacking recovers nothing, and `rdu` says so.

🚧 **Silence is not "no quota".** When the quota tool says nothing or is missing, `rdu -Q`
says so and falls back to `statvfs`, which cannot see a per-user limit.

## License

MIT

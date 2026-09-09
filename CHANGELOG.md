# Changelog

All notable changes to rapidu are documented here, newest first.

The format is based on [Keep a Changelog](https://keepachangelog.com), and this
project adheres to [Semantic Versioning](https://semver.org).

## [0.5.0] — 2026-09-09

Covers the work since 0.4.0. New `--json` keys, so a minor bump. Every entry
below shipped with a regression test and a control verified in both states.

### Added

- **`--json` now says why the density ranking is short.** `walk.top_hidden` covers
  the `-n` truncation of `top_by_size` and `top_by_inodes` and deliberately does
  not speak for `top_by_density`, which is cut by an inode floor instead — and
  nothing else did. Measured on a 885-inode tree, `rdu … -d 1 -n 0 --json`:
  `top_by_size` and `top_by_inodes` publish 14 entries, `top_by_density` publishes
  2, and `top_hidden.count` reads `0` — correctly, because `-n 0` cut nothing. So
  every truncation figure in the document said "complete" beside a ranking missing
  twelve of its fourteen entries, while the terminal printed the reason on the same
  walk: `12 of 14 entries hold fewer than 100 inodes and cannot be ranked by
  density`. It was not derivable, either: the floor is `max(100, inodes // 100)`, a
  rule the document states nowhere, and a zero-byte subtree is dropped too.

  New `walk.top_density_hidden` with `inode_floor`, `below_floor` and
  `truncated_by_limit`. **Two reasons, two figures**, which is the table's policy
  and not a new one: `below_floor` was never rankable and `-n` will not bring it
  back, while `truncated_by_limit` cleared the floor and `-n 0` will — so a
  consumer can tell "short because of the floor" from "short because you passed
  `-n 2`", and no figure here claims `-n` hid what the floor hid. `null` under
  `-c`, like `top_by_density` itself, because there is no density ranking to be
  short: emitted raw it read `below_floor: 0`, a filter that never ran reported as
  a filter that dropped nothing. Both counts come from the counter the table reads,
  so the sentence and the key cannot disagree. Adding a key does not move `schema`,
  which counts removals and changes of meaning; it still reads 5.

- **`--json` now says what `-n` cut off.** The three rankings were truncated to
  `-n` with nothing in the document naming what had been dropped, so five rows of
  `top_by_size` read identically whether the tree had five children or fourteen —
  `dirs` counts every directory walked, not the root's children, so the number was
  not recoverable from anywhere else. The terminal has always said it (`9 more —
  use -n 0 for all`, with the remainder's bytes and inodes on the row), which made
  this the same drift the parity tests exist to catch. New `walk.top_hidden` with
  `count`, `by_size` and `by_inodes`, following the table's own policy rather than
  a new one.

  The count is shared and the figures are not, because the document publishes
  *two* rankings and they select different entries: `by_size` and `by_inodes` each
  carry the `bytes`/`inodes` remainder of their own listing, computed from the
  entries that listing shows, exactly as the terminal computes its remainder row
  from the rows it is printing. So a consumer can complete either ranking and land
  on `size_bytes` and `inodes` exactly. One shared pair could not do that:
  measured on six 5 MiB directories beside six directories of ~40 tiny files,
  `--json -n 5` published `inodes: 264` against a `top_by_inodes` whose rows sum
  to 220 on a 274-inode tree, so completing the inode ranking gave 484 inodes —
  while `rdu -n 5` and `rdu -i -n 5` correctly print 264 and 54 on that same tree.
  `count` stays one figure because both rankings cut the same sibling set to the
  same `-n`, so the same *number* of entries is missing from each.

  Both remainders are `null` where the table suppresses its remainder row — an
  interrupted walk, or a nested listing where a leftover would double-count — and
  `bytes` is `null` rather than `0` under `-c`, which never stats (there the two
  remainders coincide, because `top_dirs` coerces a `size` request to `files` when
  there is no size to rank on). `count` is stated at any depth *and* on an
  interrupted walk, which is more than the table says: `say_hidden` and the
  remainder row both require a complete walk, so after an interrupt the table
  prints no truncation row at all. Adding a key does not move `schema`, which
  counts removals and changes of meaning; it still reads 5.

### Fixed

- **Two figures the document published as zero without having measured them.**
  Constraint 10 is that `None` is not zero — a caller with no measurement passes
  `None` and gets `n/a` — and both of these were that rule missed in a branch
  where a sibling field already obeyed it.

  Under `-c` no stat is taken, so the re-stat has nothing to compare and
  `recheck_ran` reads `false`. `recent_files`, `touched_files` and
  `future_mtime_files` were already nulled, and `settled` is explicitly `None`
  there — the comment calls it "the strongest claim in this section, made by an
  instrument that was switched off". But `drift_bytes`, `vanished_files` and
  `vanished_allocated_bytes` went out raw, so a walk that read no sizes reported
  that nothing had changed size. Twelve lines below where it was emitted, the same
  block already says what that value is: "`drift_bytes: 0` is an absent reading and
  not a settled tree". All three now go through `_unmeasured` like their siblings;
  `rechecked`, `recheck_gap_seconds` and `recheck_ran` keep their numbers, because
  those describe the check rather than the tree and are how a consumer learns to
  expect the nulls.

  The second is not about `-c` at all. With a quota backend that answered and no
  row mapping to the path, `reconcile` returns `verdict: not-compared` and every
  figure describing the comparison is null — `walked`, `accounted`, `quota`,
  `difference`, `share_of_quota`. `deleted_but_open` and `tolerance` read `0`,
  being the only two fields `Reconciliation.__init__` starts at a number instead of
  at `None`. Measured on a **full** walk: `walked: null` beside `tolerance: 0`, a
  threshold for a comparison that never happened — and none was computed, since
  `_tolerance()` needs a `quota_value` there is not one of. Both are now null on
  the same condition their siblings already use: `accounted` returns `None` when
  `walk_value is None`, and `within_tolerance` only ever consults `tolerance` when
  `gap is not None`. The object keeps its numeric defaults, which no decision
  reads; only the published figure changed. No `schema` bump either way — a field
  narrowing from a false zero to `null` is the same class of correction as the
  whole-sample-deleted case that took `settled` to `null`.

- **A bar filled to its last cell did not mean full, on the two paths that have no
  partial blocks.** `fmt.pct` goes to some trouble to keep the label honest —
  `>99.9%` throughout 99.95–99.99, so `100.0%` appears only when part really equals
  whole — and the Unicode bar beside it agreed, because its fill floors and the
  last cell short of 1.0 is always a partial glyph. The other two paths round the
  final cell up instead: `--ascii`, where `Style.partials` is empty, and the
  hatched remainder row, which discards the partials by design. Measured across
  90 → 100% at the widths the report uses (10 for the quota gauge, 12 for the age
  histogram, 18 for the ranked table): both went solid from **93.8%** at 8 cells
  and **97.2%** at 18. So `rdu --ascii` on a 97.5%-full quota drew `##########`
  beside the text `97.5%` — the picture said "you are out of space", the number
  beside it said 100 000 files of headroom — and on a Unicode terminal a `-n 1`
  listing whose hidden siblings hold 97.5% of the tree drew eighteen solid hatch
  cells beside its own `97.5%`.

  Both paths now hold the final cell back until the fraction reaches 1.0, keyed on
  the unrounded value because that is exactly what `pct` prints beside it. The
  97.5% quota row reads `#########-   97.5%`; at the limit it still reads
  `##########  100.0%`, and over it (`105.6%`, the one state a clamped bar cannot
  express) still fills solid beside the `OVER` marker. Nothing below the turnover
  moved — the reserve can only bite where the round-up would have reached the last
  cell — and neither half of the low-end rule changed: `min_tick` still lights a
  sliver for any non-zero share, and `min_tick=False` still leaves 0.04% of a quota
  empty. At width 1 the reserve and `min_tick` want the same single cell and the
  reserve wins, which is how `slurmpast` resolves the same collision; no surface
  here draws a bar narrower than ten.

  Second member of a family-wide sweep of this invariant, each keyed to the
  precision of its own labels: `slurmpast.render.bar_cells` on `round(percent, 1)`,
  `slurmwatch.tui` on the unrounded percent under a `>99%` label, and this one on
  the fraction, against 1.0.

- **A ratio just under parity was printed as parity.** The allocated-over-apparent
  figure used two decimals below 1, so `0.9999` rendered as `1.00x` — "allocated
  equals apparent" about a tree where it does not, and parity is exactly the
  reading the ALLOCATION panel is scanned for. Measured on the real surface:
  `apparent 976.6 KiB (1.00x allocated)` for a 975.6/976.6 KiB tree. It now reads
  `<1.00x`, the same inequality the percentage formatter already uses at its own
  top end, and an exact 1.0 still prints `1.0x`.
- **The `deleted_but_open` document reported figures for a sweep that never ran.**
  With `/proc` unavailable the terminal correctly refuses to name anything
  (`n/a - /proc is not available on this platform`) while the JSON published
  `total_bytes: 0`, `inodes: 0` and four more — each a *measurement* of the node
  saying nothing is holding space. Those six now read `null`, so a consumer can
  tell "nothing to reclaim" from "nothing was looked at". A sweep that really ran
  and found nothing still reports zeros.
- **A quota query was reported as timing out when it was never given any time.**
  When the overall deadline was already spent the command was still spawned, only
  to be killed on the next line, and the reader was told "timed out after 0s" —
  which reads as rapidu having used a zero timeout, i.e. its own bug. Nothing is
  spawned now, and the reason says the budget ran out.
- **A sub-second query budget was printed as zero.** `timed out after 0s` for a
  400 ms allowance, for the same reason. Sub-second budgets now print their real
  value; whole seconds are unchanged.

### Changed

- Ranking by files is now reflected in the report header, which had ignored
  `--sort`, and the unreadable-directory and owner listings state their bound
  rather than silently truncating.

# Known engine bugs, not yet fixed

Bugs confirmed on the stock v4.2.9 release that this repository does not fix yet. Each fix lands on `main` as its own commit, as bugs 1 to 9 did; its record then moves into the fix's description. Numbers continue the list of fixed bugs.

Keep this folder private: a bug marked "report privately" lets a policy allow what it should refuse.

| # | Bug | Effect on stock 4.2.9 | Cause | Severity | Record |
|---|---|---|---|---|---|
| 10 | An `EXISTS` whose `BIND` computes its `GRAPH` name never matches | `EXISTS` is false and `NOT EXISTS` true for every row, in queries and in policies. A `NOT EXISTS` policy of this shape allows every identity. | Fluree evaluates the check once for all rows instead of once per row. Its guard that forces per-row evaluation covers a graph name from outside the check (its issue #1443), not one computed inside it. The fix extends the guard to this shape. | Report privately | [10-exists-bind-graph-name.md](10-exists-bind-graph-name.md) |

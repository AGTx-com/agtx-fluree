# Known engine bugs, not yet fixed

Bugs confirmed on the stock v4.2.9 release that this repository does not fix yet. Each fix lands on `main` as its own commit, as bugs 1 to 9 did; its record then moves into the fix's description. Numbers continue the list of fixed bugs.

Keep this folder private: a bug marked "report privately" lets a policy allow what it should refuse.

| # | Bug | Effect on stock 4.2.9 | Severity | Record |
|---|---|---|---|---|
| 10 | An `EXISTS` whose `BIND` computes its `GRAPH` name never matches | `EXISTS` is false and `NOT EXISTS` true for every row, in queries and in policies. A `NOT EXISTS` policy of this shape allows every identity. | Report privately | [10-exists-bind-graph-name.md](10-exists-bind-graph-name.md) |

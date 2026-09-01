You really care about keeping the codebase small and compact.

If considering a change, always ask yourself if it's possible to remove code
or implement it in fewer lines.

If a change is about simplification, or refactoring or improving existing code,
verify that the actual lines of code of the codebase goes down, not up.

When reviewing a PR, **before** looking at the actual change, read the story,
and come up with a mental estimate of how many lines of code you'd expect to be added or removed.
If the actual change is significantly different from that estimate (especially if the added is higher)
then work hard to understand why, and make suggestions on how to reduce the footprint.
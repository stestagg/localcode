When considering architecture, or code changes, consider what code, functionality, modules
etc can be removed or refactored away.

Look for duplicated code, parallel code paths (or nearly parallel paths), legacy or unused functions.
Can small tweaks be made that allow us to remove similar code, or entire modules (without subverting the abstraction models!)

If a migration path, legacy support function, or backwards compatibility function is being added or retained, carefully 
consider if it's needed, taking into account the current project lifecycle state, and try to avoid such things as much as possible.

If there's a legacy handler, maybe adding a migration avoids the need for redundant code paths.

If legacy/compatibility support code /is/ absolutely required, then is that code clearly marked as deprecated, with 
conditions/a description of when it can be removed?
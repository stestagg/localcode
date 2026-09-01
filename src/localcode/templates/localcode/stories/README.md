# Stories

This directory contains the project's stories. Each story is a Markdown file
named `NN-<slug-style-title>.md`, where `NN` is its sequence number and the
remainder is a short, lowercase, hyphen-separated title. For example:
`07-add-password-reset.md`.

## Story format

A story starts with YAML frontmatter containing its title and date. Add `pr_id`
when development starts and a pull request exists.

```markdown
---
title: Add password reset
date: 2026-08-28
pr_id: 42
---

## Description 
Describe the change, why it is needed, and the expected outcome. Include enough
detail for someone to develop the change at the current understanding.

## Definition of done
Include user-verifiable acceptance criteria, a story should have a user visible change, ideally
in a primary app user interface.  Sometimes things like performance or refactor stories may not be
able to have user visible changes, but for new functionality, a story that can't surface a user visible
change may need to be re-considered, or the work stream restructured slightly.


## Comments

Any comments or notes generated over time during refinement or development of the story
can be added here, only include comments or information that is relevant to understanding
how best to implement the story. Date the comments and keep them in chronological order.
```

Before a pull request exists, omit `pr_id`. Dates use the `YYYY-MM-DD` format.

## Lifecycle

Stories move between these directories as work progresses:

1. `backlog/`: new stories begin here.
2. `ready/`: the BA has groomed and refined the story so it is ready for
   development.
3. `in_progress/`: a developer has started the story. Add its `pr_id` once the
   pull request has been created. The story remains here while the change is
   reviewed and any review feedback is addressed.
4. `done/`: move the story here after its pull request has been merged.
5. `cancelled/`: move a story here from any active stage if it becomes obsolete
   or is superseded.

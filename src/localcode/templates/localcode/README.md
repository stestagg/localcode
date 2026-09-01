# .localcode

Everything localcode knows about this project, except the source itself.

| path | what it is |
| --- | --- |
| `personas/` | reusable perspectives and general attitudes, one plain Markdown file each |
| `roles/` | task-specific instructions, one plain Markdown file each |
| `stories/` | stories and their lifecycle, from backlog through completion |
| `tools/` | project-specific workflow tools the agents drive the work through |
| `opencode.json` | which LLM this project uses, in opencode's config format |
| `state/` | gitignored: gitea's database, the bare master repo, the runtime secret, and the provider keys |

Everything outside `state/` is committed only on the `localcode` branch. That
branch is regularly rebased onto `main`, but `.localcode/` never enters a
functional branch or pull request. Agents read and write these files there.

An agent run combines any one persona with any one role. There is no allow-list
or compatibility mapping between them.

The project name and Gitea repository name come from the repository directory.
The HTTP port is runtime-only and can be selected with `localcode run --port`.

`opencode.json` names the model but never the key: keys live in
`state/opencode-auth.json`, which is not committed. `localcode llm configure`
writes both.

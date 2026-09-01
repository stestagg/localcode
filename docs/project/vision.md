# localcode - Vision

A bunch of agents all working together to build some software.

Work is serialized, but split across agents, each with a diferent concern.

## Brainstorm

 - Personas describe perspective and attitude; roles describe task-specific work
    - any persona can perform any role
    - localcode has personas and roles built in, but a project can add or override them
    - .localcode/ top-level folder for all config, project data, and other metadata (everything except the actual source code)
 - Stories in the project dir (.localcode/stories/backlog/NN-xxxxxxxx.md) as markdown

 - Most workflow actions implemented as tools (not direct drive by agent)
 - LLM via API (but local)
 - Work continues in a loop story-by-story until the PM confirms everything complete
 - Periodic per-file review
     - Describe and catalog file function, assess whether lines of code seem appropriate to the functionality
 - Each story has verifiable acceptance criteria, e.g. screen capture/video/demo that is saved / made available for human review
 - project status summary

### Architecture
 
 Code:
 - personas/ and roles/ directories containing reusable plain-Markdown instructions
 - tools/ implementation of workflow tools
 - core/  main loop and orchestration

 Arch Components:
  - Container mangement
  - caddy, gitea, custom pages flask app, agent runtime (async python)
  - cli frontend shim
  - personas, roles & tools
  - 



### Interfaces
 - launch from cli
 - runs in a container with persistent volume for any data*
 - runs & exposes (behind caddy, see below) gitea <- git ui for user interface and PR workflow
 - custom pages alongside gitea: (initially, strictly read-only) views on project data in the repo (links to gitea editor for editing) - i.e. backlog summary, progress, burndown etc..
  - 


 ### Invocation:

```
$ localcode init
(interactive bootstrap)
```

```
$ localcode run
(kicks off the loop, including the gitea server, and the agents)
```

```
$ localcode serve
(runs the gitea and custom page server, and the backend, but doesn't start any agent activity)
```

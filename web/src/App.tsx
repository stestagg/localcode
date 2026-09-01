import {
  AnchorButton,
  Button,
  Card,
  Divider,
  HTMLSelect,
  Icon,
  Navbar,
  NavbarGroup,
  NavbarHeading,
  Spinner,
  Tag,
  TextArea,
} from "@blueprintjs/core";
import { useEffect, useId, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { SessionView } from "./SessionView";
import { useLocalcode } from "./useLocalcode";
import type {
  CommandRunState,
  Connection,
  DefinitionIssue,
  PromptDefinition,
  Story,
  StoryState,
} from "./types";

type Section = "overview" | "stories" | "pulls" | "personas" | "roles" | "chat" | "ask";
type NavigationItem = {
  id: Section;
  label: string;
  icon:
    | "dashboard"
    | "list-detail-view"
    | "git-pull"
    | "person"
    | "id-number"
    | "chat";
};

const SUCCESS_LINGER_MS = 4000;
const ACTIVE_STORY_STATES: StoryState[] = ["backlog", "ready", "in_progress"];

function sectionFromHash(hash: string): Section {
  const value = hash.slice(1);
  const section = value === "agents" ? "personas" : value;
  return NAVIGATION.some((item) => item.id === section)
    ? section as Section
    : "overview";
}

const NAVIGATION_GROUPS: { label: string; items: NavigationItem[] }[] = [
  {
    label: "Project",
    items: [
      { id: "overview", label: "Overview", icon: "dashboard" },
      { id: "stories", label: "Stories", icon: "list-detail-view" },
      { id: "pulls", label: "Pull Requests", icon: "git-pull" },
    ],
  },
  {
    label: "Agent definitions",
    items: [
      { id: "personas", label: "Personas", icon: "person" },
      { id: "roles", label: "Roles", icon: "id-number" },
    ],
  },
  {
    label: "Questions",
    items: [
      { id: "chat", label: "Chat", icon: "chat" },
      { id: "ask", label: "Ask", icon: "chat" },
    ],
  },
];
const NAVIGATION = NAVIGATION_GROUPS.flatMap((group) => group.items);

const SECTION_DESCRIPTION: Record<Section, string> = {
  overview: "README and project information from the repository.",
  stories: "Project stories grouped by lifecycle state.",
  pulls: "Changes waiting for review in the project repository.",
  personas: "Perspectives and attitudes available to agents.",
  roles: "Task-specific instructions available to agents.",
  chat: "Hold a continuing conversation with one persona.",
  ask: "Put one question to a persona, and watch it answer.",
};

const CONNECTION_LABEL: Record<Connection, string> = {
  connecting: "Connecting",
  ready: "Connected",
  closed: "Disconnected",
  denied: "Not authorised",
};

const CONNECTION_INTENT: Record<Connection, "none" | "success" | "danger"> = {
  connecting: "none",
  ready: "success",
  closed: "danger",
  denied: "danger",
};

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="detail-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function readmeAssetUrl(repoUrl: string, value: string | undefined, raw = false) {
  if (!value || /^(?:[a-z][a-z\d+.-]*:|#|\/)/i.test(value)) return value;
  const view = raw ? "raw" : "src";
  return new URL(value, `${repoUrl}/${view}/branch/main/`).toString();
}

function RunStateIcon({ state }: { state: CommandRunState }) {
  if (state === "running") return <Spinner size={14} />;
  return <Icon icon={state === "success" ? "tick-circle" : "error"} intent={state === "success" ? "success" : "danger"} />;
}

function StoryRows({ stories }: { stories: Story[] }) {
  if (stories.length === 0) return <div className="story-empty">No stories.</div>;
  return (
    <ul className="story-list">
      {stories.map((story) => (
        <li key={story.fileUrl}>
          <span className="story-number">{String(story.number).padStart(2, "0")}</span>
          <a href={story.fileUrl}>{story.title}</a>
          <span className="story-metadata">
            {story.prId && <Tag minimal>PR #{story.prId}</Tag>}
            {story.date && <time dateTime={story.date}>{story.date}</time>}
          </span>
        </li>
      ))}
    </ul>
  );
}

function StorySection({
  state,
  label,
  stories,
  collapsed = false,
  expanded = true,
  onToggle,
  action,
}: {
  state: StoryState;
  label: string;
  stories: Story[] | undefined;
  collapsed?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  action?: React.ReactNode;
}) {
  return (
    <Card className={`story-section story-${state}`}>
      {collapsed ? (
        <button
          className="story-section-heading story-section-toggle"
          type="button"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          <Icon icon={expanded ? "chevron-down" : "chevron-right"} />
          <h2>{label}</h2>
          {stories && <Tag minimal>{stories.length}</Tag>}
        </button>
      ) : (
        <div className="story-section-heading">
          <h2>{label}</h2>
          {action}
          {stories && <Tag minimal>{stories.length}</Tag>}
        </div>
      )}
      {expanded && (
        stories === undefined
          ? <div className="story-loading"><Spinner size={16} /> Loading stories…</div>
          : <StoryRows stories={stories} />
      )}
    </Card>
  );
}

function DefinitionCatalog({
  label,
  kind,
  definitions,
  issues,
}: {
  label: string;
  kind: "Persona" | "Role";
  definitions: PromptDefinition[];
  issues: DefinitionIssue[];
}) {
  return (
    <section className="agent-catalog" aria-label={label}>
      {definitions.length === 0 && issues.length === 0 ? (
        <Card className="empty-state">No {label.toLowerCase()} defined.</Card>
      ) : (
        <div className="agent-grid">
          {definitions.map((item) => (
            <DefinitionCard item={item} kind={kind} key={item.name} />
          ))}
          {issues.map((issue) => (
            <Card className="agent-card agent-card-invalid" key={issue.name}>
              <div className="agent-card-heading">
                <a href={issue.fileUrl}>{issue.name}</a>
                <div className="agent-card-actions">
                  <AnchorButton href={issue.editUrl} icon="edit" minimal small>Edit</AnchorButton>
                  <Tag minimal intent="danger">Invalid</Tag>
                </div>
              </div>
              <p>{issue.message}</p>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

function DefinitionCard({
  item,
  kind,
}: {
  item: PromptDefinition;
  kind: "Persona" | "Role";
}) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const hasMore = item.prompt !== item.promptPreview;

  return (
    <Card className="agent-card">
      <div className="agent-card-heading">
        <a href={item.fileUrl}>{item.name}</a>
        <div className="agent-card-actions">
          <AnchorButton href={item.editUrl} icon="edit" minimal small>
            Edit
          </AnchorButton>
          <Tag minimal>{kind}</Tag>
        </div>
      </div>
      <div className="agent-card-content" id={contentId}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {expanded ? item.prompt : item.promptPreview}
        </ReactMarkdown>
      </div>
      {hasMore && (
        <Button
          className="agent-card-toggle"
          minimal
          small
          rightIcon={expanded ? "chevron-up" : "chevron-down"}
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
      )}
    </Card>
  );
}

export default function App() {
  const {
    connection,
    status,
    pulls,
    stories,
    runs,
    sessions,
    sessionList,
    loadStories,
    dismissRun,
    ask,
    startProcess,
    subscribeSession,
    sessionInput,
    sessionControl,
  } = useLocalcode();
  const [section, setSection] = useState<Section>(() => sectionFromHash(window.location.hash));
  const [persona, setPersona] = useState("");
  const [question, setQuestion] = useState("");
  const [askSessionId, setAskSessionId] = useState<string | null>(null);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [expandedArchive, setExpandedArchive] = useState({ done: false, cancelled: false });
  const runPane = useRef<HTMLDivElement>(null);
  const runRemovalTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const personas = status?.personas ?? [];
  const personaIssues = status?.personaIssues ?? [];
  const roles = status?.roles ?? [];
  const roleIssues = status?.roleIssues ?? [];
  const actingPersona = persona && personas.some((item) => item.name === persona)
    ? persona
    : (personas[0]?.name ?? "");
  const ready = connection === "ready";
  const sectionLabel = NAVIGATION.find((item) => item.id === section)?.label ?? "Overview";
  const selectedRun = runs.find((run) => run.id === selectedRunId);
  const askSession = askSessionId ? sessions[askSessionId] : undefined;
  const chatSession = chatSessionId ? sessions[chatSessionId] : undefined;
  const askSessions = sessionList.filter((item) => item.process === "ask");
  const chatSessions = sessionList.filter((item) => item.process === "chat");
  const chatAvailable = status?.processes.some((item) => item.name === "chat") ?? false;

  useEffect(() => {
    if (window.location.hash === "#agents") {
      window.history.replaceState(null, "", "#personas");
    }
    const selectSection = () => setSection(sectionFromHash(window.location.hash));
    window.addEventListener("hashchange", selectSection);
    return () => window.removeEventListener("hashchange", selectSection);
  }, []);

  useEffect(() => {
    if (section !== "stories" || !ready) return;
    const missing = ACTIVE_STORY_STATES.filter((state) => stories[state] === undefined);
    if (missing.length > 0) loadStories(missing);
  }, [loadStories, ready, section, stories]);

  const toggleArchive = (state: "done" | "cancelled") => {
    const opening = !expandedArchive[state];
    setExpandedArchive((current) => ({ ...current, [state]: opening }));
    if (opening && ready && stories[state] === undefined) loadStories([state]);
  };

  const navigate = (next: Section) => {
    if (next === section) return;
    window.location.hash = next;
  };

  useEffect(() => {
    const node = runPane.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [selectedRun?.output]);

  useEffect(() => {
    if (selectedRunId && !selectedRun) setSelectedRunId(null);
  }, [selectedRun, selectedRunId]);

  useEffect(() => {
    const removable = new Set(
      runs
        .filter((run) => run.state === "success" && run.id !== selectedRunId)
        .map((run) => run.id),
    );

    for (const run of runs) {
      if (!removable.has(run.id) || runRemovalTimers.current.has(run.id)) continue;
      const elapsed = Date.now() - (run.completedAt ?? Date.now());
      const timer = setTimeout(() => {
        runRemovalTimers.current.delete(run.id);
        dismissRun(run.id);
      }, Math.max(0, SUCCESS_LINGER_MS - elapsed));
      runRemovalTimers.current.set(run.id, timer);
    }

    for (const [id, timer] of runRemovalTimers.current) {
      if (removable.has(id)) continue;
      clearTimeout(timer);
      runRemovalTimers.current.delete(id);
    }
  }, [dismissRun, runs, selectedRunId]);

  useEffect(
    () => () => {
      for (const timer of runRemovalTimers.current.values()) clearTimeout(timer);
      runRemovalTimers.current.clear();
    },
    [],
  );

  return (
    <div className="app-shell">
      <Navbar className="app-header">
        <NavbarGroup>
          <Icon icon="cube" size={18} />
          <NavbarHeading>{status?.project.name ?? "localcode"}</NavbarHeading>
        </NavbarGroup>
        <NavbarGroup align="right">
          <Tag minimal intent={CONNECTION_INTENT[connection]} round>
            {CONNECTION_LABEL[connection]}
          </Tag>
          <AnchorButton
            href={status?.gitea.url ?? "/gitea/"}
            icon="git-repo"
            rightIcon="arrow-right"
            minimal
          >
            Gitea
          </AnchorButton>
        </NavbarGroup>
      </Navbar>

      <div className="app-body">
        <aside className="app-sidebar" aria-label="Primary navigation">
          <nav>
            {NAVIGATION_GROUPS.map((group) => (
              <div className="nav-group" role="group" aria-label={group.label} key={group.label}>
                {group.items.map((item) => (
                  <button
                    key={item.id}
                    className={`nav-item ${section === item.id ? "active" : ""}`}
                    type="button"
                    aria-current={section === item.id ? "page" : undefined}
                    onClick={() => navigate(item.id)}
                  >
                    <Icon icon={item.icon} />
                    <span>{item.label}</span>
                    {item.id === "pulls" && pulls.length > 0 && <Tag minimal>{pulls.length}</Tag>}
                  </button>
                ))}
              </div>
            ))}
          </nav>
          <div className="sidebar-footer">
            <Divider />
            <span>Project {status?.project.id ?? "—"}</span>
          </div>
        </aside>

        <main className="app-content">
          <div className="page-heading">
            <div>
              <h1>{sectionLabel}</h1>
              <p>{SECTION_DESCRIPTION[section]}</p>
            </div>
            {section === "pulls" && <Tag minimal>{pulls.length} open</Tag>}
          </div>

          {section === "overview" && (
            <section aria-labelledby="overview-heading">
              <h2 id="overview-heading" className="visually-hidden">Overview</h2>
              {!status ? (
                <Card className="loading-card">
                  <Spinner size={20} />
                  <span>Waiting for project status…</span>
                </Card>
              ) : (
                <div className="overview-view">
                  <Card className="readme-card">
                    <div className="readme-heading">
                      <div>
                        <Icon icon="document" />
                        <h2>README.md</h2>
                        <Tag minimal>main</Tag>
                      </div>
                      {status.project.readme !== null && (
                        <AnchorButton
                          href={status.project.readmeUrl}
                          icon="code"
                          minimal
                          small
                        >
                          View source
                        </AnchorButton>
                      )}
                    </div>
                    {status.project.readme === null ? (
                      <div className="readme-empty">
                        <Icon icon="document" size={28} />
                        <span>No README.md found on main.</span>
                      </div>
                    ) : (
                      <article className="readme-content">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            a: ({ href, ...props }) => (
                              <a {...props} href={readmeAssetUrl(status.gitea.url, href)} />
                            ),
                            img: ({ src, alt, ...props }) => (
                              <img
                                {...props}
                                src={readmeAssetUrl(status.gitea.url, src, true)}
                                alt={alt ?? ""}
                              />
                            ),
                          }}
                        >
                          {status.project.readme}
                        </ReactMarkdown>
                      </article>
                    )}
                  </Card>

                  <div className="overview-grid">
                    <Card className="info-card">
                      <div className="card-heading">
                        <Icon icon="git-repo" />
                        <h2>Repository</h2>
                      </div>
                      <dl>
                        <Detail label="Path"><code>{status.project.path}</code></Detail>
                        <Detail label="Head"><code>{status.project.head || "No commits yet"}</code></Detail>
                        <Detail label="Branches">{status.project.branches.join(", ") || "—"}</Detail>
                      </dl>
                    </Card>

                    <Card className="info-card">
                      <div className="card-heading">
                        <Icon icon="cloud" />
                        <h2>Environment</h2>
                      </div>
                      <dl>
                        <Detail label="Master"><a href={status.gitea.url}>{status.gitea.repo}</a></Detail>
                        <Detail label="Sign in">
                          <code>{status.gitea.user} / {status.gitea.password}</code>
                        </Detail>
                        <Detail label="Personas">
                          {status.personas.map((item) => item.name).join(", ") || "None defined"}
                        </Detail>
                        <Detail label="Roles">
                          {status.roles.map((item) => item.name).join(", ") || "None defined"}
                        </Detail>
                        <Detail label="Hub">
                          <Tag minimal intent={status.hub.running ? "success" : "none"}>
                            {status.hub.running ? "Running" : "Stopped"}
                          </Tag>
                        </Detail>
                      </dl>
                    </Card>
                  </div>
                </div>
              )}
            </section>
          )}

          {section === "stories" && (
            <section className="stories-view" aria-label="Stories by lifecycle state">
              <StorySection
                state="backlog"
                label="Backlog"
                stories={stories.backlog}
                action={(
                  <AnchorButton
                    className="story-add"
                    href={status ? `${status.gitea.url}/_new/localcode/.localcode/stories/backlog` : undefined}
                    icon="plus"
                    minimal
                    small
                    disabled={!status}
                    aria-label="Add backlog story"
                    title="Add backlog story"
                  />
                )}
              />
              <StorySection state="ready" label="Ready" stories={stories.ready} />
              <StorySection state="in_progress" label="In progress" stories={stories.in_progress} />
              <StorySection
                state="done"
                label="Done"
                stories={stories.done}
                collapsed
                expanded={expandedArchive.done}
                onToggle={() => toggleArchive("done")}
              />
              <StorySection
                state="cancelled"
                label="Cancelled"
                stories={stories.cancelled}
                collapsed
                expanded={expandedArchive.cancelled}
                onToggle={() => toggleArchive("cancelled")}
              />
            </section>
          )}

          {section === "personas" && (
            <DefinitionCatalog
              label="Personas"
              kind="Persona"
              definitions={personas}
              issues={personaIssues}
            />
          )}

          {section === "roles" && (
            <DefinitionCatalog
              label="Roles"
              kind="Role"
              definitions={roles}
              issues={roleIssues}
            />
          )}

          {section === "chat" && (
            <section className="ask-view" aria-label="Chat with a persona">
              <Card className="ask-form">
                <div className="ask-form-row">
                  <label htmlFor="chat-persona">Chat with</label>
                  <HTMLSelect
                    id="chat-persona"
                    value={actingPersona}
                    disabled={!ready || personas.length === 0 || !chatAvailable}
                    onChange={(event) => setPersona(event.target.value)}
                  >
                    {personas.map((item) => (
                      <option key={item.name} value={item.name}>{item.name}</option>
                    ))}
                  </HTMLSelect>
                  <span className="ask-form-note">
                    {personas.length === 0
                      ? "No personas defined."
                      : "The process waits for your first message."}
                  </span>
                  <Button
                    icon="chat"
                    intent="primary"
                    disabled={!ready || !actingPersona || !chatAvailable}
                    onClick={() => setChatSessionId(startProcess("chat", actingPersona))}
                  >
                    Start chat
                  </Button>
                </div>
              </Card>

              <SessionView
                session={chatSession}
                textInput
                pauseResume
                stop
                onControl={(control) =>
                  chatSessionId && sessionControl(chatSessionId, control)
                }
                onInput={(text) => chatSessionId && sessionInput(chatSessionId, text)}
              />

              {chatSessions.length > 0 && (
                <Card className="session-list">
                  <h2>Earlier conversations</h2>
                  <ul>
                    {chatSessions.map((item) => (
                      <li key={item.session}>
                        <button
                          type="button"
                          className={chatSessionId === item.session ? "selected" : ""}
                          onClick={() => {
                            setChatSessionId(item.session);
                            subscribeSession(item.session);
                          }}
                        >
                          <span>{item.title || item.session}</span>
                          <Tag minimal>{item.messages}</Tag>
                          <time dateTime={new Date(item.at * 1000).toISOString()}>
                            {new Date(item.at * 1000).toLocaleString()}
                          </time>
                        </button>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </section>
          )}

          {section === "ask" && (
            <section className="ask-view" aria-label="Ask a persona">
              <Card className="ask-form">
                <div className="ask-form-row">
                  <label htmlFor="ask-persona">Ask</label>
                  <HTMLSelect
                    id="ask-persona"
                    value={actingPersona}
                    disabled={!ready || personas.length === 0}
                    onChange={(event) => setPersona(event.target.value)}
                  >
                    {personas.map((item) => (
                      <option key={item.name} value={item.name}>{item.name}</option>
                    ))}
                  </HTMLSelect>
                  <span className="ask-form-note">
                    {personas.length === 0
                      ? "No personas defined."
                      : "Its instructions become the model's system prompt."}
                  </span>
                </div>
                <TextArea
                  id="ask-question"
                  className="ask-question"
                  value={question}
                  disabled={!ready || !actingPersona}
                  placeholder="What would you like to ask?"
                  onChange={(event) => setQuestion(event.target.value)}
                />
                <div className="ask-form-actions">
                  <Button
                    icon="chat"
                    intent="primary"
                    disabled={!ready || !actingPersona || !question.trim()}
                    onClick={() => {
                      setAskSessionId(ask(actingPersona, question.trim()));
                      setQuestion("");
                    }}
                  >
                    Ask
                  </Button>
                </div>
              </Card>

              {/* One question, one answer: it can be stopped, but there is no
                  turn after this one to steer, so the input and pause are off. */}
              <SessionView
                session={askSession}
                textInput={false}
                pauseResume={false}
                stop
                onControl={(control) =>
                  askSessionId && sessionControl(askSessionId, control)
                }
                onInput={(text) => askSessionId && sessionInput(askSessionId, text)}
              />

              {askSessions.length > 0 && (
                <Card className="session-list">
                  <h2>Earlier questions</h2>
                  <ul>
                    {askSessions.map((item) => (
                      <li key={item.session}>
                        <button
                          type="button"
                          className={askSessionId === item.session ? "selected" : ""}
                          onClick={() => {
                            setAskSessionId(item.session);
                            subscribeSession(item.session);
                          }}
                        >
                          <span>{item.title || item.session}</span>
                          <Tag minimal>{item.messages}</Tag>
                          <time dateTime={new Date(item.at * 1000).toISOString()}>
                            {new Date(item.at * 1000).toLocaleString()}
                          </time>
                        </button>
                      </li>
                    ))}
                  </ul>
                </Card>
              )}
            </section>
          )}

          {section === "pulls" && (
            <section aria-label="Open pull requests">
              <Card className="pull-card">
                {pulls.length === 0 ? (
                  <div className="empty-state">No open pull requests.</div>
                ) : (
                  <ul className="pull-list">
                    {pulls.map((pull) => (
                      <li key={pull.number}>
                        <Icon icon="git-pull" />
                        <a href={pull.url}>#{pull.number}</a>
                        <span>{pull.title}</span>
                        <code>{pull.branch}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </section>
          )}

        </main>
      </div>

      {selectedRun && (
        <Card className="run-window" role="dialog" aria-label={`${selectedRun.label} output`}>
          <div className="run-window-header">
            <div className="run-window-title">
              <RunStateIcon state={selectedRun.state} />
              <div>
                <strong>{selectedRun.label}</strong>
                <span>{selectedRun.action}</span>
              </div>
            </div>
            <div className="run-window-actions">
              <Tag
                minimal
                intent={selectedRun.state === "success" ? "success" : selectedRun.state === "error" ? "danger" : "primary"}
              >
                {selectedRun.state === "success" ? "Complete" : selectedRun.state === "error" ? "Failed" : "Running"}
              </Tag>
              {selectedRun.state === "error" && (
                <Button minimal small onClick={() => dismissRun(selectedRun.id)}>Dismiss</Button>
              )}
              <Button
                icon="cross"
                minimal
                small
                aria-label="Close output window"
                onClick={() => setSelectedRunId(null)}
              />
            </div>
          </div>
          <div className="run-window-log" ref={runPane} aria-live="polite">
            {selectedRun.output.length === 0 && <span className="log-empty">Waiting for output…</span>}
            {selectedRun.output.map((line) => (
              <span key={line.key} className={line.kind}>{line.text}</span>
            ))}
          </div>
        </Card>
      )}

      <footer className="command-shelf" aria-label="Command shelf">
        <div className="shelf-label">
          <Icon icon="console" />
          <span>Commands</span>
        </div>
        <div className="shelf-runs">
          {runs.length === 0 && <span className="shelf-empty">No active commands</span>}
          {runs.map((run) => (
            <button
              key={run.id}
              type="button"
              className={`shelf-run ${run.state} ${selectedRunId === run.id ? "selected" : ""}`}
              onClick={() => setSelectedRunId(run.id)}
            >
              <RunStateIcon state={run.state} />
              <span>{run.label}</span>
              <small>{run.state === "success" ? "Complete" : run.state === "error" ? "Failed" : "Running"}</small>
            </button>
          ))}
        </div>
      </footer>
    </div>
  );
}

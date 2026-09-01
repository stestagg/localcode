export type Config = {
  ws: string;
  gitea: string;
  secret: string;
};

export type Status = {
  project: {
    name: string;
    path: string;
    id: string;
    head: string;
    branches: string[];
    readme: string | null;
    readmeUrl: string;
  };
  gitea: { url: string; repo: string; user: string; password: string };
  hub: { container: string; running: boolean };
  personas: PromptDefinition[];
  personaIssues: DefinitionIssue[];
  roles: PromptDefinition[];
  roleIssues: DefinitionIssue[];
  runners: string[];
  processes: ProcessDefinition[];
};

export type ProcessDefinition = {
  name: string;
  title: string;
  description: string;
  requiresPersona: boolean;
  requiresStory: boolean;
  interactive: boolean;
};

export type PromptDefinition = {
  name: string;
  prompt: string;
  promptPreview: string;
  fileUrl: string;
  editUrl: string;
};

export type DefinitionIssue = {
  name: string;
  message: string;
  fileUrl: string;
  editUrl: string;
};

export type Pull = {
  number: number;
  title: string;
  branch: string;
  url: string;
};

export type StoryState = "backlog" | "ready" | "in_progress" | "done" | "cancelled";

export type Story = {
  number: number;
  title: string;
  date: string;
  prId: string;
  path: string;
  fileUrl: string;
};

export type Stories = Partial<Record<StoryState, Story[]>>;

/**
 * Where a session is. Four ways of being over, kept apart on purpose:
 * "Complete" is a claim, and only a run that was watched to the end earns it.
 */
export type SessionState =
  | "running"
  | "paused"
  | "stopped"
  | "finished"
  | "failed"
  | "abandoned";

export type SessionMessageKind = "text" | "image" | "status";

export type SessionMessage = {
  seq: number;
  /** Unix seconds, as python wrote it. */
  at: number;
  agent: string;
  kind: SessionMessageKind;
  text?: string;
  mime?: string;
  /** Raw bytes: MessagePack carries them, so an image needs no encoding. */
  data?: Uint8Array;
};

/** One session as this tab has it: what it is, and everything said in it. */
export type SessionData = {
  id: string;
  title: string;
  agent: string;
  process: string;
  state: SessionState;
  messages: SessionMessage[];
};

/** A session in the list, without its messages. */
export type SessionSummary = {
  session: string;
  title: string;
  agent: string;
  process: string;
  state: SessionState;
  at: number;
  messages: number;
};

export type SessionControl = "pause" | "resume" | "stop";

export type ServerEvent = {
  type: string;
  id?: string;
  action?: string;
  argv?: string[];
  data?: string;
  message?: string;
  code?: number;
  session?: string;
  title?: string;
  agent?: string;
  process?: string;
  state?: SessionState;
  messages?: SessionMessage[];
  sessions?: SessionSummary[];
  /** One posted message. Not `message`, which `error` already uses for text. */
  post?: SessionMessage;
} & Partial<Status> & { pulls?: Pull[]; states?: Stories };

export type LogLine = {
  key: number;
  kind: "out" | "err" | "meta";
  text: string;
};

export type CommandRunState = "running" | "success" | "error";

export type CommandRun = {
  id: string;
  action: string;
  label: string;
  state: CommandRunState;
  completedAt?: number;
  argv?: string[];
  output: LogLine[];
};

export type Connection = "connecting" | "ready" | "closed" | "denied";

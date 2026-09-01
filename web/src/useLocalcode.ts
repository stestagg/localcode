import { decode, encode } from "@msgpack/msgpack";
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CommandRun,
  Config,
  Connection,
  LogLine,
  Pull,
  ServerEvent,
  SessionControl,
  SessionData,
  SessionMessage,
  SessionSummary,
  Stories,
  StoryState,
  Status,
} from "./types";

const LOG_LIMIT = 2000;
/** A long conversation is still bounded; the record on disk keeps all of it. */
const MESSAGE_LIMIT = 1000;

function sendMessage(ws: WebSocket | null, message: object) {
  ws?.send(encode(message));
}

function nextLine(lines: LogLine[], kind: LogLine["kind"], text: string): LogLine[] {
  const key = (lines.at(-1)?.key ?? -1) + 1;
  const next = [...lines, { key, kind, text }];
  return next.length > LOG_LIMIT ? next.slice(-LOG_LIMIT) : next;
}

function defaultRun(id: string, action: string): CommandRun {
  return {
    id,
    action,
    label: action.replaceAll(".", " "),
    state: "running",
    output: [],
  };
}

function defaultSession(id: string): SessionData {
  return { id, title: "", agent: "", process: "", state: "running", messages: [] };
}

/**
 * `messages` with `post` in it: appended if it is new, replaced where it sits
 * if it is not.
 *
 * A streaming answer arrives many times under one seq, growing each time, so
 * replacing in place is what makes it appear rather than repeat.
 */
function withMessage(messages: SessionMessage[], post: SessionMessage): SessionMessage[] {
  const index = messages.findIndex((message) => message.seq === post.seq);
  if (index >= 0) return messages.map((item, at) => (at === index ? post : item));
  const next = [...messages, post];
  return next.length > MESSAGE_LIMIT ? next.slice(-MESSAGE_LIMIT) : next;
}

function runnerFrom(argv: string[] | undefined): string | undefined {
  const value = argv?.find((part) => part.startsWith("LOCALCODE_RUNNER="));
  return value?.slice("LOCALCODE_RUNNER=".length);
}

/** The client-side protocol and live state for every command instance. */
export function useLocalcode() {
  const socket = useRef<WebSocket | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [status, setStatus] = useState<Status | null>(null);
  const [pulls, setPulls] = useState<Pull[]>([]);
  const [stories, setStories] = useState<Stories>({});
  const [log, setLog] = useState<LogLine[]>([]);
  const [runs, setRuns] = useState<CommandRun[]>([]);
  const [sessions, setSessions] = useState<Record<string, SessionData>>({});
  const [sessionList, setSessionList] = useState<SessionSummary[]>([]);
  const loadedStoryStates = useRef(new Set<StoryState>());

  const append = useCallback((kind: LogLine["kind"], text: string) => {
    setLog((lines) => nextLine(lines, kind, text));
  }, []);

  const updateRun = useCallback(
    (event: ServerEvent, update: (run: CommandRun) => CommandRun) => {
      if (!event.id || !event.action) return;
      setRuns((current) => {
        const index = current.findIndex((run) => run.id === event.id);
        const run = index < 0 ? defaultRun(event.id!, event.action!) : current[index];
        const updated = update(run);
        if (index < 0) return [...current, updated];
        return current.map((item, itemIndex) => (itemIndex === index ? updated : item));
      });
    },
    [],
  );

  const send = useCallback((action: string, data: object = {}) => {
    sendMessage(socket.current, { action, ...data });
  }, []);

  const loadStories = useCallback((states: StoryState[]) => {
    states.forEach((state) => loadedStoryStates.current.add(state));
    sendMessage(socket.current, { action: "stories.list", states });
  }, []);

  const launch = useCallback((action: string, label: string, data: object = {}) => {
    const id = crypto.randomUUID().replaceAll("-", "").slice(0, 12);
    setRuns((current) => [...current, { ...defaultRun(id, action), label }]);
    sendMessage(socket.current, { id, action, ...data });
    return id;
  }, []);

  const dismissRun = useCallback((id: string) => {
    setRuns((current) => current.filter((run) => run.id !== id));
  }, []);

  const updateSession = useCallback(
    (id: string, update: (session: SessionData) => SessionData) => {
      setSessions((current) => ({
        ...current,
        [id]: update(current[id] ?? defaultSession(id)),
      }));
    },
    [],
  );

  const listSessions = useCallback((limit = 20) => {
    sendMessage(socket.current, { action: "session.list", limit });
  }, []);

  const subscribeSession = useCallback((session: string) => {
    sendMessage(socket.current, { action: "session.subscribe", session });
  }, []);

  const sessionInput = useCallback((session: string, text: string) => {
    sendMessage(socket.current, { action: "session.input", session, text });
  }, []);

  const sessionControl = useCallback((session: string, control: SessionControl) => {
    sendMessage(socket.current, { action: "session.control", session, control });
  }, []);

  /**
   * Ask a question, and get the session it will be answered in.
   *
   * The id is minted here rather than waited for, which is what makes the
   * viewer renderable before the server has said anything: the server honours
   * the offered id, and anything posted before the subscribe lands is replayed
   * by the history it sends back.
   */
  const ask = useCallback(
    (persona: string, prompt: string) => {
      const session = crypto.randomUUID().replaceAll("-", "").slice(0, 12);
      setSessions((current) => ({
        ...current,
        [session]: {
          ...defaultSession(session),
          title: `ask ${persona}`,
          agent: persona,
          process: "ask",
        },
      }));
      launch("ask", `ask ${persona}`, { session, persona, prompt });
      return session;
    },
    [launch],
  );

  const startProcess = useCallback(
    (process: string, persona: string) => {
      const session = crypto.randomUUID().replaceAll("-", "").slice(0, 12);
      setSessions((current) => ({
        ...current,
        [session]: {
          ...defaultSession(session),
          title: `${process} ${persona}`,
          agent: persona,
          process,
        },
      }));
      launch("process.start", `${process} ${persona}`, { session, process, persona });
      return session;
    },
    [launch],
  );

  useEffect(() => {
    let live = true;
    let ws: WebSocket | null = null;

    (async () => {
      const config: Config = await (await fetch("/config.json")).json();
      if (!live) return;

      ws = new WebSocket(location.origin.replace(/^http/, "ws") + config.ws);
      ws.binaryType = "arraybuffer";
      socket.current = ws;

      ws.onopen = () => sendMessage(ws, { action: "auth", secret: config.secret });
      ws.onclose = () => {
        setConnection((was) => (was === "denied" ? was : "closed"));
        setRuns((current) =>
          current.map((run) =>
            run.state === "running"
              ? { ...run, state: "error", output: nextLine(run.output, "err", "connection closed\n") }
              : run,
          ),
        );
      };

      ws.onmessage = (raw) => {
        const event = decode(new Uint8Array(raw.data as ArrayBuffer)) as ServerEvent;
        switch (event.type) {
          case "ready":
            setConnection("ready");
            sendMessage(ws, { action: "status" });
            sendMessage(ws, { action: "gitea.pulls" });
            sendMessage(ws, { action: "session.list", limit: 20 });
            break;
          case "status":
            setStatus(event as unknown as Status);
            break;
          case "pulls":
            setPulls(event.pulls ?? []);
            break;
          case "stories":
            setStories((current) => ({ ...current, ...(event.states ?? {}) }));
            break;
          case "sessions":
            setSessionList(event.sessions ?? []);
            break;
          case "session.history": {
            const id = event.session;
            if (!id) break;
            setSessions((current) => ({
              ...current,
              [id]: {
                id,
                title: event.title ?? current[id]?.title ?? "",
                agent: event.agent ?? current[id]?.agent ?? "",
                process: event.process ?? current[id]?.process ?? "ask",
                state: event.state ?? "running",
                messages: (event.messages ?? []).slice(-MESSAGE_LIMIT),
              },
            }));
            break;
          }
          case "session.message": {
            const id = event.session;
            const post = event.post;
            if (!id || !post) break;
            updateSession(id, (session) => ({
              ...session,
              messages: withMessage(session.messages, post),
            }));
            break;
          }
          case "session.state":
          case "session.closed": {
            const id = event.session;
            if (!id || !event.state) break;
            updateSession(id, (session) => ({ ...session, state: event.state! }));
            break;
          }
          case "agents":
          case "personas":
            sendMessage(ws, { action: "status" });
            break;
          case "metadata.changed":
            sendMessage(ws, { action: "status" });
            if (loadedStoryStates.current.size > 0) {
              sendMessage(ws, {
                action: "stories.list",
                states: [...loadedStoryStates.current],
              });
            }
            if (event.message) append("err", `metadata sync: ${event.message}\n`);
            break;
          case "start": {
            const text = `$ ${event.argv?.join(" ")}\n`;
            append("meta", text);
            updateRun(event, (run) => ({
              ...run,
              label: runnerFrom(event.argv) ?? run.label,
              state: "running",
              completedAt: undefined,
              argv: event.argv,
              output: nextLine(run.output, "meta", text),
            }));
            break;
          }
          case "stdout":
            append("out", event.data ?? "");
            updateRun(event, (run) => ({
              ...run,
              output: nextLine(run.output, "out", event.data ?? ""),
            }));
            break;
          case "stderr":
            append("err", event.data ?? "");
            updateRun(event, (run) => ({
              ...run,
              output: nextLine(run.output, "err", event.data ?? ""),
            }));
            break;
          case "exit": {
            const failed = (event.code ?? 1) !== 0;
            const text = `exit ${event.code}\n`;
            append("meta", `${text}\n`);
            updateRun(event, (run) => ({
              ...run,
              state: failed ? "error" : "success",
              completedAt: Date.now(),
              output: nextLine(run.output, failed ? "err" : "meta", text),
            }));
            sendMessage(ws, { action: "status" });
            sendMessage(ws, { action: "gitea.pulls" });
            sendMessage(ws, { action: "session.list", limit: 20 });
            break;
          }
          case "error": {
            const text = `error: ${event.message}\n`;
            if (event.message === "unauthorised") setConnection("denied");
            append("err", text);
            updateRun(event, (run) => ({
              ...run,
              state: "error",
              completedAt: undefined,
              output: nextLine(run.output, "err", text),
            }));
            break;
          }
        }
      };
    })();

    return () => {
      live = false;
      ws?.close();
    };
  }, [append, updateRun, updateSession]);

  const busy = runs.some((run) => run.state === "running");
  return {
    connection,
    status,
    pulls,
    stories,
    log,
    runs,
    sessions,
    sessionList,
    busy,
    send,
    loadStories,
    launch,
    dismissRun,
    ask,
    startProcess,
    listSessions,
    subscribeSession,
    sessionInput,
    sessionControl,
  };
}

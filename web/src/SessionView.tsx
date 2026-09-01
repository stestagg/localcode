import { Button, ButtonGroup, Card, InputGroup, Spinner, Tag } from "@blueprintjs/core";
import { useEffect, useMemo, useRef, useState } from "react";
import type { SessionControl, SessionData, SessionMessage, SessionState } from "./types";

/**
 * One session's message stream, live.
 *
 * Deliberately not the run window: that shows what a container emitted, and
 * this shows what an agent chose to say. The two are different records and
 * both are worth having, so neither is folded into the other.
 *
 * The three controls are props rather than always-on because not every flow
 * has anything to do with them. A one-shot question can be stopped but not
 * steered, so it renders with `stop` alone and gets no dead input field; a
 * multi-turn agent turns all three on. Off means absent, not disabled -- a
 * greyed-out control still says "this should work here", and it should not.
 */

const STATE_LABEL: Record<SessionState, string> = {
  running: "Running",
  paused: "Paused",
  stopped: "Stopped",
  finished: "Complete",
  failed: "Failed",
  // Not "Complete": the run was still going when whatever was watching it went
  // away, so how it ended is genuinely unknown.
  abandoned: "Interrupted",
};

const STATE_INTENT: Record<SessionState, "none" | "primary" | "warning" | "danger" | "success"> = {
  running: "primary",
  paused: "warning",
  stopped: "none",
  finished: "success",
  failed: "danger",
  abandoned: "warning",
};

function when(at: number): string {
  return new Date(at * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * An object url for an image message, released when it is no longer shown.
 *
 * The bytes came over MessagePack as a Uint8Array, so there is nothing to
 * decode -- but a url made from them has to be revoked, or every redraw of a
 * growing conversation leaks one.
 */
function ImageBody({ message }: { message: SessionMessage }) {
  const [source, setSource] = useState<string>();

  useEffect(() => {
    if (!message.data) return;
    const url = URL.createObjectURL(
      new Blob([message.data as BlobPart], { type: message.mime || "image/png" }),
    );
    setSource(url);
    return () => URL.revokeObjectURL(url);
  }, [message.data, message.mime]);

  if (!source) return <span className="session-empty">No image data.</span>;
  return <img className="session-image" src={source} alt={`from ${message.agent}`} />;
}

function MessageRow({ message }: { message: SessionMessage }) {
  return (
    <li className={`session-message session-${message.kind}`}>
      <div className="session-message-heading">
        <strong>{message.agent}</strong>
        <time dateTime={new Date(message.at * 1000).toISOString()}>{when(message.at)}</time>
      </div>
      {message.kind === "image" ? (
        <ImageBody message={message} />
      ) : (
        <p className="session-message-body">{message.text}</p>
      )}
    </li>
  );
}

export function SessionView({
  session,
  textInput = false,
  pauseResume = false,
  stop = false,
  onInput,
  onControl,
}: {
  session: SessionData | undefined;
  textInput?: boolean;
  pauseResume?: boolean;
  stop?: boolean;
  onInput?: (text: string) => void;
  onControl?: (control: SessionControl) => void;
}) {
  const [draft, setDraft] = useState("");
  const stream = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => session?.messages ?? [], [session?.messages]);
  const state = session?.state ?? "running";
  const live = state === "running" || state === "paused";
  const controls = pauseResume || stop || textInput;

  useEffect(() => {
    const node = stream.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  if (!session) return null;

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    onInput?.(text);
    setDraft("");
  };

  return (
    <Card className="session-view">
      <div className="session-heading">
        {state === "running" ? <Spinner size={14} /> : null}
        <strong>{session.title || session.id}</strong>
        <Tag minimal intent={STATE_INTENT[state]} round>
          {STATE_LABEL[state]}
        </Tag>
      </div>

      <div className="session-stream" ref={stream} aria-live="polite">
        {messages.length === 0 ? (
          <div className="session-empty">Nothing said yet.</div>
        ) : (
          <ul>
            {messages.map((message) => (
              <MessageRow key={message.seq} message={message} />
            ))}
          </ul>
        )}
      </div>

      {controls && (
        <div className="session-controls">
          {textInput && (
            <InputGroup
              className="session-input"
              value={draft}
              disabled={!live}
              placeholder="Say something to the agent…"
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submit();
              }}
              rightElement={
                <Button minimal icon="send-message" disabled={!live || !draft.trim()} onClick={submit} />
              }
            />
          )}
          {(pauseResume || stop) && (
            <ButtonGroup>
              {pauseResume && (
                <Button
                  icon={state === "paused" ? "play" : "pause"}
                  disabled={!live}
                  onClick={() => onControl?.(state === "paused" ? "resume" : "pause")}
                >
                  {state === "paused" ? "Resume" : "Pause"}
                </Button>
              )}
              {stop && (
                <Button icon="stop" intent="danger" disabled={!live} onClick={() => onControl?.("stop")}>
                  Stop
                </Button>
              )}
            </ButtonGroup>
          )}
        </div>
      )}
    </Card>
  );
}

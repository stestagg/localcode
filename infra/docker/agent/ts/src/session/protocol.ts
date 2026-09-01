/**
 * The session wire format, as the controller speaks it.
 *
 * Agreed with `src/localcode/server/sessions.py` and `server/ws.py`: binary
 * MessagePack frames, an opening `auth` frame carrying the runtime secret, then
 * `{action}` messages in and `{type}` events out. Nothing here knows what a
 * session is for -- that is `client.ts`.
 */

import { decode as unpack, encode as pack } from "@msgpack/msgpack";

/** Where a session can be. Mirrors the constants in `sessions.py`. */
export type SessionState =
  | "running"
  | "paused"
  | "stopped"
  | "finished"
  | "failed"
  | "abandoned";

/** What a message carries. `image` is here because MessagePack has bytes. */
export type MessageKind = "text" | "image" | "status";

export type Message = {
  seq: number;
  at: number;
  agent: string;
  kind: MessageKind;
  text?: string;
  mime?: string;
  data?: Uint8Array;
};

/** Actions this side sends. */
export const AUTH = "auth";
export const ATTACH = "session.attach";
export const POST = "session.post";
export const COLLECT = "session.collect";
export const CLOSE = "session.close";

/** Events this side receives. */
export const READY = "ready";
export const STATE = "session.state";
export const INPUT = "session.input";
export const POSTED = "session.posted";
export const COLLECTED = "session.collected";
export const ERROR = "error";

export type ServerEvent = {
  type: string;
  session?: string;
  state?: SessionState;
  input?: Message;
  inputs?: Message[];
  text?: string;
  at?: number;
  seq?: number;
  message?: string;
};

/**
 * The environment the controller sets on a container it wants reporting in.
 * Agreed with `SESSION_ENV` and friends in `src/localcode/llm/console.py`.
 */
export const SESSION_ENV = "LOCALCODE_SESSION";
export const SESSION_URL_ENV = "LOCALCODE_SESSION_URL";
export const SECRET_ENV = "LOCALCODE_SECRET";
export const AGENT_ENV = "LOCALCODE_AGENT";

export function encode(message: object): Uint8Array {
  return pack(message);
}

export function decode(frame: ArrayBuffer | Uint8Array): ServerEvent {
  const bytes = frame instanceof Uint8Array ? frame : new Uint8Array(frame);
  const value = unpack(bytes);
  if (typeof value !== "object" || value === null) {
    throw new TypeError("websocket messages must contain a MessagePack map");
  }
  return value as ServerEvent;
}

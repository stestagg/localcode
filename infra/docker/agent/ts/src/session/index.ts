/**
 * The agent side of a session: how code in this image says what it is doing,
 * and finds out what the person watching wants.
 *
 * The whole of it, for most callers, is:
 *
 *     await session(async (client) => {
 *       await client.say("working on it");
 *     });
 */

export {
  type SessionOptions,
  SessionClient,
  SessionError,
  STOP,
  StopRequested,
  configured,
  connect,
  session,
} from "./client.ts";
export {
  type Message,
  type MessageKind,
  type ServerEvent,
  type SessionState,
  AGENT_ENV,
  SECRET_ENV,
  SESSION_ENV,
  SESSION_URL_ENV,
  decode,
  encode,
} from "./protocol.ts";

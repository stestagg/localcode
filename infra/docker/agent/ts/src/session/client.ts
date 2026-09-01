/**
 * Talking to a session from inside an agent container.
 *
 * The point of this file is that agent code should not have to think about the
 * socket. It says things with `say`, and it looks for instructions with
 * `hasUserInput`. Pausing is not visible at all -- the checkpoint simply takes
 * longer to return -- and stopping arrives either as a value to check or as a
 * handler that throws, whichever the caller prefers:
 *
 *     await session(async (client) => {
 *       for (const step of plan) {
 *         const input = await client.hasUserInput();
 *         if (input === STOP) return;
 *         if (input) plan.revise(input);
 *         await client.say(await run(step));
 *       }
 *     });
 *
 * Cooperative throughout: nothing here interrupts work in progress, because
 * only the code doing the work knows where it is safe to stop. `signal` is the
 * one concession -- an `AbortSignal` for handing to something long-running
 * like `streamText`, so a stop can cut an answer short mid-sentence.
 */

import {
  AGENT_ENV,
  ATTACH,
  AUTH,
  CLOSE,
  COLLECT,
  COLLECTED,
  ERROR,
  INPUT,
  type Message,
  type MessageKind,
  POST,
  READY,
  SECRET_ENV,
  SESSION_ENV,
  SESSION_URL_ENV,
  type ServerEvent,
  type SessionState,
  STATE,
  decode,
  encode,
} from "./protocol.ts";

/** What `hasUserInput` returns when the session has been stopped. */
export const STOP = Symbol.for("localcode.stop");

/** The default way to abandon a run: `onStop` throws this. */
export class StopRequested extends Error {
  override readonly name = "StopRequested";

  constructor(message = "the session was stopped") {
    super(message);
  }
}

/** A failure this side can explain. */
export class SessionError extends Error {
  override readonly name = "SessionError";
}

export type SessionOptions = {
  /** Defaults to `LOCALCODE_SESSION_URL`. */
  url?: string;
  /** Defaults to `LOCALCODE_SESSION`. */
  session?: string;
  /** Defaults to `LOCALCODE_SECRET`. */
  secret?: string;
  /** What messages are attributed to. Defaults to `LOCALCODE_AGENT`. */
  agent?: string;
  /**
   * Run at the first checkpoint after a stop arrives. Throw from here to
   * abandon the run that way rather than by checking for `STOP`.
   */
  onStop?: (client: SessionClient) => void | Promise<void>;
};

/** Where an answer goes, a chunk at a time. Matches `llm/stream.ts`. */
type Sink = (chunk: string) => void | Promise<void>;

/** True when this container was started to report into a session. */
export function configured(options: SessionOptions = {}): boolean {
  return Boolean(options.session ?? process.env[SESSION_ENV]);
}

export class SessionClient {
  readonly session: string;
  readonly agent: string;

  #socket: WebSocket;
  #options: SessionOptions;
  #state: SessionState = "running";
  #inputs: Message[] = [];
  #seenInputs = new Set<number>();
  #aborter = new AbortController();
  /** Input and state waiters, all released by the next relevant event. */
  #waiters = new Set<() => void>();
  /** `refresh` calls waiting for the controller's collected response. */
  #collections: (() => void)[] = [];
  #stopHandled = false;
  #closed = false;
  #disconnected = false;
  /** Messages still streaming, by the stream id this client gave them. */
  #open = new Map<string, () => Promise<void>>();
  #streams = 0;

  constructor(socket: WebSocket, session: string, agent: string, options: SessionOptions) {
    this.#socket = socket;
    this.session = session;
    this.agent = agent;
    this.#options = options;
    socket.addEventListener("message", (event) => this.#receive(event));
    socket.addEventListener("close", () => this.#disconnect());
  }

  get state(): SessionState {
    return this.#state;
  }

  get stopped(): boolean {
    return this.#state === "stopped";
  }

  /** For handing to something long-running. Aborts the moment a stop lands. */
  get signal(): AbortSignal {
    return this.#aborter.signal;
  }

  // --- saying things -------------------------------------------------------

  async say(text: string): Promise<void> {
    await this.#post({ kind: "text", text, done: true });
  }

  async status(text: string): Promise<void> {
    await this.#post({ kind: "status", text, done: true });
  }

  async image(data: Uint8Array, mime: string): Promise<void> {
    await this.#post({ kind: "image", mime, data, done: true });
  }

  /**
   * A sink for `streamTo`, so an answer appears as it is produced.
   *
   * Every chunk reaches the browser; the record gets one message, because each
   * post after the first carries the same `seq` and only `end()` marks it done.
   * A sink that is never ended is finished off by `close`.
   */
  sink(): Sink & { end: () => Promise<void> } {
    // The controller keeps assigning seq; this only has to be unique among
    // the messages this client has open at once.
    const stream = `${this.agent}-${this.#streams++}`;
    let started = false;

    const write = async (chunk: string) => {
      if (!chunk) return;
      await this.#post({ kind: "text", text: chunk, stream, done: false });
      started = true;
    };
    write.end = async () => {
      this.#open.delete(stream);
      if (!started) return;
      await this.#post({ kind: "text", text: "", stream, done: true });
    };
    this.#open.set(stream, write.end);
    return write;
  }

  // --- looking for instructions --------------------------------------------

  /** Block while paused, then report whether execution should stop. */
  async checkpoint(): Promise<typeof STOP | undefined> {
    while (this.#state === "paused" && !this.#disconnected) {
      await this.#waitForChange();
    }

    if (this.#terminal) {
      if (!this.#stopHandled) {
        this.#stopHandled = true;
        await this.#options.onStop?.(this);
      }
      return STOP;
    }
    return undefined;
  }

  /** The next complete input message, without waiting for one to arrive. */
  async takeInput(): Promise<Message | typeof STOP | undefined> {
    if ((await this.checkpoint()) === STOP) return STOP;
    return this.#inputs.shift();
  }

  /**
   * Backwards-compatible text checkpoint for simple scripts.
   * Multimodal workflows should use `takeInput` or `waitForInput` instead.
   */
  async hasUserInput(): Promise<string | typeof STOP | undefined> {
    const input = await this.takeInput();
    if (input === STOP || input === undefined) return input;
    return input.text;
  }

  /** Wait until one input is available, or until the session ends. */
  async waitForInput(): Promise<Message | typeof STOP> {
    while (true) {
      const input = await this.takeInput();
      if (input !== undefined) return input;
      await this.#waitForChange();
    }
  }

  /** Remove and return every input currently queued on this client. */
  drainInputs(): Message[] {
    return this.#inputs.splice(0);
  }

  get #terminal(): boolean {
    return (
      this.#disconnected ||
      this.#state === "stopped" ||
      this.#state === "finished" ||
      this.#state === "failed" ||
      this.#state === "abandoned"
    );
  }

  /**
   * Ask the controller outright, rather than trusting what has been pushed.
   * Only worth it after a reconnect, when events may have been missed.
   */
  async refresh(): Promise<void> {
    if (this.#disconnected) return;
    const collected = new Promise<void>((resolve) => this.#collections.push(resolve));
    this.#send({ action: COLLECT, session: this.session });
    await collected;
  }

  // --- ending --------------------------------------------------------------

  async close(code = 0): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    for (const end of [...this.#open.values()]) await end();
    this.#send({ action: CLOSE, session: this.session, code });
    // The close frame is queued behind that one; letting the loop turn once is
    // what gets it onto the wire before the socket is torn down.
    await new Promise((resolve) => setTimeout(resolve, 0));
    this.#socket.close();
  }

  // --- the socket ----------------------------------------------------------

  #post(body: {
    kind: MessageKind;
    text?: string;
    mime?: string;
    data?: Uint8Array;
    stream?: string;
    done: boolean;
  }): void {
    this.#send({ action: POST, session: this.session, agent: this.agent, ...body });
  }

  #send(message: object): void {
    if (this.#socket.readyState !== WebSocket.OPEN) return;
    this.#socket.send(encode(message));
  }

  #receive(event: MessageEvent): void {
    let payload: ServerEvent;
    try {
      payload = decode(event.data as ArrayBuffer);
    } catch {
      return; // not ours to make sense of
    }
    if (payload.session !== undefined && payload.session !== this.session) return;

    switch (payload.type) {
      case STATE:
        this.#settle(payload.state ?? this.#state);
        break;
      case INPUT:
        if (payload.input) this.#queueInput(payload.input);
        break;
      case COLLECTED:
        this.#settle(payload.state ?? this.#state);
        for (const input of payload.inputs ?? []) this.#queueInput(input);
        this.#collections.shift()?.();
        break;
      case ERROR:
        // Reported rather than thrown: it arrives on the socket, not at a
        // call site, and there is nowhere here to throw it that anyone
        // is waiting.
        console.error(`session: ${payload.message}`);
        break;
    }
  }

  /**
   * The controller is gone.
   *
   * Treated as a stop rather than as a pause to sit out: there is nobody left
   * to resume it, nowhere left to report, and a checkpoint that never returns
   * is a container that hangs until something kills it -- which from the
   * outside looks exactly like work in progress.
   */
  #disconnect(): void {
    this.#disconnected = true;
    this.#aborter.abort(new StopRequested("the controller went away"));
    for (const resolve of this.#collections.splice(0)) resolve();
    this.#wake();
  }

  #settle(state: SessionState): void {
    this.#state = state;
    if (state !== "running" && state !== "paused") {
      this.#aborter.abort(new StopRequested());
    }
    if (state !== "paused") this.#wake();
  }

  #queueInput(input: Message): void {
    if (!Number.isInteger(input.seq) || this.#seenInputs.has(input.seq)) return;
    this.#seenInputs.add(input.seq);
    this.#inputs.push(input);
    this.#wake();
  }

  #waitForChange(): Promise<void> {
    return new Promise<void>((resolve) => this.#waiters.add(resolve));
  }

  #wake(): void {
    for (const resolve of this.#waiters) resolve();
    this.#waiters.clear();
  }
}

/** Open a socket, authenticate, and attach to the session. */
export async function connect(options: SessionOptions = {}): Promise<SessionClient> {
  const url = options.url ?? process.env[SESSION_URL_ENV];
  const id = options.session ?? process.env[SESSION_ENV];
  const secret = options.secret ?? process.env[SECRET_ENV];
  const agent = options.agent ?? process.env[AGENT_ENV] ?? "agent";

  if (!url) throw new SessionError(`no ${SESSION_URL_ENV} in this container`);
  if (!id) throw new SessionError(`no ${SESSION_ENV} in this container`);
  if (!secret) throw new SessionError(`no ${SECRET_ENV} in this container`);

  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";

  await new Promise<void>((resolve, reject) => {
    socket.addEventListener("error", () =>
      reject(new SessionError(`could not reach the controller at ${url}`)),
    );
    socket.addEventListener("close", () =>
      reject(new SessionError(`the controller closed the connection`)),
    );
    // Authenticate, then wait to be told the socket is usable. The controller
    // hangs up rather than answering when the secret is wrong, which the close
    // listener above turns into something worth reading.
    socket.addEventListener("open", () => socket.send(encode({ action: AUTH, secret })));
    socket.addEventListener("message", function ready(event) {
      try {
        if (decode(event.data as ArrayBuffer).type !== READY) return;
      } catch {
        return;
      }
      socket.removeEventListener("message", ready);
      resolve();
    });
  });

  const client = new SessionClient(socket, id, agent, options);
  socket.send(encode({ action: ATTACH, session: id }));
  await client.refresh();
  return client;
}

/**
 * Connect, run, close -- the wrapper almost every script wants.
 *
 * The session is closed however the body ends, so a script that throws still
 * leaves a session that says so rather than one that looks like it is thinking.
 */
export async function session<T>(
  run: (client: SessionClient) => Promise<T>,
  options: SessionOptions = {},
): Promise<T> {
  const client = await connect(options);
  let code = 0;
  try {
    return await run(client);
  } catch (error) {
    code = 1;
    throw error;
  } finally {
    await client.close(code);
  }
}

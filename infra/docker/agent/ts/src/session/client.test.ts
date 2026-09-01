/**
 * The client library, against a socket that is not one.
 *
 * What matters here is the part agent code relies on and cannot see: that a
 * checkpoint blocks for exactly as long as a pause lasts, that a stop is
 * unmissable however the caller chose to notice it, and that a streamed answer
 * costs the record one message rather than one per chunk.
 */

import { describe, expect, mock, test } from "bun:test";

import { SessionClient, STOP, StopRequested } from "./client.ts";
import { decode, encode } from "./protocol.ts";

/** Just enough WebSocket for the client: what it sends, and what it is told. */
class FakeSocket {
  readyState = 1; // WebSocket.OPEN
  sent: Record<string, unknown>[] = [];
  closed = false;
  #listeners = new Map<string, ((event: unknown) => void)[]>();

  addEventListener(name: string, handler: (event: unknown) => void): void {
    this.#listeners.set(name, [...(this.#listeners.get(name) ?? []), handler]);
  }

  removeEventListener(name: string, handler: (event: unknown) => void): void {
    this.#listeners.set(
      name,
      (this.#listeners.get(name) ?? []).filter((item) => item !== handler),
    );
  }

  send(frame: Uint8Array): void {
    this.sent.push(decode(frame) as unknown as Record<string, unknown>);
  }

  close(): void {
    this.closed = true;
  }

  /** What the controller would push down the socket. */
  deliver(event: object): void {
    const bytes = encode(event);
    // Exactly the encoded bytes. `encode` returns a view into a larger buffer,
    // and a real socket delivers the frame and nothing after it.
    this.frame(
      (bytes.buffer as ArrayBuffer).slice(
        bytes.byteOffset,
        bytes.byteOffset + bytes.byteLength,
      ),
    );
  }

  /** Anything at all, for the frames that are not events. */
  frame(data: ArrayBuffer): void {
    for (const handler of this.#listeners.get("message") ?? []) handler({ data });
  }

  hangUp(): void {
    for (const handler of this.#listeners.get("close") ?? []) handler({});
  }

  posts(): Record<string, unknown>[] {
    return this.sent.filter((message) => message.action === "session.post");
  }
}

function client(options = {}): [SessionClient, FakeSocket] {
  const socket = new FakeSocket();
  return [
    new SessionClient(socket as unknown as WebSocket, "s-1", "developer", options),
    socket,
  ];
}

/** Let anything already queued on the microtask queue run. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const input = (seq: number, text: string) => ({
  seq,
  at: 1,
  agent: "you",
  kind: "text" as const,
  text,
});

describe("saying things", () => {
  test("a message is attributed and finished in one post", async () => {
    const [session, socket] = client();
    await session.say("done");

    expect(socket.posts()).toEqual([
      {
        action: "session.post",
        session: "s-1",
        agent: "developer",
        kind: "text",
        text: "done",
        done: true,
      },
    ]);
  });

  test("a streamed answer is many posts but one message", async () => {
    const [session, socket] = client();
    const sink = session.sink();
    await sink("Three ");
    await sink("things");
    await sink.end();

    const posts = socket.posts();
    // Every chunk goes, so the browser sees it appear...
    expect(posts.map((post) => post.text)).toEqual(["Three ", "things", ""]);
    // ...under one stream id, which is what makes it one line in the record.
    expect(new Set(posts.map((post) => post.stream)).size).toBe(1);
    expect(posts.map((post) => post.done)).toEqual([false, false, true]);
  });

  test("a sink nothing was written to does not post at all", async () => {
    const [session, socket] = client();
    await session.sink().end();
    expect(socket.posts()).toEqual([]);
  });

  test("closing finishes anything still streaming", async () => {
    const [session, socket] = client();
    const sink = session.sink();
    await sink("cut short");
    await session.close();

    // The partial answer is closed rather than left open, so what the model did
    // say is written instead of dropped.
    expect(socket.posts().at(-1)).toMatchObject({ done: true });
    expect(socket.sent.at(-1)).toMatchObject({ action: "session.close", code: 0 });
    expect(socket.closed).toBe(true);
  });

  test("a failed worker reports its nonzero outcome", async () => {
    const [session, socket] = client();
    await session.close(2);
    expect(socket.sent.at(-1)).toMatchObject({ action: "session.close", code: 2 });
  });
});

describe("the checkpoint", () => {
  test("nothing to report reads as carry on", async () => {
    const [session] = client();
    expect(await session.hasUserInput()).toBeUndefined();
  });

  test("input is returned once, in the order it was typed", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.input", session: "s-1", input: input(1, "first") });
    socket.deliver({ type: "session.input", session: "s-1", input: input(2, "second") });

    expect(await session.hasUserInput()).toBe("first");
    expect(await session.hasUserInput()).toBe("second");
    expect(await session.hasUserInput()).toBeUndefined();
  });

  test("waitForInput blocks without polling and returns the complete message", async () => {
    const [session, socket] = client();
    let returned = false;
    const waiting = session.waitForInput().then((message) => {
      returned = true;
      return message;
    });
    await settle();
    expect(returned).toBe(false);

    socket.deliver({ type: "session.input", session: "s-1", input: input(3, "hello") });
    expect(await waiting).toEqual(input(3, "hello"));
  });

  test("push and catch-up forms of one seq are consumed only once", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.input", session: "s-1", input: input(7, "once") });
    const refreshed = session.refresh();
    expect(socket.sent.at(-1)).toMatchObject({ action: "session.collect" });
    socket.deliver({
      type: "session.collected",
      session: "s-1",
      state: "running",
      inputs: [input(7, "once")],
    });
    await refreshed;

    expect(await session.hasUserInput()).toBe("once");
    expect(await session.hasUserInput()).toBeUndefined();
  });

  test("a pause blocks until it is lifted, and nothing else has to know", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.state", session: "s-1", state: "paused" });

    let returned = false;
    const checkpoint = session.hasUserInput().then((value) => {
      returned = true;
      return value;
    });

    await settle();
    expect(returned).toBe(false);

    socket.deliver({ type: "session.state", session: "s-1", state: "running" });
    expect(await checkpoint).toBeUndefined();
  });

  test("a stop reaches a checkpoint that was parked on a pause", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.state", session: "s-1", state: "paused" });
    const checkpoint = session.hasUserInput();
    await settle();

    socket.deliver({ type: "session.state", session: "s-1", state: "stopped" });
    expect(await checkpoint).toBe(STOP);
  });

  test("a stop is STOP, and the handler runs once", async () => {
    const onStop = mock(() => {});
    const [session, socket] = client({ onStop });
    socket.deliver({ type: "session.state", session: "s-1", state: "stopped" });

    expect(await session.hasUserInput()).toBe(STOP);
    expect(await session.hasUserInput()).toBe(STOP);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  test("a handler that throws is how a caller abandons the run that way", async () => {
    const [session, socket] = client({
      onStop: () => {
        throw new StopRequested();
      },
    });
    socket.deliver({ type: "session.state", session: "s-1", state: "stopped" });

    await expect(session.hasUserInput()).rejects.toThrow(StopRequested);
  });

  test("a socket that goes away ends a paused checkpoint rather than parking it", async () => {
    // Otherwise the container sits at a checkpoint nobody is left to answer,
    // which from the outside looks exactly like work in progress.
    const [session, socket] = client();
    socket.deliver({ type: "session.state", session: "s-1", state: "paused" });
    const checkpoint = session.hasUserInput();
    await settle();

    socket.hangUp();
    expect(await checkpoint).toBe(STOP);
    expect(session.signal.aborted).toBe(true);
  });
});

describe("the abort signal", () => {
  test("it fires on a stop, so a streamed answer can be cut short", async () => {
    const [session, socket] = client();
    expect(session.signal.aborted).toBe(false);

    socket.deliver({ type: "session.state", session: "s-1", state: "stopped" });
    expect(session.signal.aborted).toBe(true);
    expect(session.stopped).toBe(true);
  });

  test("a pause is not an abort: the work in flight is finishing, not ending", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.state", session: "s-1", state: "paused" });
    expect(session.signal.aborted).toBe(false);
  });
});

describe("someone else's traffic", () => {
  test("events for another session are ignored", async () => {
    const [session, socket] = client();
    socket.deliver({ type: "session.input", session: "s-2", input: input(1, "not for us") });
    socket.deliver({ type: "session.state", session: "s-2", state: "stopped" });

    expect(await session.hasUserInput()).toBeUndefined();
    expect(session.stopped).toBe(false);
  });

  test("a frame that is not MessagePack is not a crash", async () => {
    const [session, socket] = client();
    socket.frame(new Uint8Array([0xc1, 0xc1, 0xc1]).buffer);
    socket.deliver({ type: "session.input", session: "s-1", input: input(1, "fine") });
    expect(await session.hasUserInput()).toBe("fine");
  });
});

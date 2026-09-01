import { describe, expect, test } from "bun:test";

import type { Agent, AgentTurnOptions } from "../agent/index.ts";
import type { Message, SessionClient } from "../session/index.ts";
import { STOP, StopRequested } from "../session/index.ts";
import { ChatProcess } from "./chat.ts";
import { Process, step } from "./process.ts";
import { StoryProcess, type StoryWork } from "./story.ts";

const message = (seq: number, text: string): Message => ({
  seq,
  at: 1,
  agent: "you",
  kind: "text",
  text,
});

class FakeSession {
  events: string[] = [];
  queued: Message[] = [];
  waiting: Message[] = [];
  statuses: string[] = [];
  answers: string[] = [];
  stopped = false;
  signal = new AbortController().signal;

  async checkpoint() {
    this.events.push("control");
    return this.stopped ? STOP : undefined;
  }

  async refresh() {
    this.events.push("refresh");
  }

  drainInputs() {
    return this.queued.splice(0);
  }

  async waitForInput() {
    return this.waiting.shift() ?? STOP;
  }

  async status(text: string) {
    this.statuses.push(text);
  }

  sink() {
    const write = async (chunk: string) => void this.answers.push(chunk);
    write.end = async () => {};
    return write;
  }
}

describe("process steps", () => {
  test("the decorator checkpoints before and after a successful body", async () => {
    const session = new FakeSession();
    class Example extends Process {
      @step()
      async work() {
        session.events.push("body");
      }
      async run() {
        await this.work();
      }
    }

    await new Example(session as unknown as SessionClient).run();
    expect(session.events).toEqual([
      "control",
      "refresh",
      "control",
      "body",
      "control",
      "refresh",
      "control",
    ]);
  });

  test("checkpoint input is retained until a step asks for it", async () => {
    const session = new FakeSession();
    session.queued.push(message(1, "steer"));
    class Example extends Process {
      async run() {}
    }
    const process = new Example(session as unknown as SessionClient);

    expect(await process.nextInput()).toEqual(message(1, "steer"));
  });

  test("a human stop becomes an exception at the boundary", async () => {
    const session = new FakeSession();
    session.stopped = true;
    class Example extends Process {
      async run() {}
    }
    await expect(
      new Example(session as unknown as SessionClient).checkpoint(),
    ).rejects.toThrow(StopRequested);
  });
});

describe("the chat process", () => {
  test("it continues across turns until the model calls stop", async () => {
    const session = new FakeSession();
    session.waiting.push(message(1, "hello"), message(2, "goodbye"));
    const turns: string[] = [];
    const agent = {
      name: "alice",
      async respond(input: string, options: AgentTurnOptions) {
        turns.push(input);
        await options.sink?.(`answer:${input}`);
        if (input === "goodbye") {
          const stop = options.tools?.stop as { execute?: (...args: any[]) => unknown };
          await stop.execute?.({}, {});
        }
        return { text: "", toolCalls: [], responseMessages: [] };
      },
    } as unknown as Agent;

    const chat = new ChatProcess(session as unknown as SessionClient, agent);
    await chat.run();

    expect(turns).toEqual(["hello", "goodbye"]);
    expect(session.answers).toEqual(["answer:hello", "answer:goodbye"]);
    expect(session.statuses).toEqual(["alice ended the conversation."]);
    expect(chat.finished).toBe(true);
    expect(chat.finishReason).toBe("the agent called stop");
  });

  test("input queued while a response is generating becomes the next turn", async () => {
    const session = new FakeSession();
    session.waiting.push(message(1, "first"));
    const turns: string[] = [];
    const agent = {
      name: "alice",
      async respond(input: string, options: AgentTurnOptions) {
        turns.push(input);
        if (input === "first") {
          session.queued.push(message(2, "second"));
        } else {
          const stop = options.tools?.stop as { execute?: (...args: any[]) => unknown };
          await stop.execute?.({}, {});
        }
        return { text: "", toolCalls: [{}], responseMessages: [] };
      },
    } as unknown as Agent;

    await new ChatProcess(session as unknown as SessionClient, agent).run();

    expect(turns).toEqual(["first", "second"]);
  });

  test("a human stop ends the loop at the next boundary", async () => {
    const session = new FakeSession();
    session.waiting.push(message(1, "hello"));
    const agent = {
      name: "alice",
      async respond() {
        session.stopped = true;
        return { text: "partial", toolCalls: [], responseMessages: [] };
      },
    } as unknown as Agent;

    await expect(
      new ChatProcess(session as unknown as SessionClient, agent).run(),
    ).rejects.toThrow(StopRequested);
  });

  test("a model failure escapes so the session wrapper can mark it failed", async () => {
    const session = new FakeSession();
    session.waiting.push(message(1, "hello"));
    const agent = {
      name: "alice",
      async respond() {
        throw new Error("model unavailable");
      },
    } as unknown as Agent;

    await expect(
      new ChatProcess(session as unknown as SessionClient, agent).run(),
    ).rejects.toThrow("model unavailable");
  });
});

describe("the story process", () => {
  test("hands one working environment through review, implementation, and publication", async () => {
    const session = new FakeSession();
    const calls: string[] = [];
    const work: StoryWork = {
      async preDevelopment() {
        calls.push("review");
      },
      async prepareDevelopment() {
        calls.push("prepare");
      },
      async implement() {
        calls.push("implement");
      },
      async finish() {
        calls.push("finish");
        return "http://gitea/pulls/7";
      },
    };

    const process = new StoryProcess(session as unknown as SessionClient, work);
    await process.run();

    expect(calls).toEqual(["review", "prepare", "implement", "finish"]);
    expect(session.statuses).toEqual([
      "Reviewing the story against the current codebase.",
      "Implementing the reviewed story.",
      "Opened http://gitea/pulls/7",
    ]);
    expect(process.finished).toBe(true);
  });
});

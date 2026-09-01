/** The built-in continuing chat workflow, separated from its env entrypoint. */

import { hasToolCall, tool } from "ai";
import { z } from "zod";

import { type Agent } from "../agent/index.ts";
import { type Message, type SessionClient } from "../session/index.ts";
import { Process, step } from "./process.ts";

export class ChatProcess extends Process {
  readonly agent: Agent;
  #input: Message | null = null;

  constructor(client: SessionClient, agent: Agent) {
    super(client);
    this.agent = agent;
  }

  @step()
  async wait_for_input(): Promise<void> {
    this.#input = await this.nextInput();
  }

  @step()
  async respond(): Promise<void> {
    const input = this.#input;
    this.#input = null;
    if (!input || input.kind !== "text" || !input.text) {
      await this.session.status("This chat currently accepts text input only.");
      return;
    }

    const stop = tool({
      description:
        "End this chat when the conversation has naturally concluded. " +
        "Do not call this merely because one answer is complete or the user may continue.",
      inputSchema: z.object({}),
      execute: async () => {
        await this.session.status(`${this.agent.name} ended the conversation.`);
        this.finish("the agent called stop");
        return { stopped: true };
      },
    });

    const sink = this.session.sink();
    try {
      await this.agent.respond(input.text, {
        sink,
        tools: { stop },
        stopWhen: hasToolCall("stop"),
        abortSignal: this.session.signal,
      });
    } finally {
      await sink.end();
    }
  }

  async run(): Promise<void> {
    while (!this.finished) {
      await this.wait_for_input();
      await this.respond();
    }
  }
}

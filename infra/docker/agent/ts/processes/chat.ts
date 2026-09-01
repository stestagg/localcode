#!/usr/bin/env bun
/** A continuing, persona-only conversation with a model-controlled ending. */

import { Agent } from "../src/agent/index.ts";
import { LlmError, main } from "../src/llm/errors.ts";
import { ChatProcess } from "../src/process/index.ts";
import { session } from "../src/session/index.ts";

const persona = process.env.LOCALCODE_PERSONA;
const prompt = process.env.LOCALCODE_PERSONA_PROMPT;

await main("chat", async () => {
  if (!persona) throw new LlmError("no persona name reached this container");
  if (!prompt) throw new LlmError(`persona '${persona}' has no instructions`);

  await session(async (client) => {
    const agent = await Agent.create({ name: persona, persona: prompt });
    await new ChatProcess(client, agent).run();
  });
});

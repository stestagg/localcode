import { describe, expect, test } from "bun:test";
import type { LanguageModelV4StreamPart } from "@ai-sdk/provider";
import {
  type ModelMessage,
  type ToolSet,
  hasToolCall,
  isStepCount,
  tool,
} from "ai";
import { MockLanguageModelV4, simulateReadableStream } from "ai/test";
import { z } from "zod";

import type { Selection } from "../llm/model.ts";
import {
  Agent,
  type AgentBackend,
  DirectAgentBackend,
} from "./agent.ts";

const USAGE = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
};

const finish = (reason: "stop" | "tool-calls" = "stop") => ({
  type: "finish" as const,
  finishReason: { unified: reason, raw: reason },
  usage: USAGE,
});

function selection(languageModel: MockLanguageModelV4): Selection {
  return {
    provider: "acme",
    model: "a-model",
    npm: "@ai-sdk/acme",
    baseURL: "http://acme.invalid/v1",
    languageModel,
  };
}

const placeholder = selection(new MockLanguageModelV4());
const selector = async () => placeholder;

class RecordingBackend implements AgentBackend {
  capabilities = {
    editableHistory: true,
    structuredOutput: true,
    customTools: true,
    multimodal: true,
  };
  requests: any[] = [];

  async respond(request: any) {
    this.requests.push(request);
    const response: ModelMessage = {
      role: "assistant",
      content: `answer-${this.requests.length}`,
    };
    return { text: response.content as string, toolCalls: [], responseMessages: [response] };
  }

  async generate<OutputType>(request: any) {
    this.requests.push(request);
    return {
      output: { label: "code" } as OutputType,
      responseMessages: [{ role: "assistant", content: '{"label":"code"}' }] as ModelMessage[],
    };
  }
}

describe("agent context", () => {
  test("ordinary turns retain automatic conversation history", async () => {
    const backend = new RecordingBackend();
    const agent = await Agent.create(
      { name: "alice", persona: "Be incisive.", backend },
      selector,
    );

    await agent.respond("first");
    await agent.respond("second");

    expect(backend.requests[0].messages).toHaveLength(1);
    expect(backend.requests[1].messages).toHaveLength(3);
    expect(agent.getHistory().map((item) => item.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(agent.instructions).toBe("Be incisive.");
  });

  test("history can be replaced and edited with multimodal model messages", async () => {
    const backend = new RecordingBackend();
    const agent = await Agent.create(
      { name: "alice", persona: "Persona", role: "Review", backend },
      selector,
    );
    const image: ModelMessage = {
      role: "user",
      content: [
        { type: "image", image: new Uint8Array([1, 2, 3]) },
        {
          type: "file",
          data: new Uint8Array([4, 5, 6]),
          filename: "notes.pdf",
          mediaType: "application/pdf",
        },
      ],
    };

    agent.replaceHistory([image]);
    agent.editHistory((history) => [
      ...history,
      { role: "assistant", content: "I see it." },
    ]);

    expect(agent.getHistory()).toHaveLength(2);
    expect(agent.instructions).toContain("# Role\n\nReview");
    agent.clearHistory();
    expect(agent.getHistory()).toEqual([]);
  });

  test("structured generation returns the validated backend value and keeps context", async () => {
    const backend = new RecordingBackend();
    const agent = await Agent.create(
      { name: "alice", persona: "Persona", backend },
      selector,
    );
    const output = await agent.generate("label this", z.object({ label: z.string() }));

    expect(output).toEqual({ label: "code" });
    expect(agent.getHistory()).toHaveLength(2);
  });
});

describe("the direct backend", () => {
  test("streams text while returning response messages for later turns", async () => {
    const model = new MockLanguageModelV4({
      doStream: async () => ({
        stream: simulateReadableStream({
          chunks: [
            { type: "stream-start", warnings: [] },
            { type: "text-start", id: "0" },
            { type: "text-delta", id: "0", delta: "hello" },
            { type: "text-end", id: "0" },
            finish(),
          ] satisfies LanguageModelV4StreamPart[],
        }),
      }),
    });
    const seen: string[] = [];
    const turn = await new DirectAgentBackend().respond({
      selection: selection(model),
      instructions: "Persona",
      messages: [{ role: "user", content: "hi" }],
      tools: {},
      stopWhen: isStepCount(1),
      sink: (chunk) => void seen.push(chunk),
    });

    expect(turn.text).toBe("hello");
    expect(seen).toEqual(["hello"]);
    expect(turn.responseMessages[0]?.role).toBe("assistant");
  });

  test("a tool-only stop is a successful turn, not a silent response", async () => {
    let called = false;
    const model = new MockLanguageModelV4({
      doStream: async () => ({
        stream: simulateReadableStream({
          chunks: [
            { type: "stream-start", warnings: [] },
            { type: "tool-call", toolCallId: "call-1", toolName: "stop", input: "{}" },
            finish("tool-calls"),
          ] satisfies LanguageModelV4StreamPart[],
        }),
      }),
    });
    const tools: ToolSet = {
      stop: tool({
        description: "stop",
        inputSchema: z.object({}),
        execute: async () => {
          called = true;
          return { stopped: true };
        },
      }),
    };

    const agent = await Agent.create(
      { name: "alice", persona: "Persona", tools },
      async () => selection(model),
    );
    const turn = await agent.respond("bye", { stopWhen: hasToolCall("stop") });

    expect(called).toBe(true);
    expect(turn.text).toBe("");
    expect(turn.toolCalls).toHaveLength(1);
    expect(agent.getHistory().map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "tool",
    ]);
  });

  test("structured output is parsed and checked against its schema", async () => {
    const model = new MockLanguageModelV4({
      doGenerate: async () => ({
        content: [{ type: "text", text: '{"label":"code"}' }],
        finishReason: { unified: "stop", raw: "stop" },
        usage: USAGE,
        warnings: [],
      }),
    });
    const result = await new DirectAgentBackend().generate({
      selection: selection(model),
      instructions: "Persona",
      messages: [{ role: "user", content: "label" }],
      tools: {},
      stopWhen: isStepCount(1),
      schema: z.object({ label: z.string() }),
    });

    expect(result.output).toEqual({ label: "code" });
  });
});

import { describe, expect, test } from "bun:test";
import type { LanguageModelV4StreamPart } from "@ai-sdk/provider";
import { APICallError, RetryError } from "ai";
import { MockLanguageModelV4, simulateReadableStream } from "ai/test";

import { LlmError } from "./errors.ts";
import type { Selection } from "./model.ts";
import { describe as explain, streamTo } from "./stream.ts";

/** How a stream ends, and what it cost. The spec's shapes; nothing here reads them. */
const FINISHED = {
  type: "finish",
  finishReason: { unified: "stop", raw: "stop" },
  usage: {
    inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
    outputTokens: { total: 1, text: 1, reasoning: 0 },
  },
} satisfies LanguageModelV4StreamPart;

function saying(...deltas: string[]): MockLanguageModelV4 {
  return new MockLanguageModelV4({
    doStream: async () => ({
      stream: simulateReadableStream({
        chunks: [
          { type: "stream-start", warnings: [] },
          { type: "text-start", id: "0" },
          ...deltas.map((delta) => ({ type: "text-delta" as const, id: "0", delta })),
          { type: "text-end", id: "0" },
          FINISHED,
        ] satisfies LanguageModelV4StreamPart[],
      }),
    }),
  });
}

function selection(model: MockLanguageModelV4): Selection {
  return {
    provider: "acme",
    model: "a-model",
    npm: "@ai-sdk/acme",
    baseURL: "http://host.docker.internal:8080/v1",
    languageModel: model,
  };
}

describe("streaming an answer", () => {
  test("chunks arrive as they are produced, not all at once at the end", async () => {
    const seen: string[] = [];
    await streamTo((chunk) => void seen.push(chunk), selection(saying("hello ", "world")), {
      prompt: "hi",
    });
    expect(seen).toEqual(["hello ", "world"]);
  });

  test("a stream that never produces text is a failure, not an empty answer", async () => {
    const silent = new MockLanguageModelV4({
      doStream: async () => ({
        stream: simulateReadableStream({
          chunks: [
            { type: "stream-start", warnings: [] },
            FINISHED,
          ] satisfies LanguageModelV4StreamPart[],
        }),
      }),
    });
    const attempt = streamTo(() => {}, selection(silent), { prompt: "hi" });
    await expect(attempt).rejects.toThrow(LlmError);
    await expect(attempt).rejects.toThrow(/returned no text for 'a-model'/);
  });

  test("a request that fails is reported, rather than read as silence", async () => {
    // streamText does not throw: it ends the text stream and reports to
    // onError. Without that wired up this would exit 0 having printed nothing.
    const broken = new MockLanguageModelV4({
      doStream: async () => {
        throw new APICallError({
          message: "invalid api key",
          url: "http://x/v1/chat/completions",
          requestBodyValues: {},
          statusCode: 401,
        });
      },
    });
    const attempt = streamTo(() => {}, selection(broken), { prompt: "hi" });
    await expect(attempt).rejects.toThrow(LlmError);
    await expect(attempt).rejects.toThrow("invalid api key");
  });
});

describe("what went wrong", () => {
  const where = selection(saying("x"));

  test("nothing listening says where it looked, since the host rewrote the url", () => {
    const refused = new APICallError({
      message: "Cannot connect to API: connect ECONNREFUSED 192.168.65.2:8080",
      url: "http://host.docker.internal:8080/v1/chat/completions",
      requestBodyValues: {},
    });
    expect(explain(refused, where)).toContain("could not reach http://host.docker.internal:8080");
  });

  test("a provider that answered gets to say why itself", () => {
    const refused = new APICallError({
      message: "model 'a-model' does not exist",
      url: "http://x/v1/chat/completions",
      requestBodyValues: {},
      statusCode: 404,
    });
    expect(explain(refused, where)).toBe("model 'a-model' does not exist");
  });

  test("the retries wrapped around it do not hide the address that was tried", () => {
    // What actually surfaces from a dead endpoint: the last attempt, inside the
    // RetryError that gave up on it.
    const refused = new APICallError({
      message: "Cannot connect to API: Unable to connect.",
      url: "http://host.docker.internal:45999/v1/chat/completions",
      requestBodyValues: {},
    });
    const gaveUp = new RetryError({
      message: "Failed after 3 attempts.",
      reason: "errorNotRetryable",
      errors: [refused],
    });
    expect(explain(gaveUp, where)).toContain(
      "could not reach http://host.docker.internal:45999",
    );
  });

  test("anything else is still said in one line", () => {
    expect(explain(new Error("boom"), where)).toBe("boom");
    expect(explain("boom", where)).toBe("boom");
  });
});

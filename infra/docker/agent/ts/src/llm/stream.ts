/**
 * An answer, streamed to stdout as the model produces it.
 *
 * The one thing worth knowing here: `streamText` does not throw. A request that
 * fails ends the text stream normally and reports the error to `onError`, so a
 * script that only iterated the stream would exit 0 having printed nothing and
 * call that an answer. Both guards below exist for that -- the captured error,
 * and the flat refusal to treat silence as success.
 */

import { APICallError, RetryError, streamText } from "ai";

import { LlmError } from "./errors.ts";
import type { Selection } from "./model.ts";

type StreamTextArgs = Parameters<typeof streamText>[0];

/** `Omit` over a union collapses it; this keeps the alternatives apart. */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/**
 * Everything `streamText` takes, minus the two things this owns.
 *
 * Distributive, because the argument is a union -- a prompt or a list of
 * messages, never both -- and a caller passing neither should still be caught.
 */
export type StreamOptions = DistributiveOmit<StreamTextArgs, "model" | "onError">;

/**
 * The call that actually failed, out of whatever wrapped it.
 *
 * A request is retried, so what surfaces is a `RetryError` holding the last
 * attempt; and a provider package may wrap that again. The useful error is the
 * one at the bottom, and reporting the wrapper instead loses both the status
 * code and the address that was tried.
 */
function underlying(error: unknown): APICallError | null {
  const seen = new Set<unknown>();
  let current: unknown = error;
  while (current !== null && current !== undefined && !seen.has(current)) {
    seen.add(current);
    if (APICallError.isInstance(current)) return current;
    current = RetryError.isInstance(current)
      ? current.lastError
      : (current as { cause?: unknown }).cause;
  }
  return null;
}

/** What went wrong, said in a way someone can act on. */
export function describe(error: unknown, selection: Selection): string {
  const call = underlying(error);
  if (call) {
    if (call.statusCode === undefined) {
      // Say where it looked. `localhost` on the host is this container on the
      // inside, so the url that was tried is rewritten before it gets here --
      // which makes "connection refused" baffling without the address.
      return `could not reach ${call.url || selection.baseURL}: ${call.message}`;
    }
    // The provider's own message is the useful part; a status code alone never
    // says whether it was the key, the model name or the quota.
    return call.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** Where an answer goes, a chunk at a time. */
export type Sink = (chunk: string) => void | Promise<void>;

/**
 * Run the model and hand what it says to `sink`, chunk by chunk.
 *
 * Most callers want `streamToStdout`. This is for the ones that want the text
 * somewhere else -- collected into a string, written to a file, forwarded.
 */
export async function streamTo(
  sink: Sink,
  selection: Selection,
  options: StreamOptions,
): Promise<void> {
  let failure: unknown;
  // Cast because spreading the union back together loses which side of it this
  // is; the alternatives were kept apart on the way in, which is where it counts.
  const result = streamText({
    ...options,
    model: selection.languageModel,
    onError: ({ error }: { error: unknown }) => {
      failure ??= error;
    },
  } as StreamTextArgs);

  let answered = false;
  try {
    for await (const chunk of result.textStream) {
      if (!chunk) continue;
      await sink(chunk);
      answered = true;
    }
  } catch (error) {
    failure ??= error;
  }

  if (failure !== undefined) {
    if (answered) console.error();
    throw new LlmError(describe(failure, selection));
  }
  if (!answered) {
    // A stream that ends without ever producing text is not a success. It means
    // the endpoint answered in a shape this model's protocol did not recognise,
    // and exiting 0 on it would report silence as an answer.
    throw new LlmError(
      `${selection.provider} returned no text for '${selection.model}'; check the ` +
        `model id, and that ${selection.baseURL || "the endpoint " + selection.npm + " uses"} ` +
        `is that provider's own API rather than something merely compatible`,
    );
  }
}

/**
 * Run the model and write what it says to stdout.
 *
 * Written unbuffered, chunk by chunk: stdout is a pipe here, and the point of
 * streaming is that the answer appears while it is being produced rather than
 * all at once when the process ends.
 */
export async function streamToStdout(
  selection: Selection,
  options: StreamOptions,
): Promise<void> {
  await streamTo((chunk) => Bun.write(Bun.stdout, chunk).then(() => undefined), selection, options);
}

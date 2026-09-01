#!/usr/bin/env bun
/**
 * One question, one answer, using the AI SDK directly rather than opencode.
 *
 * The shortest end-to-end check there is: it proves this container can reach a
 * provider with the project's own config and credentials, which is exactly what
 * an agent run depends on. It runs here -- inside a throwaway container --
 * rather than on the host. Nothing outside docker talks to a provider.
 *
 * Where the answer goes depends on who asked. From the terminal it is stdout,
 * as it always was. From the web ui the container is given a session, and the
 * answer goes there instead -- attributed, timestamped and kept -- while this
 * container's own stdout stays what it is, the process log.
 *
 * There is almost nothing in this file, and that is deliberate: everything it
 * does beyond reading a prompt lives in `../src`, where the next script that
 * wants a model or a session can have it too.
 */

import { LlmError, main } from "../src/llm/errors.ts";
import { selectModel } from "../src/llm/model.ts";
import { streamTo, streamToStdout } from "../src/llm/stream.ts";
import { configured, session } from "../src/session/index.ts";

/** Agreed with `SYSTEM_ENV` in `src/localcode/llm/console.py`. */
const SYSTEM_ENV = "LOCALCODE_ASK_SYSTEM";

await main("ask", async () => {
  // Over stdin rather than the environment: a prompt can be long, and it has
  // no business showing up in `docker inspect`.
  const prompt = (await Bun.stdin.text()).trim();
  if (!prompt) throw new LlmError("no prompt on stdin");

  const selection = await selectModel();
  const system = process.env[SYSTEM_ENV] || undefined;

  if (!configured()) {
    await streamToStdout(selection, { prompt, system });
    // The answer rarely ends in one, and a shell prompt on the same line reads
    // as part of it.
    await Bun.write(Bun.stdout, "\n");
    return;
  }

  await session(async (client) => {
    const sink = client.sink();
    try {
      // The signal is what makes the Stop button mean something mid-answer:
      // there is no checkpoint inside a single streamed response to put one at.
      await streamTo(sink, selection, { prompt, system, abortSignal: client.signal });
    } catch (error) {
      // An answer cut short by the person watching is not a failure. Anything
      // else is, and still is.
      if (!client.stopped) throw error;
    } finally {
      // Whatever happened, what the model did say is kept: `end` closes the
      // message so the partial answer is written rather than dropped.
      await sink.end();
    }
  });
});

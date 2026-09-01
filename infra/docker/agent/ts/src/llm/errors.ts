/**
 * The one way these scripts fail, and the one way they say so.
 *
 * A script here runs in a throwaway container whose stdout is the answer and
 * whose stderr is read by a person on the far side of docker. So a failure it
 * can explain gets one line naming the script and what to do about it, and
 * anything else gets a stack, because an unexpected error is a bug in here
 * rather than something the caller mis-configured.
 */

/** A failure with something useful to say. Never carries a stack to the user. */
export class LlmError extends Error {
  override readonly name = "LlmError";
}

/**
 * Run a script's body, and turn whatever it throws into an exit.
 *
 * The name is the script's own, so the prefix on a message is `ask:` for
 * `ask.ts` and something else for the next one -- rather than the single
 * hardcoded prefix the python this replaces had.
 */
export async function main(name: string, run: () => Promise<void>): Promise<void> {
  try {
    await run();
  } catch (error) {
    if (error instanceof LlmError) {
      console.error(`${name}: ${error.message}`);
    } else {
      console.error(`${name}: unexpected failure`);
      console.error(error);
    }
    process.exit(1);
  }
}

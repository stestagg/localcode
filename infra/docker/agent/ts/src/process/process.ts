/** Cooperative workflow steps running inside one process container. */

import type { Message } from "../session/protocol.ts";
import { type SessionClient, STOP, StopRequested } from "../session/client.ts";

export class ProcessFinished extends Error {
  override readonly name = "ProcessFinished";
}

export abstract class Process {
  readonly session: SessionClient;
  currentStep: string | null = null;
  finishReason = "";

  #finished = false;
  #pendingInputs: Message[] = [];

  constructor(session: SessionClient) {
    this.session = session;
  }

  get finished(): boolean {
    return this.#finished;
  }

  /** Request successful completion after the current step returns. */
  finish(reason = ""): void {
    this.#finished = true;
    this.finishReason = reason;
  }

  /**
   * Honour control state and collect everything typed since the last boundary.
   * The round-trip clears the controller's catch-up queue; sequence IDs keep
   * inputs already received as pushed events from appearing twice.
   */
  async checkpoint(): Promise<void> {
    if (this.finished) return;
    if ((await this.session.checkpoint()) === STOP) throw new StopRequested();
    await this.session.refresh();
    if ((await this.session.checkpoint()) === STOP) throw new StopRequested();

    const inputs = this.session.drainInputs();
    if (inputs.length) await this.onInput(inputs);
  }

  /** Default steering policy: retain inputs until a step asks for one. */
  protected async onInput(inputs: Message[]): Promise<void> {
    this.#pendingInputs.push(...inputs);
  }

  /** The next queued user message, blocking without polling when necessary. */
  async nextInput(): Promise<Message> {
    if (this.finished) throw new ProcessFinished();
    const pending = this.#pendingInputs.shift();
    if (pending) return pending;

    await this.checkpoint();
    const collected = this.#pendingInputs.shift();
    if (collected) return collected;

    const input = await this.session.waitForInput();
    if (input === STOP) throw new StopRequested();
    return input;
  }

  /** Called by the method decorator; public only so decorators can reach it. */
  async runStep<T>(name: string, run: () => Promise<T>): Promise<T> {
    await this.checkpoint();
    this.currentStep = name;
    console.log(`process: step ${name} start`);
    try {
      const result = await run();
      await this.checkpoint();
      console.log(`process: step ${name} finish`);
      return result;
    } catch (error) {
      console.error(`process: step ${name} failed`);
      throw error;
    } finally {
      this.currentStep = null;
    }
  }

  abstract run(): Promise<void>;
}

/** A standard TypeScript method decorator for one discrete process step. */
export function step(name?: string) {
  return function <This extends Process, Args extends unknown[], Result>(
    value: (this: This, ...args: Args) => Promise<Result>,
    context: ClassMethodDecoratorContext<
      This,
      (this: This, ...args: Args) => Promise<Result>
    >,
  ): (this: This, ...args: Args) => Promise<Result> {
    const chosen = name ?? String(context.name);
    return function (this: This, ...args: Args): Promise<Result> {
      return this.runStep(chosen, () => value.apply(this, args));
    };
  };
}

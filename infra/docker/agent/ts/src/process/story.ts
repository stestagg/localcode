/** The two-role story workflow; concrete repository work is injected. */

import { type SessionClient } from "../session/index.ts";
import { Process, step } from "./process.ts";

export interface StoryWork {
  preDevelopment(): Promise<void>;
  prepareDevelopment(): Promise<void>;
  implement(): Promise<void>;
  finish(): Promise<string>;
}

export class StoryProcess extends Process {
  readonly work: StoryWork;

  constructor(client: SessionClient, work: StoryWork) {
    super(client);
    this.work = work;
  }

  @step("pre-development review")
  async pre_development(): Promise<void> {
    await this.session.status("Reviewing the story against the current codebase.");
    await this.work.preDevelopment();
  }

  @step("prepare development branch")
  async prepare_development(): Promise<void> {
    await this.work.prepareDevelopment();
  }

  @step("implement story")
  async implement(): Promise<void> {
    await this.session.status("Implementing the reviewed story.");
    await this.work.implement();
  }

  @step("verify and publish")
  async publish(): Promise<void> {
    const pull = await this.work.finish();
    await this.session.status(`Opened ${pull}`);
    this.finish(`opened ${pull}`);
  }

  async run(): Promise<void> {
    await this.pre_development();
    await this.prepare_development();
    await this.implement();
    await this.publish();
  }
}

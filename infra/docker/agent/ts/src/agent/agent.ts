/** A stateful model participant: identity, tools, and editable context. */

import {
  type FlexibleSchema,
  type ModelMessage,
  Output,
  type StopCondition,
  type ToolSet,
  type UserContent,
  generateText,
  isStepCount,
  streamText,
} from "ai";

import { LlmError } from "../llm/errors.ts";
import type { Selection } from "../llm/model.ts";
import { selectModel } from "../llm/model.ts";
import { describe } from "../llm/stream.ts";

export type AgentCapabilities = {
  editableHistory: boolean;
  structuredOutput: boolean;
  customTools: boolean;
  multimodal: boolean;
};

export type AgentSink = (chunk: string) => void | Promise<void>;
export type AgentStopCondition =
  | StopCondition<ToolSet>
  | Array<StopCondition<ToolSet>>;

export type AgentTurnOptions = {
  sink?: AgentSink;
  tools?: ToolSet;
  abortSignal?: AbortSignal;
  stopWhen?: AgentStopCondition;
};

export type AgentGenerateOptions = {
  tools?: ToolSet;
  abortSignal?: AbortSignal;
  stopWhen?: AgentStopCondition;
  name?: string;
  description?: string;
};

export type AgentTurn = {
  text: string;
  toolCalls: unknown[];
  responseMessages: ModelMessage[];
};

type BackendRequest = {
  selection: Selection;
  instructions: string;
  messages: ModelMessage[];
  tools: ToolSet;
  abortSignal?: AbortSignal;
  stopWhen: AgentStopCondition;
};

export interface AgentBackend {
  readonly capabilities: AgentCapabilities;
  respond(request: BackendRequest & { sink?: AgentSink }): Promise<AgentTurn>;
  generate<OutputType>(
    request: BackendRequest & {
      schema: FlexibleSchema<OutputType>;
      name?: string;
      description?: string;
    },
  ): Promise<{ output: OutputType; responseMessages: ModelMessage[] }>;
}

/** Direct AI SDK access, using the same provider selection as `llm ask`. */
export class DirectAgentBackend implements AgentBackend {
  readonly capabilities: AgentCapabilities = {
    editableHistory: true,
    structuredOutput: true,
    customTools: true,
    multimodal: true,
  };

  async respond(request: BackendRequest & { sink?: AgentSink }): Promise<AgentTurn> {
    let failure: unknown;
    let text = "";
    const result = streamText({
      model: request.selection.languageModel,
      system: request.instructions || undefined,
      messages: request.messages,
      tools: request.tools,
      stopWhen: request.stopWhen,
      abortSignal: request.abortSignal,
      onError: ({ error }) => {
        failure ??= error;
      },
    });

    try {
      for await (const chunk of result.textStream) {
        if (!chunk) continue;
        text += chunk;
        await request.sink?.(chunk);
      }
    } catch (error) {
      failure ??= error;
    }

    if (failure !== undefined) {
      throw new LlmError(describe(failure, request.selection));
    }

    const [toolCalls, responseMessages] = await Promise.all([
      result.toolCalls,
      result.responseMessages,
    ]);
    if (!text && toolCalls.length === 0) {
      throw new LlmError(
        `${request.selection.provider} returned neither text nor a tool call for ` +
          `'${request.selection.model}'`,
      );
    }
    return {
      text,
      toolCalls,
      responseMessages: responseMessages as ModelMessage[],
    };
  }

  async generate<OutputType>(
    request: BackendRequest & {
      schema: FlexibleSchema<OutputType>;
      name?: string;
      description?: string;
    },
  ): Promise<{ output: OutputType; responseMessages: ModelMessage[] }> {
    const result = await generateText({
      model: request.selection.languageModel,
      system: request.instructions || undefined,
      messages: request.messages,
      tools: request.tools,
      stopWhen: request.stopWhen,
      abortSignal: request.abortSignal,
      output: Output.object({
        schema: request.schema,
        name: request.name,
        description: request.description,
      }),
    });
    return {
      output: result.output,
      responseMessages: result.responseMessages as ModelMessage[],
    };
  }
}

export type AgentOptions = {
  name: string;
  persona: string;
  role?: string;
  model?: string;
  tools?: ToolSet;
  backend?: AgentBackend;
};

export type ModelSelector = (options?: { model?: string }) => Promise<Selection>;

export class Agent {
  readonly name: string;
  readonly persona: string;
  readonly role?: string;
  readonly backend: AgentBackend;

  #selection: Selection;
  #tools: ToolSet;
  #history: ModelMessage[] = [];

  private constructor(options: AgentOptions, selection: Selection) {
    this.name = options.name;
    this.persona = options.persona;
    this.role = options.role;
    this.backend = options.backend ?? new DirectAgentBackend();
    this.#selection = selection;
    this.#tools = options.tools ?? {};
  }

  static async create(
    options: AgentOptions,
    selector: ModelSelector = selectModel,
  ): Promise<Agent> {
    return new Agent(options, await selector({ model: options.model }));
  }

  get instructions(): string {
    return this.role
      ? `${this.persona}\n\n# Role\n\n${this.role}`
      : this.persona;
  }

  getHistory(): ModelMessage[] {
    return [...this.#history];
  }

  replaceHistory(messages: readonly ModelMessage[]): void {
    this.#history = [...messages];
  }

  editHistory(edit: (messages: ModelMessage[]) => ModelMessage[]): void {
    this.#history = [...edit(this.getHistory())];
  }

  clearHistory(): void {
    this.#history = [];
  }

  async respond(input: UserContent, options: AgentTurnOptions = {}): Promise<AgentTurn> {
    const user: ModelMessage = { role: "user", content: input };
    this.#history.push(user);
    const tools = { ...this.#tools, ...(options.tools ?? {}) };
    const turn = await this.backend.respond({
      selection: this.#selection,
      instructions: this.instructions,
      messages: this.getHistory(),
      tools,
      abortSignal: options.abortSignal,
      stopWhen: bounded(options.stopWhen),
      sink: options.sink,
    });
    this.#history.push(...turn.responseMessages);
    return turn;
  }

  async generate<OutputType>(
    input: UserContent,
    schema: FlexibleSchema<OutputType>,
    options: AgentGenerateOptions = {},
  ): Promise<OutputType> {
    const user: ModelMessage = { role: "user", content: input };
    this.#history.push(user);
    const generated = await this.backend.generate({
      selection: this.#selection,
      instructions: this.instructions,
      messages: this.getHistory(),
      tools: { ...this.#tools, ...(options.tools ?? {}) },
      abortSignal: options.abortSignal,
      stopWhen: bounded(options.stopWhen),
      schema,
      name: options.name,
      description: options.description,
    });
    this.#history.push(...generated.responseMessages);
    return generated.output;
  }
}

function bounded(stopWhen?: AgentStopCondition): AgentStopCondition {
  const requested = stopWhen === undefined ? [] : Array.isArray(stopWhen) ? stopWhen : [stopWhen];
  return [isStepCount(8), ...requested];
}

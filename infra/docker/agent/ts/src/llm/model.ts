/**
 * The call a script makes: give me the model this project chose.
 *
 * The selection lives in the project's config and nowhere else, so it is read
 * here rather than resolved on the host and passed in. One file says which
 * model, and both the thing that runs opencode and everything in here agree by
 * reading it -- which is why there is no host flag that can disagree with it.
 */

import type { LanguageModel } from "ai";

import { LlmError } from "./errors.ts";
import { type OpencodeConfig, type Settings, loadSettings } from "./settings.ts";
import { resolveProvider } from "./provider.ts";

export interface Selection {
  /** The provider id, as the config named it. */
  provider: string;
  /** The model id, as the provider knows it. */
  model: string;
  /** The package that implemented the provider. */
  npm: string;
  /** The address the config gave, if any. Empty means the package's own default. */
  baseURL: string;
  /** Ready to hand to `streamText`, `generateText` and the rest. */
  languageModel: LanguageModel;
}

export interface SelectOptions {
  /**
   * A `provider/model` to use instead of the config's own.
   *
   * For a script that wants something other than the project's main model -- a
   * cheap one to summarise with, say -- while still resolving it against the
   * same config, credentials and catalogue.
   */
  model?: string;
  /** Already-loaded settings, if the caller has them. Read from the environment otherwise. */
  settings?: Settings;
}

/** Split `provider/model`. The model half may itself contain slashes. */
export function splitReference(reference: string): { provider: string; model: string } {
  const slash = reference.indexOf("/");
  const provider = slash < 0 ? "" : reference.slice(0, slash);
  const model = slash < 0 ? "" : reference.slice(slash + 1);
  if (!provider || !model) {
    throw new LlmError(`model must be 'provider/model'; got '${reference}'`);
  }
  return { provider, model };
}

/**
 * Every provider the config declares this exact string as a model of.
 *
 * A model id usually has a slash in it -- who published the weights, then which
 * ones -- so one written on its own is a complete-looking `provider/model` that
 * selects a provider nobody has heard of, or none at all. The config says which
 * provider serves that model, which is enough to quote the selection back
 * rather than describe it.
 */
export function declaredUnder(config: OpencodeConfig, reference: string): string[] {
  return Object.entries(config.provider ?? {})
    .filter(([, block]) => {
      const models: unknown = block?.models;
      return typeof models === "object" && models !== null && reference in models;
    })
    .map(([id]) => id);
}

/** English for a list of one or more provider names. */
function or(names: string[]): string {
  const last = names[names.length - 1] as string;
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} or ${last}` : last;
}

/**
 * The same failure, with the selection the config was probably reaching for.
 *
 * The suggestion is only ever added to a failure, never acted on: a provider
 * that does resolve is the project's answer whatever else the config lists,
 * and opencode reads this field the same way. What changes is the advice --
 * without this, a missing provider name is answered by being told to go and
 * implement a provider named after whoever published the model.
 */
function withSuggestion(error: unknown, reference: string, providers: string[]): unknown {
  if (!(error instanceof LlmError) || !providers.length) return error;
  return new LlmError(
    `${error.message}. The config declares '${reference}' as a model of ` +
      `${or(providers.map((id) => `'${id}'`))}, so "model": "${providers[0]}/${reference}" ` +
      `is probably what it should say`,
  );
}

/** Resolve the configured model, ready to be called. */
export async function selectModel(options: SelectOptions = {}): Promise<Selection> {
  const settings = options.settings ?? loadSettings();
  const reference = options.model ?? settings.config.model;
  if (typeof reference !== "string" || !reference) {
    throw new LlmError(
      `no model selected: set "model": "provider/model" in .localcode/opencode.json`,
    );
  }

  // Only when the reference selects a provider the config never declared: one
  // it did declare is a real provider having a real problem, and a suggestion
  // there would be second-guessing a config that means what it says.
  const named = reference.slice(0, Math.max(0, reference.indexOf("/")));
  const declaring =
    settings.config.provider?.[named] === undefined ? declaredUnder(settings.config, reference) : [];

  try {
    const { provider, model } = splitReference(reference);
    const resolved = await resolveProvider(settings, provider);
    return {
      provider,
      model,
      npm: resolved.npm,
      baseURL: resolved.baseURL,
      languageModel: resolved.provider.languageModel(model) as LanguageModel,
    };
  } catch (error) {
    throw withSuggestion(error, reference, declaring);
  }
}

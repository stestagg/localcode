/**
 * The two opencode files, as this container was handed them.
 *
 * `entrypoint.sh` writes them to the paths opencode reads before a runner
 * starts. These scripts have no opencode to satisfy, so they read the same two
 * environment variables directly -- the ones `src/localcode/llm/container.py`
 * fills in on the host, which is the only other place these names appear.
 *
 * Both are parsed as JSONC, because opencode accepts a `.jsonc` and the config
 * arrives exactly as it sits on disk. That is a real parser rather than the
 * hand-written scanner this replaces: keeping pace with another tool's parser
 * leniency by hand is a losing game.
 */

import { type ParseError, parse, printParseErrorCode } from "jsonc-parser";

import { LlmError } from "./errors.ts";

/** The env vars `llm/container.py` sets. Agreed with it, and with nothing else. */
export const CONFIG_ENV = "LOCALCODE_OPENCODE_CONFIG";
export const AUTH_ENV = "LOCALCODE_OPENCODE_AUTH";

/** `{env:NAME}`, opencode's way of naming a value without committing it. */
export const ENV_PLACEHOLDER = /\{env:([^}]+)\}/g;

export interface ProviderOptions {
  baseURL?: string;
  apiKey?: string;
  headers?: Record<string, string>;
  [key: string]: unknown;
}

export interface ProviderBlock {
  /** The npm package implementing this provider. opencode reads it; so do we. */
  npm?: string;
  options?: ProviderOptions;
  /** Keyed by model id, as the provider knows it. Nothing here reads the values. */
  models?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface OpencodeConfig {
  /** `provider/model`. The selection lives here and nowhere else. */
  model?: string;
  provider?: Record<string, ProviderBlock>;
  [key: string]: unknown;
}

/**
 * The credential store, left deliberately opaque.
 *
 * Which field carries a token is a fact about opencode's file rather than
 * about any provider, and it is a fact that changes; see `credential()`.
 */
export type Auth = Record<string, unknown>;

export interface Settings {
  config: OpencodeConfig;
  auth: Auth;
}

/**
 * Expand `{env:NAME}` against this container's environment.
 *
 * The host forwards the variables a config mentions and leaves the placeholders
 * alone, so they resolve here. Unexpanded, one would be sent verbatim as though
 * the literal text `{env:OPENAI_API_KEY}` were the key itself.
 */
export function expandEnv(value: string, env: NodeJS.ProcessEnv = process.env): string {
  return value.replace(ENV_PLACEHOLDER, (_, name: string) => env[name.trim()] ?? "");
}

/** A JSON (or JSONC) object out of one environment variable, or an empty one. */
function objectFromEnv(name: string, env: NodeJS.ProcessEnv): Record<string, unknown> {
  const raw = env[name];
  if (!raw || !raw.trim()) return {};

  const errors: ParseError[] = [];
  const value: unknown = parse(raw, errors, {
    allowTrailingComma: true,
    disallowComments: false,
  });
  const first = errors[0];
  if (first) {
    throw new LlmError(
      `${name} is not valid JSON: ${printParseErrorCode(first.error)} at offset ${first.offset}`,
    );
  }
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Everything the project configured, as this container received it. */
export function loadSettings(env: NodeJS.ProcessEnv = process.env): Settings {
  return {
    config: objectFromEnv(CONFIG_ENV, env) as OpencodeConfig,
    auth: objectFromEnv(AUTH_ENV, env),
  };
}

/** One provider's block from the config, or an empty one. */
export function providerBlock(config: OpencodeConfig, id: string): ProviderBlock {
  const block = config.provider?.[id];
  return typeof block === "object" && block !== null ? block : {};
}

/** One provider's `options` from the config, or an empty one. */
export function providerOptions(config: OpencodeConfig, id: string): ProviderOptions {
  const options = providerBlock(config, id).options;
  return typeof options === "object" && options !== null ? options : {};
}

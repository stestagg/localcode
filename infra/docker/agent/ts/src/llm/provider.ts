/**
 * Turning a provider id into something that can be called.
 *
 * This is where localcode and opencode agree, and the reason any of this was
 * moved off python. opencode resolves a provider by finding the npm package
 * that implements it -- named in the config, or looked up in models.dev -- and
 * loading it. So does this. A project whose config works under the `opencode`
 * runner therefore works under these scripts by construction, which was not
 * true when the two resolved providers by different rules.
 *
 * No provider is named in here. What a provider is called, which package
 * implements it and which variable holds its key are all facts that live in
 * the catalogue or in the project's own config, and both arrive at runtime.
 */

import { existsSync } from "node:fs";
import { join } from "node:path";

import { LlmError } from "./errors.ts";
import type { CatalogueEntry } from "./catalogue.ts";
import { lookup } from "./catalogue.ts";
import {
  type Auth,
  type ProviderOptions,
  type Settings,
  expandEnv,
  providerBlock,
  providerOptions,
} from "./settings.ts";

/**
 * What to send when the config says the endpoint wants no key at all.
 *
 * `apiKey: ""` is a deliberate answer rather than a missing one -- it is how a
 * server on your own machine says it needs no credential -- but a provider
 * client will not build without something to authenticate with. So a
 * placeholder goes instead: an endpoint that wants no key does not read it, and
 * one that does will reject this and say so, which is the right outcome.
 */
export const NO_KEY = "none";

/**
 * The protocol nearly everything self-hosted speaks. Not a provider: it is what
 * is left when there is no provider to look up, only an address.
 */
export const COMPATIBLE = "@ai-sdk/openai-compatible";

/** This package's own root, which is where an on-demand install has to land.
 * Two up from `src/llm`, where `node_modules` and `package.json` are. */
export const ROOT = new URL("../..", import.meta.url).pathname;

/**
 * Fields in a credential that are not the credential.
 *
 * The rest of the object is searched for one, rather than only the three names
 * this used to know about, because which field carries the token is a fact
 * about opencode's file and opencode is free to change it.
 */
const NOT_A_KEY = new Set(["type", "provider", "id", "expires", "refresh"]);

/** Names worth trying first: an api key, an oauth access token, a bearer token. */
const LIKELY_KEYS = ["key", "access", "token"];

/** The package half of a specifier: `@scope/pkg/sub` is installed as `@scope/pkg`. */
export function packageName(spec: string): string {
  const parts = spec.split("/");
  return spec.startsWith("@") ? parts.slice(0, 2).join("/") : (parts[0] ?? spec);
}

/** The stored credential for one provider, whatever opencode called the field. */
export function credential(auth: Auth, id: string): string | undefined {
  const entry = auth[id];
  if (typeof entry !== "object" || entry === null) return undefined;
  const fields = entry as Record<string, unknown>;

  for (const name of LIKELY_KEYS) {
    const value = fields[name];
    if (typeof value === "string" && value) return value;
  }
  for (const [name, value] of Object.entries(fields)) {
    if (!NOT_A_KEY.has(name) && typeof value === "string" && value) return value;
  }
  return undefined;
}

/**
 * The key to present, or `undefined` to let the provider package find its own.
 *
 * An explicit `apiKey` in the config wins, then the stored credential, then
 * whichever variable the catalogue says this provider answers to -- so a key
 * already exported for another tool is picked up without being copied anywhere.
 */
export function apiKeyFor(
  settings: Settings,
  id: string,
  entry: CatalogueEntry | null,
  env: NodeJS.ProcessEnv = process.env,
): string | undefined {
  const explicit = providerOptions(settings.config, id).apiKey;
  if (typeof explicit === "string") {
    // A `{env:NAME}` here is a reference to a key rather than one.
    return expandEnv(explicit, env) || NO_KEY;
  }

  const stored = credential(settings.auth, id);
  if (stored) return stored;

  for (const name of entry?.env ?? []) {
    const value = env[name];
    if (value) return value;
  }
  return undefined;
}

/**
 * Which npm package implements this provider.
 *
 * The config wins, because it is the project's own answer and it is the field
 * opencode reads. Then the catalogue. Then, if all we have is an address, the
 * compatible protocol -- which is what an address on its own means.
 */
export function packageFor(
  settings: Settings,
  id: string,
  entry: CatalogueEntry | null,
): string {
  const declared = providerBlock(settings.config, id).npm;
  if (typeof declared === "string" && declared) return declared;
  if (entry?.npm) return entry.npm;
  if (providerOptions(settings.config, id).baseURL) return COMPATIBLE;

  throw new LlmError(
    `nothing implements provider '${id}': models.dev has not heard of it, so ` +
      `.localcode/opencode.json has to say either which package implements it ` +
      `(provider.${id}.npm) or where it is (provider.${id}.options.baseURL)`,
  );
}

/** Install a package into this package's own tree, the way opencode does. */
async function install(spec: string): Promise<void> {
  const pkg = packageName(spec);
  console.error(`fetching ${pkg}, which this image was not built with`);
  // `process.execPath` rather than "bun": the bun already running is the one to
  // install with, and it is not required to be on PATH. Its chatter goes to
  // stderr, because stdout here is the answer.
  const proc = Bun.spawn([process.execPath, "add", "--silent", pkg], {
    cwd: ROOT,
    stdout: "ignore",
    stderr: "inherit",
  });
  if ((await proc.exited) !== 0) {
    throw new LlmError(
      `could not install ${pkg}, which provider resolution asked for; ` +
        `build the image with --build-arg PRELOAD='${pkg}' to have it there ` +
        `already, or check this container can reach the npm registry`,
    );
  }
}

/** The provider package, fetched on demand if the image was not built with it. */
export async function importProvider(spec: string): Promise<Record<string, unknown>> {
  // Whether it is here is asked of the filesystem rather than by trying the
  // import and seeing. A specifier is resolved once per process and the answer
  // is kept -- "there is no such package" included -- so an attempt made before
  // installing would still be the answer afterwards.
  if (!existsSync(join(ROOT, "node_modules", packageName(spec)))) {
    await install(spec);
  }
  try {
    // By path, for the same reason: the resolution has to happen now.
    return (await import(Bun.resolveSync(spec, ROOT))) as Record<string, unknown>;
  } catch (error) {
    throw new LlmError(`could not load ${spec}: ${error instanceof Error ? error.message : error}`);
  }
}

/**
 * A provider package's factory.
 *
 * Every AI SDK provider exports one `create<Something>`, and that is the whole
 * of the convention this relies on. Where a package exports more than one --
 * an alias, usually -- the shortest name is the general one.
 */
export function factoryFrom(
  module: Record<string, unknown>,
  spec: string,
): (options: Record<string, unknown>) => unknown {
  const factories = Object.entries(module)
    .filter(([name, value]) => name.startsWith("create") && typeof value === "function")
    .sort(([a], [b]) => a.length - b.length || a.localeCompare(b));

  const chosen = factories[0];
  if (!chosen) {
    throw new LlmError(
      `${spec} exports no create* function, so it is not an AI SDK provider package`,
    );
  }
  return chosen[1] as (options: Record<string, unknown>) => unknown;
}

/** What a provider package hands back: something that can make a language model. */
export interface LoadedProvider {
  languageModel(id: string): unknown;
}

export interface Resolved {
  provider: LoadedProvider;
  /** The package that implemented it, for diagnostics. */
  npm: string;
  /** The address the config gave, if it gave one. Empty means the package default. */
  baseURL: string;
}

/** Load and configure the provider a config selects, ready to make a model. */
export async function resolveProvider(settings: Settings, id: string): Promise<Resolved> {
  const entry = await lookup(id);
  const spec = packageFor(settings, id, entry);
  const options: ProviderOptions = providerOptions(settings.config, id);
  const baseURL = typeof options.baseURL === "string" ? options.baseURL : "";

  if (spec === COMPATIBLE && !baseURL) {
    throw new LlmError(
      `provider '${id}' speaks the compatible protocol but the config does not ` +
        `say where it is -- give it provider.${id}.options.baseURL`,
    );
  }

  const built: Record<string, unknown> = {};
  if (baseURL) built.baseURL = baseURL;
  const apiKey = apiKeyFor(settings, id, entry);
  if (apiKey !== undefined) built.apiKey = apiKey;
  if (options.headers && typeof options.headers === "object") built.headers = options.headers;
  // Only the compatible package needs telling; it has no provider of its own to
  // name, and uses this for the model's reported provider id.
  if (spec === COMPATIBLE) built.name = id;

  const factory = factoryFrom(await importProvider(spec), spec);
  let provider: unknown;
  try {
    provider = factory(built);
  } catch (error) {
    // A provider package builds its client eagerly and rejects a setup it will
    // not accept here. Its own message says which field; a stack would not.
    throw new LlmError(`${id}: ${error instanceof Error ? error.message : error}`);
  }

  if (
    typeof provider !== "function" && typeof provider !== "object" ||
    provider === null ||
    typeof (provider as LoadedProvider).languageModel !== "function"
  ) {
    throw new LlmError(`${spec} did not return a provider with a languageModel()`);
  }
  return { provider: provider as LoadedProvider, npm: spec, baseURL };
}

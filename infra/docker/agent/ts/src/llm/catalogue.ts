/**
 * models.dev, which is how a provider id becomes something we can call.
 *
 * opencode resolves `provider/model` against this catalogue, and so does
 * everything here -- that agreement is the point. Each entry says which npm
 * package implements the provider and which environment variables it answers
 * to, which between them are the only two facts about a provider that anything
 * in localcode needs to know.
 *
 * The index is baked into the image at build time rather than committed, so
 * there is still no table of providers in the repository to go stale. It is
 * only fetched at runtime when the baked copy has never heard of the provider
 * in hand -- a provider newer than the image, in other words. Refreshing every
 * time would put a four-megabyte download in front of every question for no
 * gain, and a throwaway container has nowhere to cache it.
 */

export interface CatalogueEntry {
  id: string;
  name?: string;
  /** The npm package implementing this provider, e.g. `@ai-sdk/anthropic`. */
  npm?: string;
  /** The variables this provider's key is conventionally exported as. */
  env?: string[];
}

export type Catalogue = Record<string, CatalogueEntry>;

export const CATALOGUE_URL = "https://models.dev/api.json";

/** Written by the image build, at the package root above `src/llm`. */
export const SNAPSHOT = new URL("../../models.json", import.meta.url);

/** Long enough for a slow network, short enough not to hang a question on it. */
export const REFRESH_TIMEOUT_MS = 10_000;

export interface CatalogueSource {
  snapshot?: string | URL;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

async function readSnapshot(path: string | URL): Promise<Catalogue> {
  try {
    return (await Bun.file(path).json()) as Catalogue;
  } catch {
    // No snapshot, or an unreadable one. Not fatal: the network is still
    // there, and a config that names its own `npm` never needs either.
    return {};
  }
}

async function fetchCatalogue(source: CatalogueSource): Promise<Catalogue | null> {
  const get = source.fetchImpl ?? fetch;
  try {
    const response = await get(CATALOGUE_URL, {
      signal: AbortSignal.timeout(source.timeoutMs ?? REFRESH_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as Catalogue;
  } catch {
    // Offline, or models.dev is down. The caller falls back to what the config
    // says, which is the whole reason `npm` and `baseURL` are honoured first.
    return null;
  }
}

let baked: Promise<Catalogue> | undefined;
let fresh: Promise<Catalogue | null> | undefined;

/** Forget what has been loaded. Tests only; a container asks once and exits. */
export function reset(): void {
  baked = undefined;
  fresh = undefined;
}

/** What models.dev knows about one provider, or `null` if it has not heard of it. */
export async function lookup(
  id: string,
  source: CatalogueSource = {},
): Promise<CatalogueEntry | null> {
  baked ??= readSnapshot(source.snapshot ?? SNAPSHOT);
  const entry = (await baked)[id];
  if (entry) return entry;

  fresh ??= fetchCatalogue(source);
  return (await fresh)?.[id] ?? null;
}

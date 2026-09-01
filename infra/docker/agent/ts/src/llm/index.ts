/**
 * localcode's own model access, for the scripts in this image that want a model
 * without going through opencode.
 *
 * The whole of it, for most callers, is:
 *
 *     await streamToStdout(await selectModel(), { prompt });
 *
 * Everything under that -- which provider, which package implements it, where
 * it is and which key it takes -- is read out of the project's opencode config
 * and credentials, by the same rules opencode itself uses.
 */

export { LlmError, main } from "./errors.ts";
export {
  type Auth,
  type OpencodeConfig,
  type ProviderBlock,
  type ProviderOptions,
  type Settings,
  AUTH_ENV,
  CONFIG_ENV,
  expandEnv,
  loadSettings,
  providerBlock,
  providerOptions,
} from "./settings.ts";
export { type Catalogue, type CatalogueEntry, lookup } from "./catalogue.ts";
export {
  type LoadedProvider,
  type Resolved,
  COMPATIBLE,
  NO_KEY,
  apiKeyFor,
  credential,
  packageFor,
  packageName,
  resolveProvider,
} from "./provider.ts";
export {
  type SelectOptions,
  type Selection,
  declaredUnder,
  selectModel,
  splitReference,
} from "./model.ts";
export { type Sink, type StreamOptions, describe, streamTo, streamToStdout } from "./stream.ts";

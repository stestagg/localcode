import { describe, expect, test } from "bun:test";

import { LlmError } from "./errors.ts";
import {
  AUTH_ENV,
  CONFIG_ENV,
  expandEnv,
  loadSettings,
  providerBlock,
  providerOptions,
} from "./settings.ts";

describe("the config, as opencode writes it", () => {
  test("comments and a trailing comma are accepted, because opencode accepts them", () => {
    const { config } = loadSettings({
      [CONFIG_ENV]: `{
        // which model this project wants
        "model": "acme/a-model",
        /* and where to find it */
        "provider": { "acme": { "options": { "baseURL": "http://x/v1" } } },
      }`,
    });
    expect(config.model).toBe("acme/a-model");
    expect(providerOptions(config, "acme").baseURL).toBe("http://x/v1");
  });

  test("nothing configured is not an error, it is an empty config", () => {
    const settings = loadSettings({});
    expect(settings.config).toEqual({});
    expect(settings.auth).toEqual({});
    expect(loadSettings({ [CONFIG_ENV]: "   " }).config).toEqual({});
  });

  test("something that is not json says so, and says which variable", () => {
    expect(() => loadSettings({ [CONFIG_ENV]: "{ nope" })).toThrow(LlmError);
    expect(() => loadSettings({ [CONFIG_ENV]: "{ nope" })).toThrow(CONFIG_ENV);
  });

  test("a provider that was never configured reads as empty rather than throwing", () => {
    const { config } = loadSettings({ [CONFIG_ENV]: '{"model": "acme/m"}' });
    expect(providerBlock(config, "acme")).toEqual({});
    expect(providerOptions(config, "acme")).toEqual({});
  });

  test("the credential store is parsed but not understood", () => {
    const { auth } = loadSettings({ [AUTH_ENV]: '{"acme": {"type": "api", "key": "sk-1"}}' });
    expect(auth.acme).toEqual({ type: "api", key: "sk-1" });
  });
});

describe("{env:NAME}", () => {
  test("resolves against this container's environment", () => {
    expect(expandEnv("{env:KEY}", { KEY: "sk-1" })).toBe("sk-1");
    expect(expandEnv("Bearer {env:KEY}", { KEY: "sk-1" })).toBe("Bearer sk-1");
  });

  test("a variable the host did not forward expands to nothing, not to itself", () => {
    expect(expandEnv("{env:ABSENT}", {})).toBe("");
  });

  test("text that names no variable is left exactly as it is", () => {
    expect(expandEnv("sk-literal", {})).toBe("sk-literal");
  });
});

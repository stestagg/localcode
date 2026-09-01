import { describe, expect, test } from "bun:test";

import { LlmError } from "./errors.ts";
import { declaredUnder, selectModel, splitReference } from "./model.ts";
import { type Settings, CONFIG_ENV, loadSettings } from "./settings.ts";

describe("provider/model", () => {
  test("splits on the first slash", () => {
    expect(splitReference("acme/a-model")).toEqual({ provider: "acme", model: "a-model" });
  });

  test("a model id may itself contain slashes, and keeps them", () => {
    expect(splitReference("gateway/vendor/a-model")).toEqual({
      provider: "gateway",
      model: "vendor/a-model",
    });
  });

  test("half a reference is not one", () => {
    for (const bad of ["a-model", "acme/", "/a-model", "/"]) {
      expect(() => splitReference(bad)).toThrow(LlmError);
    }
  });
});

function settings(config: unknown): Settings {
  return loadSettings({ [CONFIG_ENV]: JSON.stringify(config) });
}

describe("selecting a model", () => {
  test("a config with no selection says where to make one", async () => {
    const attempt = selectModel({ settings: settings({}) });
    await expect(attempt).rejects.toThrow(LlmError);
    await expect(attempt).rejects.toThrow(/\.localcode\/opencode\.json/);
  });

  test("a malformed selection is refused before anything is loaded", async () => {
    await expect(selectModel({ settings: settings({ model: "a-model" }) })).rejects.toThrow(
      /provider\/model/,
    );
  });

  test("an override is resolved against the same config", async () => {
    // Nothing implements 'other', so resolution fails naming it -- which is
    // proof the override was used in place of the config's own selection.
    await expect(
      selectModel({ settings: settings({ model: "acme/a-model" }), model: "other/b-model" }),
    ).rejects.toThrow(/'other'/);
  });
});

describe("a selection missing its provider", () => {
  /** What `localcode llm configure-llamacpp` writes, plus a selection. */
  function local(model: string, served = model) {
    return {
      model,
      provider: {
        "llama.cpp": {
          npm: "@ai-sdk/openai-compatible",
          options: { baseURL: "http://localhost:9931/v1", apiKey: "" } as Record<string, unknown>,
          models: { [served]: { name: `${served} (local)` } },
        },
      },
    };
  }

  test("names the provider the config declares the model under", async () => {
    const attempt = selectModel({ settings: settings(local("acme/a-model-GGUF:IQ1_S")) });
    await expect(attempt).rejects.toThrow(/"model": "llama\.cpp\/acme\/a-model-GGUF:IQ1_S"/);
    // The failure it happened to fail with is kept: the suggestion is a
    // suggestion, and the reference really did select provider 'acme'.
    await expect(attempt).rejects.toThrow(/'acme'/);
  });

  test("a model id with no slash at all gets the same suggestion", async () => {
    await expect(selectModel({ settings: settings(local("a-model")) })).rejects.toThrow(
      /"model": "llama\.cpp\/a-model"/,
    );
  });

  test("a declared provider that fails is left to say why", async () => {
    // 'llama.cpp' is the config's own answer, so its failure is about the
    // provider rather than about how the model was named.
    const config = local("llama.cpp/acme/a-model-GGUF:IQ1_S", "acme/a-model-GGUF:IQ1_S");
    delete config.provider["llama.cpp"].options.baseURL;
    const message = await selectModel({ settings: settings(config) }).then(
      () => "it resolved",
      (error: Error) => error.message,
    );
    expect(message).toMatch(/baseURL/);
    expect(message).not.toMatch(/probably/);
  });
});

describe("declaredUnder", () => {
  const models = { "acme/a-model": {} };

  test("finds every provider listing the model, and nothing else", () => {
    const config = {
      provider: {
        one: { models },
        two: { models },
        three: { models: { "acme/b-model": {} } },
      },
    };
    expect(declaredUnder(config, "acme/a-model")).toEqual(["one", "two"]);
  });

  test("a provider block that is not shaped like one is not a match", () => {
    for (const block of [null, "llama.cpp", { models: "acme/a-model" }, {}]) {
      expect(declaredUnder({ provider: { one: block } } as never, "acme/a-model")).toEqual([]);
    }
    expect(declaredUnder({}, "acme/a-model")).toEqual([]);
  });
});

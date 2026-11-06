import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("../../../services/aiStrategyClients", () => ({ apiClient: client }));

import { getMiningAlgorithms, getMiningDatasets } from "./algorithmApi";

describe("factor mining algorithm directory", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps only server-validated algorithms for the supported A-share daily scope", async () => {
    client.get.mockResolvedValue({
      data: {
        data: {
          algorithms: [
            {
              key: "rdagent",
              name: "RD-Agent",
              enabled: true,
              valid: true,
              markets: ["a_share"],
              freqs: ["day"],
              uses_llm: true,
              uses_datasets: true,
              requires_gpu: false,
            },
            {
              key: "statistical",
              name: "统计派",
              enabled: true,
              valid: true,
              markets: ["a_share"],
              freqs: ["day"],
              uses_llm: false,
              uses_datasets: false,
              requires_gpu: false,
            },
            {
              key: "future",
              name: "Future",
              enabled: true,
              valid: true,
              markets: ["a_share"],
              freqs: ["day"],
              uses_llm: false,
              uses_datasets: false,
              requires_gpu: false,
            },
            {
              key: "rdagent",
              name: "Disabled",
              enabled: false,
              valid: true,
              markets: ["a_share"],
              freqs: ["day"],
              uses_llm: true,
              uses_datasets: true,
              requires_gpu: false,
            },
            {
              key: "statistical",
              name: "Minute",
              enabled: true,
              valid: true,
              markets: ["a_share"],
              freqs: ["1min"],
              uses_llm: false,
              uses_datasets: false,
              requires_gpu: false,
            },
          ],
        },
      },
    });

    const result = await getMiningAlgorithms();
    expect(client.get).toHaveBeenCalledWith("/alpha-agent/algorithms");
    expect(result.data?.available).toBe(true);
    expect(result.data?.algorithms.map((item) => item.key)).toEqual([
      "rdagent",
      "statistical",
    ]);
  });

  it("fails closed when the capability endpoint is unavailable", async () => {
    client.get.mockRejectedValue(new Error("offline"));
    const result = await getMiningAlgorithms();
    expect(result.data).toEqual({ algorithms: [], available: false });
  });

  it("loads only server-registered A-share daily mining datasets", async () => {
    client.get.mockResolvedValue({ data: { data: { datasets: [
      { key: "features_daily", name: "日线因子宽表", markets: ["a_share"], freqs: ["day"] },
      { key: "l1_factors", name: "L1 因子库", markets: ["a_share"], freqs: ["day"] },
      { key: "unsupported", name: "Unsupported", markets: ["us_stock"], freqs: ["day"] },
    ] } } });
    const result = await getMiningDatasets();
    expect(client.get).toHaveBeenCalledWith("/alpha-agent/datasets");
    expect(result.data?.datasets.map((item) => item.key)).toEqual([
      "features_daily", "l1_factors",
    ]);
  });
});

import type { HarnessCapability, HarnessId, Reasoning } from "../domain";

interface Props {
  harness: HarnessId;
  model: string;
  reasoning: Reasoning;
  capabilities: HarnessCapability[];
  onHarness?: (value: HarnessId) => void;
  onModel: (value: string) => void;
  onReasoning: (value: Reasoning) => void;
  showHarness?: boolean;
}
export function ModelControl({
  harness,
  model,
  reasoning,
  capabilities,
  onHarness,
  onModel,
  onReasoning,
  showHarness = true,
}: Props) {
  const capability = capabilities.find((item) => item.id === harness);
  const models = capability?.models ?? [];
  const selected = models.find((item) => item.id === model);
  const levels = selected?.reasoning ?? capability?.reasoning ?? ["default"];
  const effective = levels.includes(reasoning) ? reasoning : "default";
  return (
    <>
      {showHarness && (
        <label className="field">
          <span>Harness</span>
          <select
            aria-label="Harness"
            value={harness}
            onChange={(event) => onHarness?.(event.target.value as HarnessId)}
          >
            {capabilities.map((item) => (
              <option key={item.id} value={item.id} disabled={!item.available}>
                {item.label}
                {item.available ? "" : " · unavailable"}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="field">
        <span>Model</span>
        <select
          aria-label="Model"
          value={model}
          onChange={(event) => onModel(event.target.value)}
        >
          <option value="">Harness default</option>
          {model && !selected && <option value={model}>{model}</option>}
          {models.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Reasoning</span>
        <select
          aria-label="Reasoning"
          value={effective}
          onChange={(event) => onReasoning(event.target.value as Reasoning)}
        >
          {levels.map((level) => (
            <option key={level} value={level}>
              {level === "default" ? "Default" : level}
            </option>
          ))}
        </select>
      </label>
    </>
  );
}

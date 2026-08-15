import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { HarnessCapability, HarnessId, Reasoning } from "../domain";
import { Icon } from "./Icon";

interface SearchOption {
  value: string;
  label: string;
  iconName?: string;
  disabled?: boolean;
}

interface SearchableSelectProps {
  ariaLabel: string;
  value: string;
  options: SearchOption[];
  onChange: (value: string) => void;
  emptyOptionLabel?: string;
  loading: boolean;
  loadingLabel: string;
  emptyLabel: string;
  includeEmptyOption?: boolean;
  disabled?: boolean;
}

function SearchableSelect({
  ariaLabel,
  value,
  options,
  onChange,
  emptyOptionLabel,
  loading,
  loadingLabel,
  emptyLabel,
  includeEmptyOption = false,
  disabled = false,
}: SearchableSelectProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const menuOptions = useMemo(() => {
    if (loading) return [{ value: "", label: loadingLabel, disabled: true }];
    if (options.length === 0) {
      return includeEmptyOption
        ? [{ value: "", label: emptyOptionLabel ?? "Default" }]
        : [{ value: "", label: emptyLabel, disabled: true }];
    }
    return includeEmptyOption
      ? [{ value: "", label: emptyOptionLabel ?? "Default" }, ...options]
      : options;
  }, [emptyLabel, emptyOptionLabel, includeEmptyOption, loading, loadingLabel, options]);

  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return menuOptions.filter((option) => {
      if (option.disabled) return false;
      if (!normalized) return true;
      return `${option.label} ${option.value}`.toLowerCase().includes(normalized);
    });
  }, [menuOptions, query]);

  const selected = menuOptions.find((option) => option.value === value);
  const displayValue = loading ? loadingLabel : selected?.label ?? value;
  const selectedIconClass = selected?.iconName
    ? ` searchable-select-selected-${selected.iconName}`
    : "";

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  useEffect(() => {
    setActiveIndex((index) =>
      filteredOptions.length === 0 ? 0 : Math.min(index, filteredOptions.length - 1),
    );
  }, [filteredOptions.length]);

  const openPicker = () => {
    if (disabled || loading) return;
    setQuery("");
    setActiveIndex(0);
    setOpen(true);
  };

  const choose = (option: SearchOption) => {
    if (option.disabled) return;
    onChange(option.value);
    setQuery("");
    setOpen(false);
  };

  return (
    <div
      className={`searchable-select${open ? " open" : ""}${
        selected?.iconName && !open ? " has-selected-icon" : ""
      }`}
      ref={rootRef}
    >
      {selected?.iconName && !open && (
        <Icon
          name={selected.iconName}
          className={`searchable-select-selected-icon${selectedIconClass}`}
        />
      )}
      <input
        className={`searchable-select-input${selected?.iconName && !open ? " has-selected-icon" : ""}`}
        role="combobox"
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-controls={open ? listboxId : undefined}
        aria-expanded={open}
        aria-activedescendant={
          open && filteredOptions[activeIndex]
            ? `${listboxId}-option-${activeIndex}`
            : undefined
        }
        disabled={disabled || loading}
        readOnly={!open}
        value={open ? query : displayValue}
        placeholder={open ? `Search ${ariaLabel.toLowerCase()}…` : undefined}
        onFocus={openPicker}
        onClick={openPicker}
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
        }}
        onKeyDown={(event) => {
          if (!open) {
            if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
              event.preventDefault();
              openPicker();
            }
            return;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
          } else if (event.key === "ArrowDown" && filteredOptions.length > 0) {
            event.preventDefault();
            setActiveIndex((index) => (index + 1) % filteredOptions.length);
          } else if (event.key === "ArrowUp" && filteredOptions.length > 0) {
            event.preventDefault();
            setActiveIndex(
              (index) =>
                (index - 1 + filteredOptions.length) % filteredOptions.length,
            );
          } else if (event.key === "Enter" && filteredOptions[activeIndex]) {
            event.preventDefault();
            choose(filteredOptions[activeIndex]);
          }
        }}
      />
      <Icon name="chevron-down" className="searchable-select-chevron" />
      {open && (
        <div className="searchable-select-menu" id={listboxId} role="listbox">
          {filteredOptions.length > 0 ? (
            filteredOptions.map((option, index) => (
              <button
                type="button"
                role="option"
                id={`${listboxId}-option-${index}`}
                aria-selected={option.value === value}
                className={`searchable-select-option${
                  index === activeIndex ? " active" : ""
                }`}
                key={`${option.value}-${index}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(option)}
              >
                {option.iconName && (
                  <Icon
                    name={option.iconName}
                    className={`harness-option-icon harness-option-icon-${option.iconName}`}
                  />
                )}
                <span>{option.label}</span>
              </button>
            ))
          ) : (
            <div className="searchable-select-empty">No matches</div>
          )}
        </div>
      )}
    </div>
  );
}

interface Props {
  harness: HarnessId;
  model: string;
  reasoning: Reasoning;
  capabilities: HarnessCapability[];
  onHarness?: (value: HarnessId) => void;
  onModel: (value: string) => void;
  onReasoning: (value: Reasoning) => void;
  showHarness?: boolean;
  loading?: boolean;
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
  loading = false,
}: Props) {
  const capability = capabilities.find((item) => item.id === harness);
  const models = capability?.models ?? [];
  const selected = models.find((item) => item.id === model);
  const levels = selected?.reasoning ?? capability?.reasoning ?? ["default"];
  const effective = levels.includes(reasoning) ? reasoning : "default";
  const harnessIconById: Partial<Record<HarnessId, string>> = {
    codex: "brand-openai",
    claude: "brand-anthropic",
    opencode: "brand-opencode",
    pi: "brand-pi",
  };
  const harnessOptions = capabilities.map((item) => ({
    value: item.id,
    label: `${item.label}${item.available ? "" : " · unavailable"}`,
    iconName: harnessIconById[item.id],
    disabled: !item.available,
  }));
  const modelOptions = [
    ...(model && !selected ? [{ value: model, label: model }] : []),
    ...models.map((item) => ({ value: item.id, label: item.label })),
  ];

  return (
    <>
      {showHarness && (
        <label className="field">
          <span>Harness</span>
          <SearchableSelect
            ariaLabel="Harness"
            value={loading || capabilities.length === 0 ? "" : harness}
            options={harnessOptions}
            loading={loading}
            loadingLabel="Loading harnesses…"
            emptyLabel="No harnesses detected"
            disabled={capabilities.length === 0}
            onChange={(value) => onHarness?.(value as HarnessId)}
          />
        </label>
      )}
      <label className="field">
        <span>Model</span>
        <SearchableSelect
          ariaLabel="Model"
          value={loading ? "" : model}
          options={modelOptions}
          loading={loading}
          loadingLabel="Loading models…"
          emptyLabel="No models detected"
          emptyOptionLabel="Harness default"
          includeEmptyOption
          onChange={onModel}
        />
      </label>
      <label className="field">
        <span>Reasoning</span>
        <SearchableSelect
          ariaLabel="Reasoning"
          value={effective}
          options={levels.map((level) => ({
            value: level,
            label: level === "default" ? "Default" : level,
          }))}
          loading={loading}
          loadingLabel="Loading reasoning…"
          emptyLabel="No reasoning levels detected"
          onChange={(value) => onReasoning(value as Reasoning)}
        />
      </label>
    </>
  );
}

import { useState } from "react";
import { ArrowLeft, Check } from "@phosphor-icons/react";

import { updateProfile } from "../api.js";
import { companions } from "../lib/companions.js";
import { MascotVisual } from "./Mascot.jsx";

export function CompanionSwitcher({ userId, currentCompanionId, onBack, onComplete }) {
  const [selectedId, setSelectedId] = useState(currentCompanionId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selected = companions.find((companion) => companion.id === selectedId);

  const save = async () => {
    if (!selected || busy) return;
    setBusy(true);
    setError("");
    try {
      const saved = await updateProfile(userId, { companion_id: selected.id });
      onComplete(saved || { companion_id: selected.id });
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "陪伴方式没有保存，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="screen companion-switcher" aria-label="更换陪伴方式">
      <header className="companion-switcher-header">
        <button type="button" className="icon-button" aria-label="返回数字心智" onClick={onBack}>
          <ArrowLeft size={21} weight="bold" />
        </button>
        <div>
          <p className="eyebrow">互动模式</p>
          <h1>更换陪伴方式</h1>
        </div>
      </header>

      <div className="companion-switcher-scroll">
        <p className="companion-switcher-intro">
          只调整回应语气、回复长短和追问深浅，不需要重录声纹，也不会改变数字分身。
        </p>
        <p className="companion-switcher-note">
          当前会话不变；保存后从下一次会话生效。
        </p>

        <div className="companion-switcher-list" role="radiogroup" aria-label="选择陪伴方式">
          {companions.map((companion) => {
            const selected = companion.id === selectedId;
            return (
              <button
                type="button"
                className="companion-switcher-option"
                data-selected={selected}
                role="radio"
                aria-checked={selected}
                aria-label={`选择${companion.name}，${companion.tagline}`}
                key={companion.id}
                onClick={() => setSelectedId(companion.id)}
              >
                <MascotVisual
                  companionId={companion.id}
                  emotion={selected ? "happy" : "neutral"}
                  className="companion-switcher-mascot"
                />
                <span>
                  <strong>{companion.name}</strong>
                  <small>{companion.tagline}</small>
                  <em>{companion.description}</em>
                </span>
                {selected && <Check size={21} weight="bold" aria-hidden="true" />}
              </button>
            );
          })}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
      </div>

      <footer className="companion-switcher-footer">
        <button type="button" className="button-primary" onClick={() => void save()} disabled={busy}>
          {busy ? "正在保存…" : `保存${selected?.name || ""}的陪伴方式`}
        </button>
      </footer>
    </section>
  );
}

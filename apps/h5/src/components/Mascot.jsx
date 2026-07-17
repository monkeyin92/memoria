import { useEffect, useMemo, useState } from "react";

const assets = {
  neutral: "mascot-neutral.webp",
  happy: "mascot-happy.webp",
  curious: "mascot-curious.webp",
  upset: "mascot-upset.webp",
};

export function Mascot({ emotion, uiState, onActivate, disabled }) {
  const [talkPhase, setTalkPhase] = useState(false);
  const stateEmotion = uiState === "thinking" ? "curious" : emotion;
  const activeEmotion = useMemo(() => {
    if (uiState !== "speaking" || !talkPhase) return stateEmotion;
    return stateEmotion === "neutral" ? "happy" : "neutral";
  }, [stateEmotion, talkPhase, uiState]);

  useEffect(() => {
    if (uiState !== "speaking") {
      setTalkPhase(false);
      return undefined;
    }
    const timer = window.setInterval(
      () => setTalkPhase((current) => !current),
      460,
    );
    return () => window.clearInterval(timer);
  }, [uiState]);

  return (
    <button
      type="button"
      className={`mascot-button mascot-${uiState}`}
      aria-label={disabled ? "正在连接" : "轻触吉祥物开始实时对话"}
      disabled={disabled}
      onClick={onActivate}
    >
      <span className="mascot-stage" aria-hidden="true">
        {Object.entries(assets).map(([name, file]) => (
          <img
            key={name}
            className={`mascot-image ${activeEmotion === name ? "is-visible" : ""}`}
            src={`${import.meta.env.BASE_URL}assets/${file}`}
            alt=""
            draggable="false"
          />
        ))}
        <span className="chest-pulse" />
      </span>
    </button>
  );
}

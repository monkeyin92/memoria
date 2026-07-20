import { useEffect, useRef, useState } from "react";

const assets = {
  neutral: "mascot-neutral.webp",
  happy: "mascot-happy.webp",
  curious: "mascot-curious.webp",
  upset: "mascot-upset.webp",
};

/** Soft crossfade so expression changes feel continuous, not a hard cut. */
const EMOTION_CROSSFADE_MS = 560;

export function Mascot({ emotion, uiState, onActivate, disabled }) {
  // Thinking uses curious face; speaking keeps the semantic emotion (no mouth cut).
  const targetEmotion = uiState === "thinking" ? "curious" : emotion;
  const [visibleEmotion, setVisibleEmotion] = useState(targetEmotion);
  // Keep previous frame under the new one until fade completes → true dissolve.
  const [fadingOut, setFadingOut] = useState(null);
  const visibleRef = useRef(visibleEmotion);
  visibleRef.current = visibleEmotion;

  useEffect(() => {
    if (targetEmotion === visibleRef.current) return undefined;
    setFadingOut(visibleRef.current);
    setVisibleEmotion(targetEmotion);
    const timer = window.setTimeout(() => {
      setFadingOut(null);
    }, EMOTION_CROSSFADE_MS);
    return () => window.clearTimeout(timer);
  }, [targetEmotion]);

  return (
    <button
      type="button"
      className={`mascot-button mascot-${uiState}`}
      aria-label={disabled ? "正在连接" : "轻触吉祥物开始实时对话"}
      disabled={disabled}
      onClick={onActivate}
      style={{ "--mascot-crossfade-ms": `${EMOTION_CROSSFADE_MS}ms` }}
    >
      <span className="mascot-stage" aria-hidden="true">
        {Object.entries(assets).map(([name, file]) => {
          const isFront = name === visibleEmotion;
          const isBack = name === fadingOut;
          const layerClass = [
            "mascot-image",
            isFront ? "is-visible is-front" : "",
            isBack ? "is-fading-out" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <img
              key={name}
              className={layerClass}
              src={`${import.meta.env.BASE_URL}assets/${file}`}
              alt=""
              draggable="false"
            />
          );
        })}
        <span className="chest-pulse" />
      </span>
    </button>
  );
}

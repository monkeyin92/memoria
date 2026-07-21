import { useId } from "react";

import { companionById, defaultCompanionId } from "../lib/companions.js";

const expressions = new Set(["neutral", "happy", "curious", "caring"]);

function OpenEye({ x, gradientId }) {
  return (
    <g transform={`translate(${x} 63)`}>
      <g className="mascot-eye-open">
        <ellipse className="mascot-eye-outline" cx="0" cy="1" rx="22" ry="29" />
        <ellipse cx="0" cy="0" rx="19" ry="26" fill={`url(#${gradientId})`} />
        <ellipse className="mascot-eye-glow" cx="0" cy="17" rx="12" ry="5" />
        <circle className="mascot-eye-shine" cx="-7" cy="-10" r="5" />
        <circle className="mascot-eye-shine-small" cx="7" cy="2" r="2.4" />
      </g>
    </g>
  );
}

function OpenEyes({ gradientId }) {
  return (
    <g className="mascot-open-eyes">
      <OpenEye x={72} gradientId={gradientId} />
      <OpenEye x={168} gradientId={gradientId} />
    </g>
  );
}

function expressionFor(emotion) {
  return expressions.has(emotion) ? emotion : "neutral";
}

export function MascotVisual({
  companionId = defaultCompanionId,
  emotion = "neutral",
  uiState = "idle",
  className = "",
  ariaLabel,
}) {
  const companion = companionById(companionId);
  const gradientId = useId().replaceAll(":", "");
  const expression = expressionFor(emotion);
  const faceStyle = {
    "--face-left": companion.face.left,
    "--face-top": companion.face.top,
    "--face-width": companion.face.width,
    "--face-height": companion.face.height,
    "--face-ink": companion.face.ink,
    "--eye-top": companion.face.eyeTop,
    "--eye-bottom": companion.face.eyeBottom,
    "--eye-glow": companion.face.glow,
    "--chest-top": companion.chest?.top || "74%",
  };

  return (
    <span
      className={`mascot-visual mascot-${uiState} ${className}`.trim()}
      data-companion={companion.id}
      data-expression={expression}
      data-face-tone={companion.face.tone}
      style={faceStyle}
      role={ariaLabel ? "img" : undefined}
      aria-label={ariaLabel}
      aria-hidden={ariaLabel ? undefined : "true"}
    >
      <span className="mascot-stage">
        <span className="mascot-body-shell">
          <img
            className="mascot-body"
            src={companion.image}
            alt=""
            draggable="false"
          />
          <svg
            className="mascot-face"
            viewBox="0 0 240 140"
            focusable="false"
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="var(--eye-top)" />
                <stop offset="0.64" stopColor="var(--eye-top)" />
                <stop offset="1" stopColor="var(--eye-bottom)" />
              </linearGradient>
            </defs>

            <g className={`face-expression ${expression === "neutral" ? "is-visible" : ""}`}>
              <path className="face-brow" d="M52 29 Q72 20 92 29" />
              <path className="face-brow" d="M148 29 Q168 20 188 29" />
              <OpenEyes gradientId={gradientId} />
              <path className="face-mouth" d="M108 108 Q120 118 132 108" />
            </g>

            <g className={`face-expression ${expression === "happy" ? "is-visible" : ""}`}>
              <path className="face-brow" d="M52 32 Q72 23 92 31" />
              <path className="face-brow" d="M148 31 Q168 23 188 32" />
              <path className="happy-eye" d="M51 69 Q72 47 93 69" />
              <path className="happy-eye" d="M147 69 Q168 47 189 69" />
              <g className="face-mouth happy-mouth">
                <path
                  className="happy-mouth-fill"
                  d="M102 93 Q120 101 138 93 Q136 120 120 122 Q104 120 102 93Z"
                />
                <path className="happy-tongue" d="M108 113 Q120 104 132 113 Q128 120 120 120 Q112 120 108 113Z" />
              </g>
            </g>

            <g className={`face-expression face-curious ${expression === "curious" ? "is-visible" : ""}`}>
              <path className="face-brow" d="M50 28 Q70 17 91 25" />
              <path className="face-brow" d="M149 35 Q169 28 188 36" />
              <OpenEyes gradientId={gradientId} />
              <ellipse className="curious-mouth face-mouth" cx="120" cy="109" rx="5" ry="7" />
            </g>

            <g className={`face-expression ${expression === "caring" ? "is-visible" : ""}`}>
              <path className="face-brow" d="M52 36 Q73 34 92 21" />
              <path className="face-brow" d="M148 21 Q167 34 188 36" />
              <OpenEyes gradientId={gradientId} />
              <path className="face-mouth" d="M108 113 Q120 101 132 113" />
            </g>
          </svg>
          {companion.chest && <span className="chest-pulse" />}
        </span>
      </span>
    </span>
  );
}

export function Mascot({
  emotion,
  uiState,
  onActivate,
  disabled,
  active = false,
  companionId = defaultCompanionId,
}) {
  const expression = expressionFor(emotion);
  const companion = companionById(companionId);
  const canActivate = !disabled && !active;
  const label = disabled
    ? "正在连接"
    : active
      ? `${companion.name}正在陪伴`
      : `轻触${companion.name}开始实时对话`;

  return (
    <button
      type="button"
      className={`mascot-button mascot-${uiState}`}
      aria-label={label}
      disabled={!canActivate}
      onClick={canActivate ? onActivate : undefined}
      data-expression={expression}
    >
      <MascotVisual
        companionId={companionId}
        emotion={emotion}
        uiState={uiState}
      />
    </button>
  );
}

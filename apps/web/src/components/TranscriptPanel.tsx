import type { TranscriptLine } from "../state/sessionStore";

export function TranscriptPanel({ lines }: { lines: TranscriptLine[] }) {
  return (
    <div className="transcripts" aria-live="polite">
      {lines.map((line, idx) => (
        <div
          key={`${line.speaker}-${line.generation_id}-${idx}`}
          className={`line ${line.speaker} ${line.final ? "final" : "interim"}`}
        >
          <span className="who">{line.speaker === "user" ? "你" : "助手"}</span>
          <span className="text">{line.text}</span>
        </div>
      ))}
    </div>
  );
}

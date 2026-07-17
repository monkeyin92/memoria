import { UI_STATE_LABELS, type UiState } from "../types/events";

export function ConnectionBanner({
  state,
  error,
}: {
  state: UiState;
  error: string | null;
}) {
  return (
    <div className="banner" role="status">
      <strong>{UI_STATE_LABELS[state]}</strong>
      {error ? <span className="error"> — {error}</span> : null}
    </div>
  );
}

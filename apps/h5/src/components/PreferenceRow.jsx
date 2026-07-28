export function PreferenceRow({
  Icon,
  title,
  caption,
  checked,
  onToggle,
  disabled = false,
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`preference-row ${disabled ? "disabled" : ""}`}
      onClick={onToggle}
      disabled={disabled}
    >
      <span className="preference-icon"><Icon size={21} weight="fill" /></span>
      <span className="preference-copy"><strong>{title}</strong><small>{caption}</small></span>
      <span className={`switch ${checked ? "on" : ""}`} aria-hidden="true"><i /></span>
    </button>
  );
}

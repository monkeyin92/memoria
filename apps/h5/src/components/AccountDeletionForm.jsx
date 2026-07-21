import { useState } from "react";
import { Trash, WarningCircle } from "@phosphor-icons/react";

import { deleteAccountData } from "../api.js";

export const accountDeletionPhrase = "永久删除我的全部数据";

function errorMessage(error) {
  return error instanceof Error && error.message
    ? error.message
    : "账户注销没有完成，请检查密码后重试。";
}

export function AccountDeletionForm({
  autoFocus = false,
  onBusyChange,
  onDeleted,
  submitLabel = "永久删除全部数据",
}) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const deleteAccount = async () => {
    if (!password || confirmation !== accountDeletionPhrase || busy) return;
    setBusy(true);
    onBusyChange?.(true);
    setError("");
    try {
      await deleteAccountData(password, confirmation);
    } catch (actionError) {
      setError(errorMessage(actionError));
      setBusy(false);
      onBusyChange?.(false);
      return;
    }
    await onDeleted?.();
  };

  return (
    <form
      className="account-data-form account-danger-zone"
      onSubmit={(event) => {
        event.preventDefault();
        void deleteAccount();
      }}
    >
      <strong>永久注销账号</strong>
      <p>
        此操作不可撤销。账号、对话、回顾、人格、声纹和声音样本都将永久删除。
      </p>
      <label htmlFor="account-delete-password">删除验证密码</label>
      <input
        id="account-delete-password"
        type="password"
        autoComplete="current-password"
        autoFocus={autoFocus}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <label htmlFor="account-delete-confirmation">
        输入“{accountDeletionPhrase}”
      </label>
      <input
        id="account-delete-confirmation"
        type="text"
        autoComplete="off"
        spellCheck="false"
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
      />
      <button
        type="submit"
        className="button-danger full-width"
        disabled={!password || confirmation !== accountDeletionPhrase || busy}
      >
        <Trash size={18} aria-hidden="true" />
        {busy ? "正在永久删除…" : submitLabel}
      </button>
      {error && (
        <div className="account-deletion-error" role="alert">
          <WarningCircle size={18} weight="fill" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}
    </form>
  );
}

export const DEFAULT_NETWORK_TIMEOUT_MS = 10_000;

function abortError(reason) {
  if (reason instanceof Error) return reason;
  const error = new Error("请求已取消");
  error.name = "AbortError";
  return error;
}

export function withAbortTimeout(
  operation,
  {
    timeoutMs = DEFAULT_NETWORK_TIMEOUT_MS,
    message = "请求超时，请稍后重试",
    signal: parentSignal = null,
  } = {},
) {
  if (parentSignal?.aborted) {
    return Promise.reject(abortError(parentSignal.reason));
  }
  const controller = new AbortController();
  let timer = null;
  let rejectParentAbort = null;
  const onParentAbort = () => {
    controller.abort(parentSignal.reason);
    rejectParentAbort?.(abortError(parentSignal.reason));
  };
  const parentAbort = parentSignal
    ? new Promise((_, reject) => {
        rejectParentAbort = reject;
        parentSignal.addEventListener("abort", onParentAbort, { once: true });
      })
    : null;
  const timeout =
    Number.isFinite(timeoutMs) && timeoutMs > 0
      ? new Promise((_, reject) => {
          timer = globalThis.setTimeout(() => {
            const error = new Error(message);
            error.name = "TimeoutError";
            reject(error);
            controller.abort(error);
          }, timeoutMs);
        })
      : null;
  const pending = [Promise.resolve().then(() => operation(controller.signal))];
  if (timeout) pending.push(timeout);
  if (parentAbort) pending.push(parentAbort);
  return Promise.race(pending).finally(() => {
    if (timer !== null) globalThis.clearTimeout(timer);
    parentSignal?.removeEventListener("abort", onParentAbort);
  });
}

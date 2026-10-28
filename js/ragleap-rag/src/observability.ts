export type EventHandler<T = Record<string, unknown>> = (
  event: T,
) => void;

/**
 * Invoke handlers in order. Hook errors are logged and swallowed so
 * instrumentation does not interrupt the operation being observed.
 */
export function fire_event<T>(
  event: T,
  handlers: readonly EventHandler<T>[] | null | undefined,
  hook_name: string,
): void {
  if (!handlers || handlers.length === 0) return;

  for (const handler of handlers) {
    try {
      handler(event);
    } catch (error) {
      console.warn(
        `Observability hook '${hook_name}' raised an exception (swallowed):`,
        error,
      );
    }
  }
}

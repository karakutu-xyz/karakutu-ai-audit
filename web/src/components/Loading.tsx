export function Loading({ children = "Loading…" }: { children?: string }) {
  return (
    <p className="loading" role="status">
      {children}
    </p>
  );
}

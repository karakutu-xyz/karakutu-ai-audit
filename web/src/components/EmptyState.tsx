import type { ReactNode } from "react";
import { Icon } from "./Icon";

export function EmptyState({
  title,
  children,
  icon = "grid",
}: {
  title: string;
  children: ReactNode;
  icon?: Parameters<typeof Icon>[0]["name"];
}) {
  return (
    <div className="empty">
      <Icon name={icon} size={24} />
      <h2>{title}</h2>
      <div>{children}</div>
    </div>
  );
}

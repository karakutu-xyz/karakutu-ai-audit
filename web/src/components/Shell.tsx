import type { ReactNode } from "react";
import { useServiceStatus } from "../api/hooks";
import { Link } from "../router";
import { ModePill } from "./ModePill";
import { QueueIndicator } from "./QueueIndicator";
import { TopNav } from "./TopNav";

export function Shell({ children }: { children: ReactNode }) {
  const { status } = useServiceStatus();
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <Link href="/articles" className="wordmark">
            ▣ karakutu
          </Link>
          <TopNav />
          <QueueIndicator />
          <ModePill status={status} />
        </div>
      </header>
      <main id="main" tabIndex={-1}>
        {children}
      </main>
    </>
  );
}

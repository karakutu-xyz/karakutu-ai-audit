import type { ReactNode } from "react";

type IconName =
  | "grid"
  | "flow"
  | "plus"
  | "arrow"
  | "check"
  | "close"
  | "refresh"
  | "code"
  | "download"
  | "settings"
  | "search"
  | "flag"
  | "thumb"
  | "clock";

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
      </>
    ),
    flow: (
      <>
        <rect x="8" y="2" width="8" height="5" rx="1.5" />
        <rect x="2" y="17" width="7" height="5" rx="1.5" />
        <rect x="15" y="17" width="7" height="5" rx="1.5" />
        <path d="M12 7v5M5.5 17v-5h13v5" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="m9 5 7 7-7 7" />,
    check: <path d="m5 12 4 4L19 6" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 11.5-2L20 8M4 16l2.5 3A7 7 0 0 0 18 17" />
      </>
    ),
    code: (
      <>
        <path d="m7 7-5 5 5 5m10-10 5 5-5 5M14 3l-4 18" />
      </>
    ),
    download: (
      <>
        <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />
      </>
    ),
    settings: (
      <>
        <path d="M4 7h16M4 17h16" />
        <circle cx="8" cy="7" r="3" />
        <circle cx="16" cy="17" r="3" />
      </>
    ),
    search: (
      <>
        <circle cx="10" cy="10" r="6" />
        <path d="m15 15 6 6" />
      </>
    ),
    flag: (
      <>
        <path d="M5 22V3m0 0c5-4 9 4 15 0v11c-6 4-10-4-15 0" />
      </>
    ),
    thumb: (
      <>
        <path d="M8 10 12 2c4 0 2 6 2 6h5a2 2 0 0 1 2 2l-2 10H8z" />
        <path d="M3 10h5v10H3z" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 6v6l4 2" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}

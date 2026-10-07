import type { ReactNode } from "react";

const paths = {
  dashboard: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
  folder: <path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z" />,
  connect: <><path d="m8 8-5 4 5 4m8-8 5 4-5 4m-3-11-2 14" /></>,
  key: <><circle cx="8" cy="8" r="5" /><path d="m12 12 9 9m-4-4 3-3m-6 0 3-3" /></>,
  history: <><path d="M3 11a9 9 0 1 1 2.6 7.4M3 4v7h7m2-4v5l3 2" /></>,
  settings: <><path d="M4 7h16M4 17h16" /><rect x="7" y="4" width="4" height="6" fill="currentColor" stroke="none" /><rect x="14" y="14" width="4" height="6" fill="currentColor" stroke="none" /></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Zm-4 9 3 3 5-6" /></>,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></>,
  logout: <><path d="M9 4H4v16h5m-1-8h13m-4-4 4 4-4 4" /></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  chevron: <path d="m9 5 7 7-7 7" />,
  activity: <path d="M2 12h5l3-8 4 16 3-8h5" />,
  cpu: <><rect x="5" y="5" width="14" height="14" /><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4" /><rect x="9" y="9" width="6" height="6" /></>,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof paths;

export function Icon({ name, className = "h-5 w-5" }: { name: IconName; className?: string }) {
  return (
    <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 ${className}`}>
      {paths[name]}
    </svg>
  );
}

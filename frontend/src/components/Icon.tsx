import type { ReactNode, SVGProps } from "react";

type IconName =
  | "aperture"
  | "check"
  | "chevron"
  | "image"
  | "lock"
  | "plus"
  | "spark"
  | "trash"
  | "warning"
  | "wave";

const paths: Record<IconName, ReactNode> = {
  aperture: <><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5 8.8 9M4.6 8.2l6.3.1m-7.4 5.1L8.7 10m.7 10 3-5.5m7-1.8-6.3-.1m6.5-5-5.2 3.5"/></>,
  check: <path d="m5 12.5 4 4L19 6.8" />,
  chevron: <path d="m8.5 10 3.5 3.5 3.5-3.5" />,
  image: <><rect x="3.5" y="4" width="17" height="16" rx="1.5"/><circle cx="9" cy="9" r="1.5"/><path d="m5 17 4.5-4.5 3 3 2-2 4 3.5"/></>,
  lock: <><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></>,
  plus: <path d="M12 5v14M5 12h14" />,
  spark: <><path d="m12 2 1.5 5.1L18 9l-4.5 1.9L12 16l-1.5-5.1L6 9l4.5-1.9L12 2Z"/><path d="m18.5 15 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7.7-2.3Z"/></>,
  trash: <><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></>,
  warning: <><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5m0 3v.1"/></>,
  wave: <><path d="M3 9v6M7 6v12m4-15v18m4-15v12m4-9v6"/></>,
};

export function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="20"
      viewBox="0 0 24 24"
      width="20"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.6"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}

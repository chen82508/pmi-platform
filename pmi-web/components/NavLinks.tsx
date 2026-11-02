"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/pmi_dashboard", label: "Indexes", exact: false, not: "/pmi_dashboard/health" },
  { href: "/pmi_dashboard/health", label: "Health", exact: true, not: null },
];

export function NavLinks() {
  const path = usePathname();
  return (
    <nav className="flex items-center gap-1 text-sm">
      {LINKS.map(({ href, label, exact, not }) => {
        const active = exact ? path === href : path.startsWith(href) && path !== not;
        return (
          <Link
            key={href}
            href={href}
            className={`rounded-md px-3 py-1.5 transition-colors ${
              active ? "bg-white/10 text-white" : "text-white/65 hover:text-white"
            }`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

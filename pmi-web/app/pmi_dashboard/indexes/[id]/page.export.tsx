// Static export entry for /pmi_dashboard/indexes/[id]; see index-page.tsx.
import { api } from "@/lib/api-client";

export async function generateStaticParams(): Promise<{ id: string }[]> {
  return (await api.listIndexes()).map((m) => ({ id: m.id }));
}

export { default } from "./index-page";

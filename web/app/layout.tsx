import type { Metadata } from "next";
import "./globals.css";
import { MeProvider } from "@/components/Auth";
import { DemoBanner } from "@/components/DemoBanner";
import { Nav } from "@/components/Nav";
import { currentUser } from "@/lib/server";

export const metadata: Metadata = {
  title: "FeatureLedger",
  description: "Feature-level P&L and Safe Deprecation Radar",
};

export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const me = await currentUser();
  return (
    <html lang="en">
      <body>
        <MeProvider me={me}>
          {me ? (
            <div className="flex min-h-screen">
              <Nav />
              <div className="min-w-0 flex-1">
                <DemoBanner />
                <main className="px-8 py-7 print:p-0">{children}</main>
              </div>
            </div>
          ) : (
            <main className="min-h-screen">{children}</main>
          )}
        </MeProvider>
      </body>
    </html>
  );
}

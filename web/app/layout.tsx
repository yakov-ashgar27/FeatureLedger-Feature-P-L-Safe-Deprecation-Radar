import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";

export const metadata: Metadata = {
  title: "FeatureLedger",
  description: "Feature-level P&L and Safe Deprecation Radar",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen">
          <Nav />
          <main className="min-w-0 flex-1 px-8 py-7">{children}</main>
        </div>
      </body>
    </html>
  );
}

/**
 * Root Layout — Wraps all pages.
 *
 * This is a Server Component (default in Next.js 14 App Router).
 * It sets up the HTML shell, global styles, and fonts.
 */
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "NL SQL Agent",
  description: "AI-powered SQL agent — ask questions in plain English",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}

import type {Metadata} from "next";
import {Geist, Source_Serif_4} from "next/font/google";
import {ThemeProvider} from "next-themes";
import {SiteHeader} from "@/components/site-header";
import {SiteFooter} from "@/components/site-footer";
import "./globals.css";

const defaultUrl = process.env.VERCEL_URL
    ? `https://${process.env.VERCEL_URL}`
    : "http://localhost:3000";

export const metadata: Metadata = {
    metadataBase: new URL(defaultUrl),
    title: "GlyphScholar",
    description: "A hybrid RAG + PixelRAG system",
};

const geistSans = Geist({
    variable: "--font-geist-sans",
    display: "swap",
    subsets: ["latin"],
});

// Display serif for the marketing/landing page — reserved for headline-scale
// text; body copy and the app UI stay on Geist Sans.
const sourceSerif = Source_Serif_4({
    variable: "--font-source-serif",
    display: "swap",
    subsets: ["latin"],
});

export default function RootLayout({
                                       children,
                                   }: Readonly<{
    children: React.ReactNode;
}>) {
    return (
        <html lang="en" suppressHydrationWarning>
        <body
            className={`${geistSans.className} ${sourceSerif.variable} antialiased`}
        >
        <ThemeProvider
            attribute="class"
            defaultTheme="system"
            enableSystem
            disableTransitionOnChange
        >
            <div className="grid min-h-svh grid-rows-[auto_1fr_auto]">
                <SiteHeader/>
                <div className="min-h-0">{children}</div>
                <SiteFooter/>
            </div>
        </ThemeProvider>
        </body>
        </html>
    );
}
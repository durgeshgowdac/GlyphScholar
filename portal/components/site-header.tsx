"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AuthLogo } from "@/components/auth-logo";
import { ThemeSwitcher } from "@/components/theme-switcher";

const GITHUB_URL = "https://github.com/durgeshgowdac/GlyphScholar";

export function SiteHeader() {
    const pathname = usePathname();
    const isAuthRoute = pathname?.startsWith("/auth");

    return (
        <header className="border-b">
            <div className="mx-auto flex w-full items-center justify-between px-8 py-4">
                <Link href="/" className="w-fit">
                    <AuthLogo />
                </Link>
                <div className="flex items-center gap-1">
                    <Button variant="ghost" size="icon" asChild>
                        <Link
                            href={GITHUB_URL}
                            target="_blank"
                            rel="noreferrer noopener"
                            aria-label="GlyphScholar on GitHub"
                        >
                            <Github className="h-4 w-4" />
                        </Link>
                    </Button>
                    <ThemeSwitcher />
                    {!isAuthRoute && (
                        <div className="ml-2 flex items-center gap-2">
                            <Button variant="ghost" asChild>
                                <Link href="/auth/login">Log in</Link>
                            </Button>
                            <Button asChild>
                                <Link href="/auth/sign-up">Create an account</Link>
                            </Button>
                        </div>
                    )}
                </div>
            </div>
        </header>
    );
}
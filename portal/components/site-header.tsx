"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AuthLogo } from "@/components/auth-logo";
import { ThemeSwitcher } from "@/components/theme-switcher";

const GITHUB_URL = "https://github.com/durgeshgowdac/GlyphScholar";
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

export function SiteHeader() {
    const pathname = usePathname();
    const isAuthRoute = pathname?.startsWith("/auth");
    const [loggedIn, setLoggedIn] = useState(false);

    useEffect(() => {
        fetch("/auth/check", { credentials: "include" })
            .then((res) => setLoggedIn(res.ok))
            .catch(() => setLoggedIn(false));
    }, [pathname]);

    return (
        <header className="border-b">
            <div className="mx-auto flex w-full items-center justify-between px-8 py-4">
                <Link href="/" className="w-fit">
                    <AuthLogo />
                </Link>
                <div className="flex items-center gap-1">
                    <Button variant="ghost" size="icon" asChild>
                        <Link href={GITHUB_URL} target="_blank" rel="noreferrer noopener" aria-label="GlyphScholar on GitHub">
                            <Github className="h-4 w-4" />
                        </Link>
                    </Button>
                    <ThemeSwitcher />
                    {!isAuthRoute && (
                        <div className="ml-2 flex items-center gap-2">
                            {loggedIn ? (
                                <Button variant="ghost" asChild>
                                    <a href={`${BACKEND_URL}/auth/logout`}>Log out</a>
                                </Button>
                            ) : (
                                <>
                                    <Button variant="ghost" asChild>
                                        <Link href="/auth/login">Log in</Link>
                                    </Button>
                                    <Button asChild>
                                        <Link href="/auth/sign-up">Create an account</Link>
                                    </Button>
                                </>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </header>
    );
}
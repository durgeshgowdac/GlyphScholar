"use client";

import { cn } from "@/lib/utils";
import { createClient } from "@/lib/supabase/client";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { Label } from "@/components/ui/label";
import Link from "next/link";
import { useState, useEffect } from "react";
// AuthLogo is no longer rendered here — the /auth layout shell
// (app/auth/layout.tsx) carries the wordmark now.

// Derive the backend URL at runtime from whatever hostname the browser is
// currently on — just swap the port to 8000.
// function getBackendUrl(): string {
//   if (typeof window === "undefined") return "http://127.0.0.1:8000";
//   const { protocol, hostname } = window.location;
//   return `${protocol}//${hostname}:8000`;
// }
function getBackendUrl(): string {
  return process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
}

export function LoginForm({
  className,
  ...props
}: React.ComponentPropsWithoutRef<"div">) {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [checking, setChecking] = useState(true); // NEW

  // On mount: if already authenticated, go straight to chat
  useEffect(() => {
    const checkSession = async () => {
      try {
        const probe = await fetch("/auth/check", {credentials: "include"});
        if (probe.ok) {
          const supabase = createClient();
          const {data: {user}} = await supabase.auth.getUser(); // server-verified, not cached
          if (user) {
            const { data: { session } } = await supabase.auth.getSession();
            if (session?.access_token) {
              const backendUrl = getBackendUrl();
              window.location.href = `${backendUrl}/auth/bridge?token=${session.access_token}`;
              return;
            }
          }
        }
      } catch {
        // backend unreachable, show login form
      } finally {
        setChecking(false);
      }
    };
    checkSession();
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    const supabase = createClient();
    const backendUrl = getBackendUrl();
    setIsLoading(true);
    setError(null);

    const normalizedIdentifier = identifier.trim().toLowerCase();

    try {
      let loginEmail = normalizedIdentifier;

      if (!normalizedIdentifier.includes("@")) {
        // Resolved server-side (backend holds the service_role key) rather
        // than via a public anon-callable RPC, so the lookup can't be hit
        // directly against Supabase's REST API and bypass rate limiting.
        const resolveRes = await fetch(`${backendUrl}/auth/resolve-login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ identifier: normalizedIdentifier }),
        });

        if (resolveRes.status === 429) {
          throw new Error("Too many attempts. Please wait a minute and try again.");
        }
        if (!resolveRes.ok) {
          throw new Error("Invalid username or password.");
        }

        const { email: emailFromUsername } = await resolveRes.json();
        if (!emailFromUsername) {
          throw new Error("Invalid username or password.");
        }

        loginEmail = emailFromUsername;
      }

      const { data, error: loginError } = await supabase.auth.signInWithPassword({
        email: loginEmail,
        password,
      });

      if (loginError) throw new Error("Invalid username/email or password.");

      const accessToken = data.session?.access_token;
      if (!accessToken) throw new Error("Could not establish a session.");

      window.location.href = `${backendUrl}/auth/bridge?token=${accessToken}`;

    } catch (err: unknown) {
      console.error("Login error:", err);
      setError(err instanceof Error ? err.message : "An unexpected error occurred.");
    } finally {
      setIsLoading(false);
    }
  };

  // Don't flash the login form while checking
  if (checking) {
    return <p className="text-sm text-muted-foreground">Loading...</p>;
  }

  return (
    <div className={cn("flex flex-col gap-6", className)} {...props}>
      <Card className="shadow-none">
        <CardHeader>
          <CardTitle
            className="text-2xl"
            style={{ fontFamily: "var(--font-source-serif)" }}
          >
            Login
          </CardTitle>
          <CardDescription>
            Enter your email or username below to login to your account
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleLogin}>
            <div className="flex flex-col gap-6">
              <div className="grid gap-2">
                <Label htmlFor="identifier">Email or Username</Label>
                <Input
                  id="identifier"
                  type="text"
                  placeholder="johndoe@example.com or johndoe"
                  autoComplete="username"
                  required
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <div className="flex items-center">
                  <Label htmlFor="password">Password</Label>
                  <Link
                    href="/auth/forgot-password"
                    className="ml-auto inline-block text-sm underline-offset-4 hover:underline"
                  >
                    Forgot your password?
                  </Link>
                </div>
                <PasswordInput
                  id="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              {error && <p className="text-sm text-red-500">{error}</p>}
              <Button type="submit" className="w-full" disabled={isLoading}>
                {isLoading ? "Logging in..." : "Login"}
              </Button>
            </div>
            <div className="mt-4 text-center text-sm">
              Don&apos;t have an account?{" "}
              <Link href="/auth/sign-up" className="underline underline-offset-4">
                Sign up
              </Link>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
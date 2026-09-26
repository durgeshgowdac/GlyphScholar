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
import { Label } from "@/components/ui/label";
import Link from "next/link";
import { useEffect, useState } from "react";

export function ForgotPasswordForm({
  className,
  ...props
}: React.ComponentPropsWithoutRef<"div">) {
  const [identifier, setIdentifier] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    setSuccess(false);
    setIdentifier("");
    setError(null);
  }, []);

  const handleForgotPassword = async (e: React.FormEvent) => {
        e.preventDefault();

        const supabase = createClient();

        setIsLoading(true);
        setError(null);

        const normalizedIdentifier = identifier.trim().toLowerCase();

        try {
          let resetEmail = normalizedIdentifier;

          // Username entered
          if (!normalizedIdentifier.includes("@")) {
            const { data: emailFromUsername } =
                await supabase.rpc("get_email_from_username", {
                  p_username: normalizedIdentifier,
                });

            if (!emailFromUsername) {
              setSuccess(true);
              return;
            }

            resetEmail = emailFromUsername;
          }

          // console.log("SITE_URL:", process.env.NEXT_PUBLIC_SITE_URL);
          // console.log("origin:", window.location.origin);

          const redirectTo = `${process.env.NEXT_PUBLIC_SITE_URL ?? window.location.origin}/auth/update-password`;
          // console.log("redirectTo:", redirectTo);

          await supabase.auth.resetPasswordForEmail(resetEmail, {
              redirectTo,
          });

          // await supabase.auth.resetPasswordForEmail(resetEmail, {
          //     redirectTo: "http://localhost:3000/auth/update-password",
          // });

          // Always show success
          setSuccess(true);


        } catch (error: unknown) {
          console.error("Password reset error:", error);

          // Still don't reveal anything
          setSuccess(true);

        } finally {
          setIsLoading(false);
        }
      };

  return (
    <div className={cn("flex flex-col gap-6", className)} {...props}>
      {success ? (
        <Card className="shadow-none">
          <CardHeader>
            <CardTitle
              className="text-2xl"
              style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Check Your Email
            </CardTitle>
            <CardDescription>Password reset instructions sent</CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              If you registered using your email and password, you will receive
              a password reset email.
            </p>
            <p className="text-sm text-muted-foreground">
              Back to {" "}
              <Link
                  href="/auth/login"
                  className="underline underline-offset-4"
                  onClick={() => {
                    setSuccess(false);
                    setIdentifier("");
                    setError(null);
                  }}
              >
                sign in.
              </Link>
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card className="shadow-none">
          <CardHeader>
            <CardTitle
              className="text-2xl"
              style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Reset Your Password
            </CardTitle>
            <CardDescription>
              Type in your email and we&apos;ll send you a link to reset your
              password
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleForgotPassword}>
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
                {error && <p className="text-sm text-red-500">{error}</p>}
                <Button type="submit" className="w-full" disabled={isLoading}>
                  {isLoading ? "Sending..." : "Send reset email"}
                </Button>
              </div>
              <div className="mt-4 text-center text-sm">
                Already have an account?{" "}
                <Link
                  href="/auth/login"
                  className="underline underline-offset-4"
                >
                  Login
                </Link>
              </div>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
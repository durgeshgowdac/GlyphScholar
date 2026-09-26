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
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type UserRole =
    | "student"
    | "teacher"
    | "researcher"
    | "professional"
    | "other";

export function SignUpForm({
                             className,
                             ...props
                           }: React.ComponentPropsWithoutRef<"div">) {
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<UserRole>("student");
  const [password, setPassword] = useState("");
  const [repeatPassword, setRepeatPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const router = useRouter();

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();

    const supabase = createClient();

    setIsLoading(true);
    setError(null);

    const normalizedUsername = username.trim().toLowerCase();
    const normalizedEmail = email.trim().toLowerCase();

    try {
      // Username validation
      if (normalizedUsername.length < 4) {
        throw new Error("Username must be at least 4 characters long.");
      }

      if (normalizedUsername.length > 32) {
        throw new Error("Username cannot exceed 32 characters.");
      }

      if (!/^[a-z0-9_]+$/.test(normalizedUsername)) {
        throw new Error(
            "Username may only contain lowercase letters, numbers, and underscores."
        );
      }

      // Password validation
      if (password !== repeatPassword) {
        throw new Error("Passwords do not match.");
      }

      // Check if username already exists
      const { data: usernameExists, error: usernameError } =
          await supabase.rpc("username_exists", {
            p_username: normalizedUsername,
          });

      if (usernameError) {
        throw usernameError;
      }

      if (usernameExists) {
        throw new Error("Username is already taken.");
      }

      // Create Supabase auth user
      const { data, error: signUpError } = await supabase.auth.signUp({
        email: normalizedEmail,
        password,
        options: {
          data: {
            username: normalizedUsername,
            role,
          },
          // emailRedirectTo: `${window.location.origin}/auth/confirm?next=/auth/login`,
          // emailRedirectTo: `${window.location.origin}/protected`,
          // emailRedirectTo: `${window.location.origin}/auth/confirm`
          emailRedirectTo: `${process.env.NEXT_PUBLIC_SITE_URL ?? window.location.origin}/auth/confirm`,
        },
      });

      // prod-change
      // console.log("SIGNUP RESULT:", data);

      if (signUpError) {
        throw signUpError;
      }

      router.push("/auth/sign-up-success");

    } catch (error: unknown) {
      console.error("Signup error:", error);

      if (error instanceof Error) {
        setError(error.message);
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setIsLoading(false);
    }
  };

  return (
      <div className={cn("flex flex-col gap-6", className)} {...props}>
        <Card className="shadow-none">
          <CardHeader>
            <CardTitle
              className="text-2xl"
              style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Sign up
            </CardTitle>
            <CardDescription>Create a new account</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSignUp}>
              <div className="flex flex-col gap-6">
                <div className="grid gap-2">
                  <Label htmlFor="email">Email</Label>
                  <Input
                      id="email"
                      type="email"
                      placeholder="johndoe@example.com"
                      required
                      value={email}
                      onChange={(e) => setEmail((e.target.value).trim().toLowerCase())}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="username">Username</Label>
                  <Input
                      id="username"
                      type="text"
                      placeholder="johndoe"
                      autoComplete="username"
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                      required
                      minLength={4}
                      maxLength={32}
                      value={username}
                      onChange={(e) => setUsername((e.target.value).trim().toLowerCase())}
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="role">Role</Label>
                  <Select value={role} onValueChange={(value) => setRole(value as UserRole)}>
                    <SelectTrigger>
                      <SelectValue placeholder="Select your role" />
                    </SelectTrigger>

                    <SelectContent>
                      <SelectItem value="student">Student</SelectItem>
                      <SelectItem value="teacher">Teacher</SelectItem>
                      <SelectItem value="researcher">Researcher</SelectItem>
                      <SelectItem value="professional">Professional</SelectItem>
                      <SelectItem value="other">Other</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="grid gap-2">
                  <div className="flex items-center">
                    <Label htmlFor="password">Password</Label>
                  </div>
                  <PasswordInput
                      id="password"
                      autoComplete="new-password"
                      required
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                  />
                </div>
                <div className="grid gap-2">
                  <div className="flex items-center">
                    <Label htmlFor="repeat-password">Confirm Password</Label>
                  </div>
                  <PasswordInput
                      id="repeat-password"
                      autoComplete="new-password"
                      required
                      value={repeatPassword}
                      onChange={(e) => setRepeatPassword(e.target.value)}
                  />
                </div>
                {error && <p className="text-sm text-red-500">{error}</p>}
                <Button type="submit" className="w-full" disabled={isLoading}>
                  {isLoading ? "Creating an account..." : "Sign up"}
                </Button>
              </div>
              <div className="mt-4 text-center text-sm">
                Already have an account?{" "}
                <Link href="/auth/login" className="underline underline-offset-4">
                  Login
                </Link>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
  );
}
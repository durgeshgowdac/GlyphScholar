import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import Link from "next/link";

export default function Page() {
  return (
    <Card className="shadow-none">
      <CardHeader>
        <CardTitle
          className="text-2xl"
          style={{ fontFamily: "var(--font-source-serif)" }}
        >
          Thank you for signing up!
        </CardTitle>
        <CardDescription>Check your email to confirm</CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          You&apos;ve successfully signed up. Please check your email to
          confirm your account before{" "}
          <Link href="/auth/login" className="underline underline-offset-4">
            signing in.
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
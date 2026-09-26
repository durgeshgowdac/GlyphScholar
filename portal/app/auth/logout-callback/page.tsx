"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export default function LogoutCallback() {
  const router = useRouter();

  useEffect(() => {
    const run = async () => {
      const supabase = createClient();
      await supabase.auth.signOut();
      router.replace("/auth/login");
    };
    run();
  }, [router]);

  return <p className="text-sm text-muted-foreground">Signing out...</p>;
}
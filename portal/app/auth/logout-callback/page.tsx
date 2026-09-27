"use client";

import { useEffect } from "react";
import { createClient } from "@/lib/supabase/client";

export default function LogoutCallback() {

  useEffect(() => {
    const run = async () => {
      const supabase = createClient();
      await supabase.auth.signOut();
      window.location.href = "/auth/login"; // full reload
    };
    run();
  }, []);

  return <p className="text-sm text-muted-foreground">Signing out...</p>;
}
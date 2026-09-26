"use client";

import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

export function AuthLogo() {
  const { resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // Avoid a hydration mismatch / flash of the wrong logo: don't render
  // until we know the resolved theme on the client.
  useEffect(() => {
    setMounted(true);
  }, []);

  const src =
    mounted && resolvedTheme === "dark" ? "/logo_dark.svg" : "/logo_light.svg";

  return (
    <div className="flex items-center gap-2">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt="GlyphScholar"
        className="h-8 w-auto"
        style={{ visibility: mounted ? "visible" : "hidden" }}
      />
      <span className="text-lg font-semibold tracking-tight">
        GlyphScholar
      </span>
    </div>
  );
}
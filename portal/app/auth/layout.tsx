export default function AuthLayout({
                                     children,
                                   }: {
  children: React.ReactNode;
}) {
  return (
      <div className="grid h-full md:grid-cols-5">
        {/* Branding panel — carries the landing page's identity into the
          auth flow. The logo, GitHub link, and theme toggle live in the
          shared SiteHeader above this, so this panel only needs to carry
          the pitch. Hidden on small screens for space. */}
        <div className="hidden flex-col justify-center gap-10 border-r bg-muted/30 p-10 md:col-span-2 md:flex">
          <div>
            <p
                className="text-2xl leading-snug"
                style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Ask your papers about the parts other tools skip.
            </p>
            <p className="mt-4 max-w-xs text-sm leading-relaxed text-muted-foreground">
              Hybrid RAG indexes the text. PixelRAG indexes the figures,
              tables, and equations — so an answer can point back to a
              diagram, not just a paragraph.
            </p>
          </div>

          <div className="max-w-[220px]">
            <div className="rounded-lg border bg-card p-3">
              <div className="rounded-md border-2 border-dashed border-[#3c5a8a] p-2 dark:border-[#6f8fc4]">
                <div className="h-10 rounded bg-muted/70" />
              </div>
              <span className="mt-2 inline-block rounded border border-[#3c5a8a] px-1.5 py-0.5 text-xs text-[#3c5a8a] dark:border-[#6f8fc4] dark:text-[#6f8fc4]">
              p. 4 · Fig. 2
            </span>
            </div>
          </div>
        </div>

        {/* Form pane */}
        <div className="flex items-center justify-center p-6 md:col-span-3 md:p-10">
          <div className="w-full max-w-sm">{children}</div>
        </div>
      </div>
  );
}
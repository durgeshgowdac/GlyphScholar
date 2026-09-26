import Link from "next/link";
import { FileText, Layers, Quote } from "lucide-react";
import { Button } from "@/components/ui/button";

const steps = [
  {
    n: "1",
    title: "Upload a PDF",
    body: "MinerU's layout parser reads the document the way a person would — separating running text from figures, tables, and equations instead of flattening everything into one text stream.",
  },
  {
    n: "2",
    title: "It's indexed twice",
    body: "Paragraphs go into a hybrid text index — dense embeddings re-ranked with BM25. Every figure, table, and equation is cropped and embedded as an image in a separate visual index, PixelRAG.",
  },
  {
    n: "3",
    title: "Ask, and get a grounded answer",
    body: "A question is matched against both indexes at once. If the answer lives in a chart rather than a caption, that's what gets retrieved — and the reply points back to the exact page it came from.",
  },
];

const specs = [
  {
    icon: Layers,
    label: "Hybrid text retrieval",
    body: "Dense pgvector similarity and BM25 keyword re-ranking run together, so a question can be phrased loosely or match a term in the document exactly and still land on the right passage.",
  },
  {
    icon: FileText,
    label: "PixelRAG visual grounding",
    body: "Figures, tables, and equations are embedded as images, not summarized into a caption first. A question about a trend line or a formula is matched against the pixels, not a lossy description of them.",
  },
  {
    icon: Quote,
    label: "Page-level citations",
    body: "Every answer carries a reference back to the page and region it was drawn from, so you can check a number or a claim against the source in one click instead of taking it on faith.",
  },
];

export function LandingPage() {
  return (
      <main>
        {/* Hero */}
        <section className="mx-auto grid w-full max-w-5xl gap-12 px-6 py-20 md:grid-cols-5 md:items-center md:py-28">
          <div className="md:col-span-3">
            <h1
                className="text-4xl leading-[1.1] tracking-tight md:text-5xl"
                style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Your papers have figures, tables, and equations. Most PDF
              assistants read past them.
            </h1>
            <p className="mt-6 max-w-md text-base leading-relaxed text-muted-foreground">
              GlyphScholar indexes both the words on the page and the page
              itself, so an answer can point to a diagram or a formula —
              not just paraphrase the sentence next to it.
            </p>
            <div className="mt-8 flex items-center gap-3">
              <Button size="lg" asChild>
                <Link href="/auth/sign-up">Create an account</Link>
              </Button>
              <Button size="lg" variant="outline" asChild>
                <Link href="/auth/login">Log in</Link>
              </Button>
            </div>
          </div>

          {/* Document + citation mock */}
          <div className="md:col-span-2">
            <div className="rounded-lg border bg-card p-4">
              <div className="space-y-2">
                <div className="h-2 w-4/5 rounded-full bg-muted" />
                <div className="h-2 w-full rounded-full bg-muted" />
                <div className="h-2 w-3/5 rounded-full bg-muted" />
              </div>
              <div className="mt-4 rounded-md border-2 border-dashed border-[#3c5a8a] p-3 dark:border-[#6f8fc4]">
                <div className="h-16 rounded bg-muted/70" />
                <p className="mt-2 text-xs text-muted-foreground">
                  Fig. 2 — throughput vs. batch size
                </p>
              </div>
              <div className="mt-4 space-y-2">
                <div className="h-2 w-full rounded-full bg-muted" />
                <div className="h-2 w-2/3 rounded-full bg-muted" />
              </div>
            </div>
            <div className="mt-3 ml-6 rounded-lg border bg-card p-4">
              <p className="text-sm leading-relaxed">
                Throughput peaks around a batch size of 64, then falls off.
              </p>
              <span className="mt-2 inline-block rounded border border-[#3c5a8a] px-1.5 py-0.5 text-xs text-[#3c5a8a] dark:border-[#6f8fc4] dark:text-[#6f8fc4]">
                p. 4 · Fig. 2
              </span>
            </div>
          </div>
        </section>

        {/* How it works */}
        <section className="border-t">
          <div className="mx-auto w-full max-w-5xl px-6 py-20">
            <h2
                className="text-2xl md:text-3xl"
                style={{ fontFamily: "var(--font-source-serif)" }}
            >
              How a document becomes something you can ask questions of
            </h2>
            <div className="mt-10 grid gap-10 md:grid-cols-3 md:gap-8">
              {steps.map((step) => (
                  <div key={step.n}>
                  <span className="text-sm text-muted-foreground">
                    {step.n}
                  </span>
                    <h3 className="mt-2 font-medium">{step.title}</h3>
                    <p className="mt-2 max-w-xs text-sm leading-relaxed text-muted-foreground">
                      {step.body}
                    </p>
                  </div>
              ))}
            </div>
          </div>
        </section>

        {/* Specs / differentiators */}
        <section className="border-t">
          <div className="mx-auto w-full max-w-5xl px-6 py-20">
            <h2
                className="text-2xl md:text-3xl"
                style={{ fontFamily: "var(--font-source-serif)" }}
            >
              What makes the retrieval different
            </h2>
            <dl className="mt-10 divide-y border-t">
              {specs.map(({ icon: Icon, label, body }) => (
                  <div
                      key={label}
                      className="grid gap-2 py-6 md:grid-cols-5 md:gap-8"
                  >
                    <dt className="flex items-center gap-2 md:col-span-2">
                      <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <span className="font-medium">{label}</span>
                    </dt>
                    <dd className="max-w-md text-sm leading-relaxed text-muted-foreground md:col-span-3">
                      {body}
                    </dd>
                  </div>
              ))}
            </dl>
          </div>
        </section>

        {/* Closing CTA */}
        <section className="border-t">
          <div className="mx-auto flex w-full max-w-5xl flex-col items-start justify-between gap-6 px-6 py-20 md:flex-row md:items-center">
            <h2
                className="max-w-sm text-2xl md:text-3xl"
                style={{ fontFamily: "var(--font-source-serif)" }}
            >
              Upload the first paper and see what it finds.
            </h2>
            <Button size="lg" asChild>
              <Link href="/auth/sign-up">Create an account</Link>
            </Button>
          </div>
        </section>
      </main>
  );
}
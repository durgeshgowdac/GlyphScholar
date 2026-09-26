export function SiteFooter() {
    return (
        <footer className="border-t">
            <div
                className="mx-auto flex w-full flex-col gap-1 px-8 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
        <span>
          built (and occasionally broken, then fixed) by Durgesh Gowda C
        </span>
                <span>Copyright © 2026 GlyphScholar. All rights reserved.</span>
            </div>
        </footer>
    );
}
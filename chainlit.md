# GlyphScholar

Upload a document and ask questions about it — GlyphScholar reads both the **text** and the **visuals**.

## How it works

- **Upload a document** using the attachment button to get started.
- **Text retrieval** finds the most relevant passages in your document.
- **PixelRAG visual retrieval** pulls in the actual figures, tables, and equations as image crops — not just captions — so answers can point to what's actually on the page. Full-page images are used as a fallback when no crop is available.
- **A vision-capable model** reads the retrieved text and images together to answer your question.

## Supported files

| Type | Extensions |
|---|---|
| Document | `.pdf` (full text + PixelRAG visual indexing) |
| Image | `.png`, `.jpg`, `.jpeg` |
| Text / data | `.txt`, `.md`, `.csv`, `.json` |

Up to 4 files per upload, 200 MB each. Each file is checked against its actual content, not just its extension — a renamed file that doesn't match its claimed type will be rejected.

A standalone image (not one extracted from a PDF) has no surrounding text to search alongside, so it's matched to your questions more loosely than PDF figures are — for precise figure lookups, uploading the source PDF works better than uploading a cropped image on its own.

## Upload limits

- There's a per-user storage quota shared across all your uploads in a session.
- PDFs count against that quota at a multiple of their file size, since ingestion also renders page images for visual retrieval — so large or image-heavy PDFs use up quota faster than their raw file size suggests.
- If a file would put you over quota, it's skipped (your other files and message still go through) — delete older uploads to free up space.

## Tips

- Ask about specific figures, tables, or charts — GlyphScholar can look directly at them, not just read captions.
- You can upload multiple documents in a session and ask questions across all of them.
- If a file gets rejected, check that its extension matches its actual format.
- If an answer looks off, try rephrasing — retrieval quality depends on how the question is asked.
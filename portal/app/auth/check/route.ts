import { NextRequest, NextResponse } from "next/server";

export async function GET(request: NextRequest) {
  const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
  const cookieHeader = request.headers.get("cookie") ?? "";

  try {
    const res = await fetch(`${backendUrl}/auth/check`, {
      headers: { cookie: cookieHeader },
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch {
    return NextResponse.json({ ok: false }, { status: 503 });
  }
}
import { NextResponse, type NextRequest } from "next/server";

// No session cookie -> the sign-in page. (The API still validates every token; this only saves a
// round trip and keeps the shell from rendering for anonymous visitors.)
export function middleware(req: NextRequest) {
  if (req.cookies.get("fl_session")) return NextResponse.next();
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = `?next=${encodeURIComponent(req.nextUrl.pathname + req.nextUrl.search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!login|api/|_next/|favicon|robots.txt).*)"],
};

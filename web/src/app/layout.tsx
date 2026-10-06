import type { Metadata } from "next";
import localFont from "next/font/local";
import { AppHeader } from "@/components/app-header";
import { SignedIn } from "@/components/sign-in";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

// Pretendard: the open Korean UI face closest to the Toss reference (DESIGN.md, typography).
const pretendard = localFont({
  src: "../../node_modules/pretendard/dist/web/variable/woff2/PretendardVariable.woff2",
  variable: "--font-sans",
  weight: "45 920",
  display: "swap",
});

export const metadata: Metadata = {
  title: "입찰메이트 RFP 도우미",
  description: "과거 제안요청서 질문, 검증, 데이터셋 만들기",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className={`${pretendard.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <TooltipProvider>
          <SignedIn>
            <AppHeader />
            {children}
          </SignedIn>
        </TooltipProvider>
      </body>
    </html>
  );
}

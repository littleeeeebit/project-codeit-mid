import { Suspense } from "react";
import { VerifyPage } from "@/components/verify/verify-page";

export const metadata = { title: "검증 · 입찰메이트" };

export default function Page() {
  return (
    <Suspense>
      <VerifyPage />
    </Suspense>
  );
}
